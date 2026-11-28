"""Testes de isolamento das rotas REST de sessões PTY."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest
from fastapi import HTTPException, Request

import backend.api.handlers.terminal as terminal_handler
from backend.api.handlers.terminal import CloseBody, close_terminal, list_terminals
from backend.services.policy_compiler import EgressPolicy
from backend.services.pty_registry import pty_registry
from backend.services.pty_session import PtySession
from backend.vtypes.contracts import JsonObject, JsonValue


def _request(user_id: str) -> Request:
    return cast(
        "Request",
        SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=user_id))),
    )


def _session(
    terminal_id: str,
    *,
    user_id: str,
    thread_id: str,
    workspace_id: str,
) -> PtySession:
    return cast(
        "PtySession",
        SimpleNamespace(
            terminal_id=terminal_id,
            user_id=user_id,
            thread_id=thread_id,
            workspace_id=workspace_id,
            is_alive=lambda: True,
            close=lambda: None,
        ),
    )


class _FakeWebSocket:
    def __init__(self, **query: str) -> None:
        self.query_params = query
        self.sent_text: list[str] = []
        self.close_code: int | None = None

    async def accept(self) -> None:
        return None

    async def send_text(self, value: str) -> None:
        self.sent_text.append(value)

    async def send_bytes(self, _value: bytes) -> None:
        return None

    async def close(self, *, code: int) -> None:
        self.close_code = code

    async def receive(self) -> dict[str, str]:
        return {"type": "websocket.disconnect"}


class _FakeInteractiveSession:
    def __init__(self, terminal_id: str, workspace_id: str) -> None:
        self.terminal_id = terminal_id
        self.workspace_id = workspace_id
        self.thread_id = "thread"
        self.user_id = "local"
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()

    def is_alive(self) -> bool:
        return True

    def subscribe(self) -> asyncio.Queue[bytes | None]:
        return self.queue

    def unsubscribe(self, _queue: asyncio.Queue[bytes | None]) -> None:
        return None

    def write(self, _data: bytes) -> None:
        return None

    def resize(self, _cols: int, _rows: int) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    pty_registry._sessions.clear()
    pty_registry._closed.clear()
    yield
    pty_registry._sessions.clear()
    pty_registry._closed.clear()


@pytest.mark.asyncio
async def test_list_route_filters_user_and_workspace_but_shares_threads() -> None:
    pty_registry.add(
        _session(
            "owned",
            user_id="alice",
            thread_id="thread-1",
            workspace_id="workspace-1",
        )
    )
    pty_registry.add(
        _session(
            "other-user",
            user_id="bob",
            thread_id="thread-1",
            workspace_id="workspace-1",
        )
    )
    pty_registry.add(
        _session(
            "other-thread",
            user_id="alice",
            thread_id="thread-2",
            workspace_id="workspace-1",
        )
    )
    pty_registry.add(
        _session(
            "other-workspace",
            user_id="alice",
            thread_id="thread-1",
            workspace_id="workspace-2",
        )
    )

    result = await list_terminals(
        _request("alice"), thread_id="thread-1", workspace_id="workspace-1"
    )

    assert [item["terminal_id"] for item in result["terminals"]] == [
        "other-thread",
        "owned",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "thread_id", "workspace_id"),
    [
        ("bob", "thread-1", "workspace-1"),
        ("alice", "thread-1", "workspace-2"),
    ],
)
async def test_close_route_rejects_user_or_workspace_mismatch(
    user_id: str, thread_id: str, workspace_id: str
) -> None:
    pty_registry.add(
        _session(
            "owned",
            user_id="alice",
            thread_id="thread-1",
            workspace_id="workspace-1",
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        await close_terminal(
            CloseBody(
                terminal_id="owned",
                thread_id=thread_id,
                workspace_id=workspace_id,
            ),
            _request(user_id),
        )

    assert exc_info.value.status_code == 404
    assert pty_registry.get("owned") is not None


@pytest.mark.asyncio
async def test_close_route_closes_only_matching_context() -> None:
    pty_registry.add(
        _session(
            "owned",
            user_id="alice",
            thread_id="thread-1",
            workspace_id="workspace-1",
        )
    )

    result = await close_terminal(
        CloseBody(
            terminal_id="owned",
            thread_id="thread-1",
            workspace_id="workspace-1",
        ),
        _request("alice"),
    )

    assert result == {"status": "closed", "terminal_id": "owned"}
    assert pty_registry.get("owned") is None


@pytest.mark.asyncio
async def test_list_route_thread_scope_returns_only_this_conversation() -> None:
    for terminal_id, thread_id in (("here", "thread-1"), ("elsewhere", "thread-2")):
        pty_registry.add(
            _session(
                terminal_id,
                user_id="alice",
                thread_id=thread_id,
                workspace_id="workspace-1",
            )
        )

    result = await list_terminals(
        _request("alice"),
        thread_id="thread-1",
        workspace_id="workspace-1",
        scope="thread",
    )

    assert [item["terminal_id"] for item in result["terminals"]] == ["here"]


@pytest.mark.asyncio
async def test_close_route_thread_scope_rejects_terminal_from_another_thread() -> None:
    pty_registry.add(
        _session(
            "elsewhere",
            user_id="alice",
            thread_id="thread-2",
            workspace_id="workspace-1",
        )
    )

    with pytest.raises(HTTPException) as excinfo:
        await close_terminal(
            CloseBody(
                terminal_id="elsewhere",
                thread_id="thread-1",
                workspace_id="workspace-1",
                scope="thread",
            ),
            _request("alice"),
        )

    assert excinfo.value.status_code == 404
    assert pty_registry.get("elsewhere") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace_case", ["existing", "newly-created"])
async def test_terminal_websocket_creates_interactive_pty_for_trusted_local_workspace(
    workspace_case: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    workspace_path = tmp_path / workspace_case
    workspace_path.mkdir()
    workspace = SimpleNamespace(
        id=f"workspace-{workspace_case}",
        cwd=str(workspace_path),
        trusted=True,
        transport="local",
        owner_id=None,
    )
    created: JsonObject = {}
    session = _FakeInteractiveSession("terminal-1", workspace.id)

    def create_pty(**kwargs: JsonValue) -> _FakeInteractiveSession:
        created.update(kwargs)
        return session

    async def no_egress(_workspace_id: str, _policy: EgressPolicy) -> None:
        return None

    monkeypatch.setattr(terminal_handler, "_auth_enabled", lambda: False)
    monkeypatch.setattr(terminal_handler, "_user_from_token", _async_none)
    monkeypatch.setattr(
        terminal_handler, "_trusted_workspace", lambda _workspace_id: workspace
    )
    monkeypatch.setattr(terminal_handler.PtySession, "create", create_pty)
    monkeypatch.setattr(
        "backend.sandbox.policy.resolve_policy", lambda _path: "workspace-policy"
    )
    monkeypatch.setattr("backend.sandbox.egress_proxy.egress_dir_for", no_egress)
    ws = _FakeWebSocket(
        thread_id="thread",
        workspace_id=workspace.id,
        terminal_id=session.terminal_id,
    )

    await terminal_handler.terminal_ws(cast("Any", ws))

    assert created["cwd"] == str(Path(workspace.cwd))
    assert created["workspace_id"] == workspace.id
    assert created["thread_id"] == "thread"
    assert created["policy"] == "workspace-policy"
    assert pty_registry.get(session.terminal_id) is session
    assert [json.loads(message)["type"] for message in ws.sent_text] == ["connected"]


async def _async_none(_token: str) -> None:
    return None


@pytest.mark.asyncio
async def test_terminal_websocket_returns_pty_creation_error_to_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = SimpleNamespace(
        id="workspace-1",
        cwd=str(tmp_path),
        trusted=True,
        transport="local",
        owner_id=None,
    )

    def fail_to_create[**P](*_args: P.args, **_kwargs: P.kwargs) -> NoReturn:
        raise RuntimeError("PTY backend unavailable")

    async def no_egress(_workspace_id: str, _policy: EgressPolicy) -> None:
        return None

    monkeypatch.setattr(terminal_handler, "_auth_enabled", lambda: False)
    monkeypatch.setattr(terminal_handler, "_user_from_token", _async_none)
    monkeypatch.setattr(
        terminal_handler, "_trusted_workspace", lambda _workspace_id: workspace
    )
    monkeypatch.setattr(terminal_handler.PtySession, "create", fail_to_create)
    monkeypatch.setattr(
        "backend.sandbox.policy.resolve_policy", lambda _path: "workspace-policy"
    )
    monkeypatch.setattr("backend.sandbox.egress_proxy.egress_dir_for", no_egress)
    ws = _FakeWebSocket(thread_id="thread", workspace_id=workspace.id)

    await terminal_handler.terminal_ws(cast("Any", ws))

    assert json.loads(ws.sent_text[0]) == {
        "type": "error",
        "message": "PTY backend unavailable",
    }
    assert ws.close_code == 1011
