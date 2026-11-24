"""Testes isolados do rastreador de arquivos por turno."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import git
import pytest

from backend.api import turn_files


def _repo(path: Path) -> git.Repo:
    repo = git.Repo.init(path)
    (path / "README.md").write_text("antes\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("initial")
    return repo


def test_compute_reports_text_changes_and_hunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-1")
    assert snapshot is not None
    before = turn_files.capture_tool_state(snapshot.run_id)
    (tmp_path / "README.md").write_text("antes\ndepois\n", encoding="utf-8")
    turn_files.record_tool_effect(snapshot.run_id, before)

    changes = turn_files.finalize(snapshot)

    assert [change.path for change in changes] == ["README.md"]
    assert changes[0].additions == 1
    assert changes[0].deletions == 0
    assert changes[0].hunks[0]["header"].startswith("@@")


def test_capture_tracks_multiple_repositories_inside_one_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.workspace import workspace as workspace_module

    workspace_root = tmp_path / "workspace"
    first_root = workspace_root / "first"
    second_root = workspace_root / "second"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    _repo(first_root)
    _repo(second_root)
    monkeypatch.setattr(
        workspace_module.workspace_registry,
        "get",
        lambda _workspace_id: SimpleNamespace(cwd=str(workspace_root)),
    )

    snapshot = turn_files.capture("ws-multi", "thread", "run-multi")
    assert snapshot is not None
    assert len(snapshot.repositories) == 2
    before = turn_files.capture_tool_state(snapshot.run_id)
    (first_root / "README.md").write_text("first changed\n", encoding="utf-8")
    (second_root / "README.md").write_text("second changed\n", encoding="utf-8")
    turn_files.record_tool_effect(snapshot.run_id, before)

    changes = turn_files.finalize(snapshot)

    assert [change.path for change in changes] == [
        "first/README.md",
        "second/README.md",
    ]


def test_explicit_file_tool_does_not_attribute_unrelated_concurrent_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    target = tmp_path / "README.md"
    unrelated = tmp_path / "notes.txt"
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-explicit-target")
    assert snapshot is not None
    before = turn_files.capture_tool_state(
        snapshot.run_id,
        "file_write",
        {"file_path": "README.md", "content": "agent"},
    )
    target.write_text("agent edit\n", encoding="utf-8")
    unrelated.write_text("user edit\n", encoding="utf-8")
    turn_files.record_tool_effect(snapshot.run_id, before)

    changes = turn_files.finalize(snapshot)

    assert [change.path for change in changes] == ["README.md"]


def test_compute_excludes_tracked_files_matched_by_gitignore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (tmp_path / ".gitignore").write_text(".godot/\n", encoding="utf-8")
    generated = tmp_path / ".godot" / "editor" / "layout.cfg"
    generated.parent.mkdir(parents=True)
    generated.write_text("before\n", encoding="utf-8")
    repo.index.add([".gitignore", ".godot/editor/layout.cfg"])
    repo.index.commit("track editor state")
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-ignored")
    assert snapshot is not None
    before = turn_files.capture_tool_state(snapshot.run_id)
    (tmp_path / "README.md").write_text("user code\n", encoding="utf-8")
    generated.write_text("editor state changed\n", encoding="utf-8")
    turn_files.record_tool_effect(snapshot.run_id, before)

    changes = turn_files.finalize(snapshot)

    assert [change.path for change in changes] == ["README.md"]


def test_compute_does_not_attribute_preexisting_user_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    (tmp_path / "README.md").write_text("user's unsaved change\n", encoding="utf-8")
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-user-change")
    assert snapshot is not None

    assert turn_files.finalize(snapshot) == []


def test_overlapping_runs_keep_independent_snapshots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    first = turn_files.capture("ws", "thread", "run-a")
    second = turn_files.capture("ws", "thread", "run-b")
    assert first is not None and second is not None
    (tmp_path / "README.md").write_text("a\n", encoding="utf-8")
    turn_files.finalize(first)
    (tmp_path / "README.md").write_text("b\n", encoding="utf-8")
    turn_files.finalize(second)

    assert turn_files.latest("thread", "ws", "run-a")[0] == "run-a"
    assert turn_files.latest("thread", "ws", "run-b")[0] == "run-b"


def test_compute_failure_returns_empty_result(tmp_path: Path) -> None:
    snapshot = turn_files.TurnWorkspaceSnapshot(
        run_id="missing",
        thread_id="thread",
        workspace_id="ws",
        repo_root=tmp_path / "missing-repo",
        scope_root=tmp_path / "missing-repo",
        initial={},
    )

    assert turn_files.compute(snapshot) == []


def test_compute_includes_files_committed_during_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-commit")
    assert snapshot is not None
    before = turn_files.capture_tool_state(snapshot.run_id)
    (tmp_path / "README.md").write_text("antes\ndepois\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("edit during run")
    turn_files.record_tool_effect(snapshot.run_id, before)

    changes = turn_files.finalize(snapshot)

    assert [change.path for change in changes] == ["README.md"]
    assert changes[0].additions == 1


def test_latest_returns_active_run_without_explicit_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-active")
    assert snapshot is not None

    run_id, status, files = turn_files.latest("thread", "ws")

    assert run_id == "run-active"
    assert status == "active"
    assert files == []


def test_latest_ignores_empty_run_id_as_active_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-empty")
    assert snapshot is not None

    run_id, status, _files = turn_files.latest("thread", "ws", "")

    assert run_id == "run-empty"
    assert status == "active"


def test_capture_tracks_changes_before_first_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git.Repo.init(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-unborn")
    assert snapshot is not None
    before = turn_files.capture_tool_state(snapshot.run_id)
    (tmp_path / "new.py").write_text("print('ok')\n", encoding="utf-8")
    turn_files.record_tool_effect(snapshot.run_id, before)

    changes = turn_files.finalize(snapshot)

    assert [change.path for change in changes] == ["new.py"]
    assert changes[0].status == "A"


def test_oversized_committed_file_is_not_reported_as_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    large = "x" * (turn_files.MAX_FILE_BYTES + 1)
    (tmp_path / "large.txt").write_text(large, encoding="utf-8")
    repo.index.add(["large.txt"])
    repo.index.commit("add large file")
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-large")
    assert snapshot is not None
    before = turn_files.capture_tool_state(snapshot.run_id)
    (tmp_path / "large.txt").write_text(large + "y", encoding="utf-8")
    repo.index.add(["large.txt"])
    repo.index.commit("edit large file")
    turn_files.record_tool_effect(snapshot.run_id, before)

    assert turn_files.finalize(snapshot) == []


def test_unobserved_workspace_changes_are_not_attributed_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-concurrent-user-edit")
    assert snapshot is not None
    (tmp_path / "README.md").write_text("alterado fora de uma tool\n", encoding="utf-8")

    assert turn_files.finalize(snapshot) == []


def test_tool_summary_uses_file_content_at_first_tool_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        turn_files,
        "_repo_and_scope",
        lambda _workspace_id: (repo, tmp_path, tmp_path),
    )

    snapshot = turn_files.capture("ws", "thread", "run-concurrent-edit")
    assert snapshot is not None
    (tmp_path / "README.md").write_text("alteração do usuário\n", encoding="utf-8")
    before_tool = turn_files.capture_tool_state(snapshot.run_id)
    (tmp_path / "README.md").write_text("alteração do agente\n", encoding="utf-8")
    turn_files.record_tool_effect(snapshot.run_id, before_tool)

    changes = turn_files.finalize(snapshot)

    assert [change.path for change in changes] == ["README.md"]
    assert changes[0].additions == changes[0].deletions == 1
    assert "antes" not in str(changes[0].hunks)
    assert "alteração do agente" in str(changes[0].hunks)
