"""Reversão de commit pelo Git workbench."""

from __future__ import annotations

from pathlib import Path

import git
import pytest

from backend.api.handlers import workspaces


def _repo_with_change(root: Path) -> tuple[git.Repo, str]:
    repo = git.Repo.init(root)
    with repo.config_writer() as config:
        config.set_value("user", "name", "Teste")
        config.set_value("user", "email", "teste@example.com")
    target = root / "a.txt"
    for content in ("antes\n", "depois\n"):
        target.write_text(content, encoding="utf-8")
        repo.index.add(["a.txt"])
        commit = repo.index.commit(content.strip())
    return repo, commit.hexsha


@pytest.mark.asyncio
async def test_revert_desfaz_o_commit_no_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, sha = _repo_with_change(tmp_path)
    monkeypatch.setattr(workspaces, "_open_workspace_repo", lambda _id: repo)

    response = await workspaces.git_revert_commit(
        "ws", workspaces.RevertCommitRequest(sha=sha)
    )

    assert response.status == "ok", response.message
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "antes\n"


@pytest.mark.asyncio
async def test_revert_recusa_valor_que_viraria_opcao_do_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _sha = _repo_with_change(tmp_path)
    monkeypatch.setattr(workspaces, "_open_workspace_repo", lambda _id: repo)

    response = await workspaces.git_revert_commit(
        "ws", workspaces.RevertCommitRequest(sha="--abort")
    )

    assert response.status == "error"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "depois\n"
