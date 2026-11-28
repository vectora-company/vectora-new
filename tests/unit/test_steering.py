"""Orientações do usuário durante o run: fila, loop e evento SSE."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from backend.engine import steering
from backend.engine.conversation_loop import LoopConfig, run_conversation
from backend.engine.stream_events import EngineEvent, SteerApplied
from backend.persistence.native.session_store import SessionStore
from backend.storage.sqlite.pool import AsyncConnectionPool
from backend.tools.context import ToolContext
from backend.tools.registry import ToolRegistry, ToolSpec
from backend.vtypes.message import MessageRole, VMessage, VMessageChunk, text_message


@pytest.fixture(autouse=True)
def _clean_queue() -> None:
    steering._pending.clear()


def test_steers_are_delivered_in_order_and_cleared() -> None:
    assert steering.push_steer("t", "  primeiro ")
    assert steering.push_steer("t", "segundo")

    assert steering.has_pending_steers("t")
    assert steering.drain_steers("t") == ["primeiro", "segundo"]
    assert steering.drain_steers("t") == []
    assert not steering.has_pending_steers("t")


def test_empty_text_and_full_queue_are_refused() -> None:
    assert not steering.push_steer("t", "   ")
    for i in range(5):
        assert steering.push_steer("t", f"o{i}")
    assert not steering.push_steer("t", "excedente")


def test_steer_text_is_capped() -> None:
    steering.push_steer("t", "x" * 10_000)
    assert len(steering.drain_steers("t")[0]) == 4000


def test_discard_drops_leftovers() -> None:
    steering.push_steer("t", "sobrou")
    steering.discard_steers("t")
    assert steering.drain_steers("t") == []


def test_steer_event_becomes_sse_event() -> None:
    from backend.engine.sse_adapter import to_sse_line

    payload = json.loads(
        to_sse_line(SteerApplied(content="use pnpm")).removeprefix("data: ")
    )

    assert payload["type"] == "steer_applied"
    assert payload["content"] == "use pnpm"


class _RecordingClient:
    """Guarda o histórico recebido em cada chamada e responde sempre ``ok``."""

    def __init__(self) -> None:
        self.histories: list[list[str]] = []

    async def astream(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[VMessageChunk]:
        self.histories.append(
            [m.text() for m in messages if m.role is MessageRole.USER]
        )
        if len(self.histories) == 1:
            # A orientação chega enquanto o modelo ainda responde.
            steering.push_steer("thread-s", "responda em português")
        yield VMessageChunk(delta_text="ok")

    async def agenerate(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> VMessage:
        raise NotImplementedError


@pytest.fixture
async def session_store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    pool = AsyncConnectionPool(str(tmp_path / "steer.db"), min_size=1, max_size=2)
    await pool.open()
    store = SessionStore(pool)
    await store.setup()
    await store.create_session("thread-s", user_id="alice")
    try:
        yield store
    finally:
        await pool.close()


async def test_loop_applies_a_steer_that_arrives_during_the_answer(
    session_store: SessionStore,
) -> None:
    await session_store.append_message(
        "thread-s", text_message(MessageRole.USER, "explique o projeto")
    )
    client = _RecordingClient()
    events: list[EngineEvent] = []

    async def on_event(event: EngineEvent) -> None:
        events.append(event)

    await run_conversation(
        session_store=session_store,
        chat_client=client,
        tool_registry=ToolRegistry(),
        ctx=ToolContext(user_id="alice", thread_id="thread-s"),
        thread_id="thread-s",
        config=LoopConfig(run_id="run-s"),
        on_event=on_event,
    )

    assert len(client.histories) == 2
    assert client.histories[0] == ["explique o projeto"]
    assert client.histories[1] == ["explique o projeto", "responda em português"]
    assert [e.content for e in events if isinstance(e, SteerApplied)] == [
        "responda em português"
    ]


async def test_steer_endpoint_queues_only_when_a_run_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.api.handlers import chat as chat_handlers
    from backend.api.handlers import threads as thread_handlers

    async def allow(*_args: str | None, **_kwargs: bool) -> None:
        return None

    monkeypatch.setattr(thread_handlers, "_assert_owns_thread", allow)
    request = chat_handlers.SteerRunRequest(thread_id="thread-e", text="mude o foco")

    monkeypatch.setattr("backend.api.native_stream.has_active_run", lambda _t: False)
    assert await chat_handlers.steer_run(request, None) == {  # ty: ignore[invalid-argument-type]
        "queued": False,
        "active": False,
    }
    assert not steering.has_pending_steers("thread-e")

    monkeypatch.setattr("backend.api.native_stream.has_active_run", lambda _t: True)
    assert await chat_handlers.steer_run(request, None) == {  # ty: ignore[invalid-argument-type]
        "queued": True,
        "active": True,
    }
    assert steering.drain_steers("thread-e") == ["mude o foco"]
