"""Verificação do turno no chat: evento SSE, emissão no loop e histórico."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.engine.conversation_loop import LoopConfig, run_conversation
from backend.engine.stream_events import EngineEvent, VerificationFinished
from backend.persistence.native.session_store import SessionStore
from backend.services.verification.engine import VerificationResult
from backend.storage.sqlite.pool import AsyncConnectionPool
from backend.tools.context import ToolContext
from backend.tools.registry import ToolRegistry, ToolSpec
from backend.vtypes.message import MessageRole, VMessage, VMessageChunk, text_message


def test_verification_event_becomes_sse_event() -> None:
    from backend.engine.sse_adapter import to_sse_line

    line = to_sse_line(
        VerificationFinished(status="degraded", outcome="not_run", detail="sem gates")
    )

    payload = json.loads(line.removeprefix("data: "))
    assert payload["type"] == "verification"
    assert payload["status"] == "degraded"
    assert payload["outcome"] == "not_run"
    assert payload["detail"] == "sem gates"


class _AnsweringClient:
    async def astream(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[VMessageChunk]:
        yield VMessageChunk(delta_text="feito")

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
    pool = AsyncConnectionPool(str(tmp_path / "verify.db"), min_size=1, max_size=2)
    await pool.open()
    store = SessionStore(pool)
    await store.setup()
    await store.create_session("thread-v", user_id="alice")
    try:
        yield store
    finally:
        await pool.close()


async def _run_with_verification(
    session_store: SessionStore,
    monkeypatch: pytest.MonkeyPatch,
    result: VerificationResult,
) -> list[VerificationFinished]:
    async def fake_run_verification(*_args: str | None, **_kwargs: str | None):
        return result

    monkeypatch.setattr(
        "backend.services.verification.run_verification", fake_run_verification
    )
    await session_store.append_message(
        "thread-v", text_message(MessageRole.USER, "faça")
    )
    events: list[EngineEvent] = []

    async def on_event(event: EngineEvent) -> None:
        events.append(event)

    await run_conversation(
        session_store=session_store,
        chat_client=_AnsweringClient(),
        tool_registry=ToolRegistry(),
        ctx=ToolContext(user_id="alice", thread_id="thread-v"),
        thread_id="thread-v",
        config=LoopConfig(run_id="run-v"),
        on_event=on_event,
    )
    return [e for e in events if isinstance(e, VerificationFinished)]


async def test_loop_emits_verification_when_a_gate_ran(
    session_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = VerificationResult(
        passed=True, status="passed", snapshot_sha256="abc", outcome="passed"
    )

    emitted = await _run_with_verification(session_store, monkeypatch, result)

    assert [(e.status, e.outcome) for e in emitted] == [("passed", "passed")]


async def test_loop_stays_quiet_for_a_plain_chat_turn(
    session_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = VerificationResult(
        passed=False, status="degraded", snapshot_sha256="abc", outcome="not_run"
    )

    emitted = await _run_with_verification(session_store, monkeypatch, result)

    assert emitted == []


async def test_history_verification_goes_on_the_turns_last_answer() -> None:
    from backend.services.agent_factory import _history_verifications

    run = SimpleNamespace(
        run_id="r1",
        detail="Verificação concluída com sucesso.",
        evidence=[
            {"kind": "quality_gate_verification", "step_id": "s1", "status": "failed"},
            {
                "kind": "quality_gate_verification",
                "status": "passed",
                "outcome": "passed",
            },
        ],
    )
    plain_run = SimpleNamespace(run_id="r2", detail="", evidence=[])

    class _Store:
        async def list_agent_runs(
            self, thread_id: str, *, limit: int = 50
        ) -> list[SimpleNamespace]:
            return [run, plain_run]

        async def latest_assistant_message_id(self, thread_id: str, run_id: str) -> int:
            return {"r1": 42, "r2": 43}[run_id]

    verifications = await _history_verifications(_Store(), "t")  # ty: ignore[invalid-argument-type]

    assert set(verifications) == {"42"}
    assert verifications["42"].status == "passed"
    assert verifications["42"].outcome == "passed"
    assert verifications["42"].detail == "Verificação concluída com sucesso."


def _not_run_evidence() -> list[dict]:
    return [
        {
            "kind": "quality_gate_verification",
            "status": "degraded",
            "outcome": "not_run",
        }
    ]


async def test_history_hides_not_verified_on_a_plain_chat_turn() -> None:
    """Conversa sem edição e sem gate não ganha "Não verificado" ao recarregar."""
    from backend.services.agent_factory import _history_verifications

    chat_run = SimpleNamespace(
        run_id="r1",
        detail="Nenhum quality gate foi executado: a execução não foi verificada.",
        evidence=_not_run_evidence(),
        files_touched=[],
    )

    class _Store:
        async def list_agent_runs(
            self, thread_id: str, *, limit: int = 50
        ) -> list[SimpleNamespace]:
            return [chat_run]

        async def latest_assistant_message_id(self, thread_id: str, run_id: str) -> int:
            return 42

    assert await _history_verifications(_Store(), "t") == {}  # ty: ignore[invalid-argument-type]


async def test_history_keeps_not_verified_when_the_turn_touched_files() -> None:
    from backend.services.agent_factory import _history_verifications

    edit_run = SimpleNamespace(
        run_id="r1",
        detail="Nenhum quality gate foi executado: a execução não foi verificada.",
        evidence=_not_run_evidence(),
        files_touched=["backend/main.py"],
    )

    class _Store:
        async def list_agent_runs(
            self, thread_id: str, *, limit: int = 50
        ) -> list[SimpleNamespace]:
            return [edit_run]

        async def latest_assistant_message_id(self, thread_id: str, run_id: str) -> int:
            return 42

    verifications = await _history_verifications(_Store(), "t")  # ty: ignore[invalid-argument-type]

    assert verifications["42"].outcome == "not_run"
    assert verifications["42"].status == "degraded"
