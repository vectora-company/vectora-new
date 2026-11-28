"""Raciocínio de ponta a ponta: providers, motor, persistência e SSE."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from backend.engine.conversation_loop import (
    LoopConfig,
    _strip_reasoning_blocks,
    run_conversation,
)
from backend.engine.stream_events import EngineEvent, ReasoningChunk
from backend.llm.anthropic.chat_client import AnthropicChatClient
from backend.llm.anthropic.client import AnthropicClient
from backend.llm.google.chat_client import (
    GoogleChatClient,
    _parse_candidate_parts,
    _thought_text,
)
from backend.llm.openai.chat_client import OpenAIChatClient
from backend.llm.openai.client import OpenAIClient
from backend.llm.reasoning import (
    gemini_thinking_config,
    openai_reasoning,
    reasoning_effort,
    thinking_budget,
)
from backend.persistence.native.session_store import SessionStore
from backend.storage.sqlite.pool import AsyncConnectionPool
from backend.tools.context import ToolContext
from backend.tools.registry import ToolRegistry, ToolSpec
from backend.vtypes.message import (
    ContentBlock,
    MessageRole,
    VMessage,
    VMessageChunk,
    text_message,
)


@pytest.fixture(autouse=True)
def _no_prompt_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.settings import settings

    monkeypatch.setattr(settings, "prompt_cache_enabled", False)


def test_effort_maps_to_provider_parameters() -> None:
    assert thinking_budget("medium") == 8192
    assert thinking_budget("") is None
    assert openai_reasoning("gpt-5", "max") == {"summary": "auto", "effort": "high"}
    assert openai_reasoning("gpt-4.1", "high") is None
    assert gemini_thinking_config("gemini-2.5-pro", "low") == {
        "includeThoughts": True,
        "thinkingBudget": 2048,
    }
    assert gemini_thinking_config("gemini-2.0-flash", "low") is None


def test_anthropic_payload_enables_thinking_only_with_effort() -> None:
    client = AnthropicChatClient(
        "claude-sonnet-4-5",
        AnthropicClient(api_key="k", http_client=httpx.AsyncClient()),
    )
    messages = [text_message(MessageRole.USER, "oi")]

    without_effort = client._payload(
        messages, tools=None, temperature=0.2, max_tokens=1000
    )
    token = reasoning_effort.set("medium")
    try:
        with_effort = client._payload(
            messages, tools=None, temperature=0.2, max_tokens=1000
        )
    finally:
        reasoning_effort.reset(token)

    assert "thinking" not in without_effort
    assert with_effort["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert with_effort["max_tokens"] >= 8192 + 4096
    assert "temperature" not in with_effort


async def test_anthropic_stream_emits_thinking_as_reasoning() -> None:
    body = (
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"thinking","thinking":""}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"thinking_delta","thinking":"vou somar"}}\n\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"signature_delta","signature":"abc"}}\n\n'
        b'data: {"type":"content_block_start","index":1,'
        b'"content_block":{"type":"text","text":""}}\n\n'
        b'data: {"type":"content_block_delta","index":1,'
        b'"delta":{"type":"text_delta","text":"4"}}\n\n'
        b'data: {"type":"message_stop"}\n\n'
    )
    client = AnthropicChatClient(
        "claude-sonnet-4-5",
        AnthropicClient(
            api_key="k",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(
                        200,
                        content=body,
                        headers={"content-type": "text/event-stream"},
                    )
                )
            ),
        ),
    )

    chunks = [c async for c in client.astream([text_message(MessageRole.USER, "2+2")])]

    assert [c.delta_reasoning for c in chunks if c.delta_reasoning] == ["vou somar"]
    assert [c.delta_text for c in chunks if c.delta_text] == ["4"]


async def test_openai_stream_emits_reasoning_summary() -> None:
    body = (
        b'data: {"type":"response.reasoning_summary_text.delta","delta":"pensando"}\n\n'
        b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        b'data: {"type":"response.completed","response":{"usage":{}}}\n\n'
    )
    client = OpenAIChatClient(
        "gpt-5",
        OpenAIClient(
            api_key="k",
            http_client=httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda _r: httpx.Response(
                        200,
                        content=body,
                        headers={"content-type": "text/event-stream"},
                    )
                )
            ),
        ),
    )

    chunks = [c async for c in client.astream([text_message(MessageRole.USER, "oi")])]

    assert [c.delta_reasoning for c in chunks if c.delta_reasoning] == ["pensando"]
    assert [c.delta_text for c in chunks if c.delta_text] == ["ok"]
    assert client._payload([], tools=None, temperature=None, max_tokens=None)[
        "reasoning"
    ] == {"summary": "auto"}


def test_gemini_separates_thoughts_from_answer() -> None:
    parts = [
        {"text": "primeiro penso", "thought": True},
        {"text": "resposta"},
    ]
    assert _thought_text(parts) == "primeiro penso"
    assert _parse_candidate_parts(parts)[0] == "resposta"


async def test_gemini_stream_emits_thought_chunk() -> None:
    class _Fake:
        async def stream_generate_content(self, model: str, payload: dict):
            assert payload["generationConfig"]["thinkingConfig"]["includeThoughts"]
            yield {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "analisando", "thought": True},
                                {"text": "feito"},
                            ]
                        }
                    }
                ]
            }

    client = GoogleChatClient("gemini-2.5-pro", _Fake())  # ty: ignore[invalid-argument-type]

    chunks = [c async for c in client.astream([text_message(MessageRole.USER, "oi")])]

    assert [c.delta_reasoning for c in chunks if c.delta_reasoning] == ["analisando"]
    assert [c.delta_text for c in chunks if c.delta_text] == ["feito"]


def test_reasoning_blocks_never_go_back_to_the_model() -> None:
    message = VMessage(
        role=MessageRole.ASSISTANT,
        content=[
            ContentBlock(kind="reasoning", reasoning_text="segredo do modelo"),
            ContentBlock(kind="text", text="resposta"),
        ],
    )

    cleaned = _strip_reasoning_blocks([message])

    assert [b.kind for b in cleaned[0].content] == ["text"]
    assert [b.kind for b in message.content] == ["reasoning", "text"]


def test_reasoning_chunk_becomes_sse_event() -> None:
    import json

    from backend.engine.sse_adapter import to_sse_line

    line = to_sse_line(ReasoningChunk(content="passo 1"))

    payload = json.loads(line.removeprefix("data: "))
    assert payload["type"] == "reasoning"
    assert payload["content"] == "passo 1"


class _ThinkingClient:
    async def agenerate(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> VMessage:
        raise NotImplementedError

    async def astream(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[VMessageChunk]:
        yield VMessageChunk(delta_reasoning="analiso ")
        yield VMessageChunk(delta_reasoning="o pedido")
        yield VMessageChunk(delta_text="pronto")


@pytest.fixture
async def session_store(tmp_path: Path) -> AsyncIterator[SessionStore]:
    pool = AsyncConnectionPool(str(tmp_path / "reasoning.db"), min_size=1, max_size=2)
    await pool.open()
    store = SessionStore(pool)
    await store.setup()
    await store.create_session("thread-r", user_id="alice")
    try:
        yield store
    finally:
        await pool.close()


async def test_loop_emits_and_persists_reasoning(session_store: SessionStore) -> None:
    ctx = ToolContext(user_id="alice", thread_id="thread-r")
    await session_store.append_message(
        "thread-r", text_message(MessageRole.USER, "ajude")
    )
    events: list[EngineEvent] = []

    async def on_event(event: EngineEvent) -> None:
        events.append(event)

    result = await run_conversation(
        session_store=session_store,
        chat_client=_ThinkingClient(),
        tool_registry=ToolRegistry(),
        ctx=ctx,
        thread_id="thread-r",
        config=LoopConfig(run_id="run-r"),
        on_event=on_event,
    )

    assert result.stopped_reason == "stop"
    assert [e.content for e in events if isinstance(e, ReasoningChunk)] == [
        "analiso ",
        "o pedido",
    ]
    history = await session_store.get_history("thread-r")
    assistant = history[-1]
    assert [b.kind for b in assistant.content] == ["reasoning", "text"]
    assert assistant.content[0].reasoning_text == "analiso o pedido"
    assert assistant.text() == "pronto"
