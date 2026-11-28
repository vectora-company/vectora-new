from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from backend.api.handlers import workspaces
from backend.transport.base import DirEntry, RunResult


@pytest.fixture
def remote(monkeypatch: pytest.MonkeyPatch):
    ws = SimpleNamespace(transport="codespace", remote_path="/workspaces/project")
    transport = AsyncMock()

    async def resolve(_workspace_id: str, path: str):
        target = f"/workspaces/project/{path}" if path else "/workspaces/project"
        return ws, transport, target

    monkeypatch.setattr(
        workspaces,
        "_remote_fs_context",
        AsyncMock(side_effect=resolve),
    )
    return ws, transport


async def test_remote_tree_lists_workspace_entries(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ws, transport = remote
    monkeypatch.setattr(
        workspaces,
        "_remote_fs_stat",
        AsyncMock(return_value={"is_dir": True}),
    )
    transport.list_dir.return_value = [
        DirEntry(
            name="a.txt", path="/workspaces/project/src/a.txt", is_dir=False, size=4
        )
    ]

    result = await workspaces.workspace_tree("ws", path="src")

    assert result.entries[0].path == "src/a.txt"
    assert result.entries[0].size == 4
    transport.list_dir.assert_awaited_once_with("/workspaces/project/src")


async def test_remote_file_preview_and_hash(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ws, transport = remote
    content = b"remote"
    monkeypatch.setattr(
        workspaces,
        "_remote_fs_stat",
        AsyncMock(return_value={"is_file": True, "size": len(content)}),
    )
    transport.read_file.return_value = content

    result = await workspaces.workspace_file("ws", path="src/a.txt")

    assert result.content == "remote"
    assert result.sha256 == hashlib.sha256(content).hexdigest()


async def test_remote_update_uses_optimistic_hash(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ws, transport = remote
    current = b"old"
    monkeypatch.setattr(
        workspaces,
        "_remote_fs_stat",
        AsyncMock(return_value={"is_file": True, "size": len(current)}),
    )
    transport.read_file.return_value = current

    with pytest.raises(HTTPException) as exc:
        await workspaces.update_fs_file(
            "ws",
            "src/a.txt",
            workspaces.UpdateFsFileRequest(content="new", expected_sha256="stale"),
        )

    assert exc.value.status_code == 412
    transport.write_file.assert_not_awaited()


async def test_remote_create_file_uses_transport(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ws, transport = remote
    monkeypatch.setattr(
        workspaces,
        "_remote_fs_stat",
        AsyncMock(return_value={"exists": False}),
    )

    result = await workspaces.create_fs_file(
        "ws", workspaces.CreateFsNodeRequest(path="src/new.txt", content="hello")
    )

    assert result.status == "ok"
    transport.write_file.assert_awaited_once_with(
        "/workspaces/project/src/new.txt", b"hello"
    )


async def test_remote_create_directory_uses_confined_path(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, transport = remote
    run = AsyncMock()
    monkeypatch.setattr(workspaces, "_remote_fs_run", run)

    result = await workspaces.create_fs_dir(
        "ws", workspaces.CreateFsNodeRequest(path="src/new")
    )

    assert result.status == "ok"
    run.assert_awaited_once_with(
        transport,
        ws,
        "import os,sys; os.makedirs(sys.argv[1], exist_ok=True)",
        "/workspaces/project/src/new",
    )


async def test_remote_delete_requires_explicit_permanent_confirmation(remote) -> None:
    _ws, transport = remote

    result = await workspaces.delete_fs_node("ws", path="src/a.txt", permanent=False)

    assert result.status == "error"
    assert "lixeira" in result.message
    transport.run.assert_not_awaited()


async def test_remote_move_checks_conflicts_and_runs_rename(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    ws, transport = remote
    monkeypatch.setattr(
        workspaces,
        "_remote_fs_stat",
        AsyncMock(side_effect=[{"exists": True}, {"exists": False}]),
    )
    run = AsyncMock()
    monkeypatch.setattr(workspaces, "_remote_fs_run", run)

    result = await workspaces.move_fs_node(
        "ws", workspaces.MoveFsNodeRequest(from_path="src/a.txt", to_path="src/b.txt")
    )

    assert result.status == "ok"
    run.assert_awaited_once_with(
        transport,
        ws,
        "import os,sys; s,d=sys.argv[1:]; os.makedirs(os.path.dirname(d), exist_ok=True); os.rename(s,d)",
        "/workspaces/project/src/a.txt",
        "/workspaces/project/src/b.txt",
    )


async def test_remote_update_writes_new_content_after_hash_check(
    remote, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ws, transport = remote
    current = b"old"
    monkeypatch.setattr(
        workspaces,
        "_remote_fs_stat",
        AsyncMock(return_value={"is_file": True, "size": len(current)}),
    )
    transport.read_file.return_value = current
    new_content = "updated"

    result = await workspaces.update_fs_file(
        "ws",
        "src/a.txt",
        workspaces.UpdateFsFileRequest(
            content=new_content, expected_sha256=hashlib.sha256(current).hexdigest()
        ),
    )

    assert result.status == "ok"
    assert result.sha256 == hashlib.sha256(new_content.encode()).hexdigest()
    transport.write_file.assert_awaited_once_with(
        "/workspaces/project/src/a.txt", new_content.encode()
    )


async def test_remote_search_returns_relative_matches(remote) -> None:
    _ws, transport = remote
    transport.run.return_value = RunResult(
        0,
        json.dumps(
            {
                "type": "match",
                "data": {
                    "path": {"text": "/workspaces/project/src/a.txt"},
                    "line_number": 3,
                    "lines": {"text": "needle here\n"},
                },
            }
        ),
        "",
    )

    result = await workspaces.search_workspace_files("ws", q="needle", path="src")

    assert result.hits[0].path == "a.txt"
    assert result.hits[0].line_number == 3


async def test_remote_path_resolver_rejects_traversal_without_transport_call() -> None:
    from backend.workspace.remote_fs import resolve_remote_workspace_path

    transport = AsyncMock()
    ws = MagicMock(remote_path="/workspaces/project")

    resolved, error = await resolve_remote_workspace_path(transport, ws, "../escape")

    assert resolved is None
    assert "escapes" in (error or "")
    transport.run.assert_not_awaited()


async def test_remote_path_resolver_checks_canonical_symlink_target() -> None:
    from backend.workspace.remote_fs import resolve_remote_workspace_path

    transport = AsyncMock()
    transport.run.return_value = RunResult(2, "", "")
    ws = MagicMock(remote_path="/workspaces/project")

    resolved, error = await resolve_remote_workspace_path(transport, ws, "linked/key")

    assert resolved is None
    assert "escapes" in (error or "")
