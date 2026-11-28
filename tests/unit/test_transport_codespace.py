from __future__ import annotations

import asyncio
import base64
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.transport import codespace
from backend.transport.base import RunResult
from backend.transport.codespace import CodespaceTransport


async def _timed_out[T](
    awaitable: Coroutine[None, None, T],
    timeout: float,  # noqa: ASYNC109
) -> None:
    awaitable.close()
    raise TimeoutError(f"timeout={timeout}")


@pytest.fixture
def transport() -> CodespaceTransport:
    return CodespaceTransport(
        codespace_name="example", remote_path="/workspaces/project"
    )


async def test_read_file_returns_bounded_bytes(transport, monkeypatch) -> None:
    binary = b"\x00\xffcontent"
    run = AsyncMock(
        return_value=RunResult(0, base64.b64encode(binary).decode("ascii"), "")
    )
    monkeypatch.setattr(transport, "run", run)

    assert await transport.read_file("/workspaces/project/a.txt", 12) == binary
    run.assert_awaited_once()


async def test_list_dir_preserves_names_with_spaces_and_file_sizes(
    transport, monkeypatch
) -> None:
    run = AsyncMock(
        return_value=RunResult(
            0,
            '[{"name":"a file.txt","is_dir":false,"size":17},'
            '{"name":"source dir","is_dir":true,"size":null}]',
            "",
        )
    )
    monkeypatch.setattr(transport, "run", run)

    entries = await transport.list_dir("/workspaces/project")

    assert [(entry.name, entry.is_dir, entry.size) for entry in entries] == [
        ("a file.txt", False, 17),
        ("source dir", True, None),
    ]


async def test_ensure_started_succeeds_without_host_gh(monkeypatch) -> None:
    process = AsyncMock()
    process.returncode = 0
    monkeypatch.setattr(
        codespace.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )

    assert await codespace.ensure_started("example")
    process.wait.assert_awaited_once()


async def test_ensure_started_timeout_kills_process(monkeypatch) -> None:
    process = AsyncMock()
    process.wait = AsyncMock(return_value=0)
    process.kill = AsyncMock()
    monkeypatch.setattr(
        codespace.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(codespace.asyncio, "wait_for", _timed_out)

    assert not await codespace.ensure_started("example")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_run_reports_missing_gh_cli(transport, monkeypatch) -> None:
    monkeypatch.setattr(
        codespace,
        "ensure_started",
        AsyncMock(side_effect=FileNotFoundError("gh")),
    )

    result = await transport.run(["pwd"], cwd=".")

    assert result.exit_code == 127
    assert "não encontrado" in result.stderr


async def test_run_reports_ssh_timeout(transport, monkeypatch) -> None:
    process = AsyncMock()
    process.communicate = AsyncMock()
    process.kill = AsyncMock()
    process.wait = AsyncMock()
    monkeypatch.setattr(codespace, "ensure_started", AsyncMock(return_value=True))
    monkeypatch.setattr(
        codespace.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    monkeypatch.setattr(codespace.asyncio, "wait_for", _timed_out)

    result = await transport.run(["pwd"], cwd=".", timeout=0.01)

    assert result.exit_code == -1
    assert "Timeout" in result.stderr
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


async def test_read_file_surfaces_missing_file(transport, monkeypatch) -> None:
    monkeypatch.setattr(
        transport,
        "run",
        AsyncMock(return_value=RunResult(1, "", "No such file or directory")),
    )

    with pytest.raises(FileNotFoundError):
        await transport.read_file("/workspaces/project/missing.txt")


@pytest.mark.parametrize("operation", ["list_dir", "read_file", "write_file"])
async def test_filesystem_operations_surface_cli_failure(
    transport, monkeypatch, operation
) -> None:
    monkeypatch.setattr(
        transport,
        "run",
        AsyncMock(return_value=RunResult(127, "", "gh unavailable")),
    )

    call = (
        transport.list_dir("/workspaces/project")
        if operation == "list_dir"
        else transport.read_file("/workspaces/project/a.txt")
        if operation == "read_file"
        else transport.write_file("/workspaces/project/a.txt", b"content")
    )
    with pytest.raises(RuntimeError, match="gh unavailable"):
        await call
