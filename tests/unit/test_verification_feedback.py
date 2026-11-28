"""A falha da verificação de um turno volta ao modelo no turno seguinte."""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.engine.conversation_loop import (
    LoopConfig,
    _previous_verification_note,
    _with_verification_note,
    run_conversation,
)
from backend.persistence.native.agent_run import AgentRun
from backend.persistence.native.session_store import SessionStore
from backend.services.verification.engine import (
    FEEDBACK_MAX_CHARS,
    VerificationResult,
)
from backend.storage.sqlite.pool import AsyncConnectionPool
from backend.tools.context import ToolContext
from backend.tools.registry import ToolRegistry
from backend.vtypes.message import MessageRole, VMessageChunk, text_message


@pytest.fixture
async def session_store(tmp_path: Path):
    pool = AsyncConnectionPool(str(tmp_path / "feedback.db"), min_size=1, max_size=2)
    await pool.open()
    store = SessionStore(pool)
    await store.setup()
    await store.create_session("thread-f", user_id="alice")
    try:
        yield store
    finally:
        await pool.close()


async def _save_run(
    store: SessionStore, run_id: str, status: str, feedback: str
) -> None:
    await store.upsert_agent_run(
        AgentRun(
            run_id=run_id,
            thread_id="thread-f",
            user_id="alice",
            status=status,  # ty: ignore[invalid-argument-type]
            evidence=[
                {
                    "kind": "quality_gate_verification",
                    "status": status,
                    "failure_feedback": feedback,
                }
            ],
        )
    )


def test_evidence_entry_carries_a_capped_failure_feedback() -> None:
    result = VerificationResult(
        passed=False,
        status="failed",
        snapshot_sha256="abc",
        failure_feedback="x" * 10_000,
    )

    entry = result.to_evidence_entry()

    feedback = entry["failure_feedback"]
    assert isinstance(feedback, str)
    assert len(feedback) == FEEDBACK_MAX_CHARS


async def test_failed_previous_run_produces_a_note(session_store) -> None:
    await _save_run(session_store, "r1", "failed", "pytest: 2 testes falharam")

    note = await _previous_verification_note(session_store, "thread-f", "r2")

    assert note is not None
    assert "pytest: 2 testes falharam" in note
    assert "verificação do turno anterior falhou" in note


async def test_passed_previous_run_produces_no_note(session_store) -> None:
    await _save_run(session_store, "r1", "passed", "")

    assert await _previous_verification_note(session_store, "thread-f", "r2") is None


async def test_the_current_run_is_never_its_own_previous(session_store) -> None:
    await _save_run(session_store, "r1", "failed", "falhou")

    assert await _previous_verification_note(session_store, "thread-f", "r1") is None


def test_note_goes_after_the_leading_system_messages() -> None:
    history = [
        text_message(MessageRole.SYSTEM, "prompt"),
        text_message(MessageRole.USER, "oi"),
    ]

    out = _with_verification_note(history, "corrija")

    assert [m.text() for m in out] == ["prompt", "corrija", "oi"]
    assert _with_verification_note(history, None) is history


class _RecordingClient:
    def __init__(self) -> None:
        self.system_texts: list[str] = []

    async def astream(self, messages, *, tools=None, temperature=None, max_tokens=None):
        self.system_texts = [m.text() for m in messages if m.role is MessageRole.SYSTEM]
        yield VMessageChunk(delta_text="vou corrigir")

    async def agenerate(
        self, messages, *, tools=None, temperature=None, max_tokens=None
    ):
        raise NotImplementedError


async def test_next_turn_sends_the_failure_to_the_model(
    session_store, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _save_run(session_store, "r1", "failed", "ruff: 3 erros")
    await session_store.append_message(
        "thread-f", text_message(MessageRole.USER, "continue")
    )

    async def fake_verification(*_args, **_kwargs):
        return VerificationResult(
            passed=True, status="passed", snapshot_sha256="x", outcome="passed"
        )

    monkeypatch.setattr(
        "backend.services.verification.run_verification", fake_verification
    )
    client = _RecordingClient()

    await run_conversation(
        session_store=session_store,
        chat_client=client,
        tool_registry=ToolRegistry(),
        ctx=ToolContext(user_id="alice", thread_id="thread-f"),
        thread_id="thread-f",
        config=LoopConfig(run_id="r2"),
    )

    assert any("ruff: 3 erros" in text for text in client.system_texts)
