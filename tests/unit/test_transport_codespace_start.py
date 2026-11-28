from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any, NoReturn
from unittest.mock import AsyncMock

import pytest

from backend.transport import codespace
from backend.transport.codespace import CodespaceTransport


async def _timed_out[T](
    awaitable: Coroutine[None, None, T],
    timeout: float,  # noqa: ASYNC109
) -> NoReturn:
    awaitable.close()
    raise TimeoutError(f"timeout={timeout}")


@pytest.mark.asyncio
async def test_run_starts_codespace_before_running_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    start_process = AsyncMock()
    start_process.returncode = 0
    ssh_process = AsyncMock()
    ssh_process.returncode = 0
    ssh_process.communicate.return_value = (b"ready\n", b"")

    async def create_process(*args: str, **kwargs: int) -> AsyncMock:
        calls.append(tuple(args))
        return start_process if len(calls) == 1 else ssh_process

    monkeypatch.setattr(codespace.asyncio, "create_subprocess_exec", create_process)
    transport = CodespaceTransport(
        codespace_name="example", remote_path="/workspaces/project"
    )

    result = await transport.run(["printf", "%s", "ready"], cwd=".")

    assert result.exit_code == 0
    assert result.stdout == "ready\n"
    assert [call[:3] for call in calls] == [
        ("gh", "codespace", "start"),
        ("gh", "codespace", "ssh"),
    ]
    assert start_process.wait.await_count == 1


@pytest.mark.asyncio
async def test_run_does_not_open_ssh_when_codespace_start_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock()
    process.kill = AsyncMock()
    process.wait = AsyncMock(return_value=0)
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(codespace.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(codespace.asyncio, "wait_for", _timed_out)
    transport = CodespaceTransport(
        codespace_name="example", remote_path="/workspaces/project"
    )

    result = await transport.run(["pwd"], cwd=".")

    assert result.exit_code == 1
    assert "Não foi possível iniciar" in result.stderr
    create_process.assert_awaited_once()
    process.kill.assert_called_once()


@pytest.mark.asyncio
async def test_run_reports_missing_gh_without_running_remote_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ensure_started = AsyncMock(side_effect=FileNotFoundError("gh"))
    create_process = AsyncMock()
    monkeypatch.setattr(codespace, "ensure_started", ensure_started)
    monkeypatch.setattr(codespace.asyncio, "create_subprocess_exec", create_process)
    transport = CodespaceTransport(
        codespace_name="example", remote_path="/workspaces/project"
    )

    result = await transport.run(["pwd"], cwd=".")

    assert result.exit_code == 127
    assert "GitHub CLI (gh) não encontrado" in result.stderr
    ensure_started.assert_awaited_once_with("example")
    create_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_started_timeout_terminates_start_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = AsyncMock()
    process.kill = AsyncMock()
    process.wait = AsyncMock(return_value=0)
    monkeypatch.setattr(
        codespace.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(codespace.asyncio, "wait_for", _timed_out)

    assert not await codespace.ensure_started("example")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
