"""Cache explícito de contexto do Gemini (backend/llm/google/prompt_cache.py)."""

from __future__ import annotations

import pytest

from backend.llm.google import prompt_cache as pc
from backend.vtypes import VMessage
from backend.vtypes.contracts import JsonObject
from tests.json_values import dig


class _Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.created: list[dict] = []
        self.fail = fail

    async def create_cached_content(self, payload: dict) -> dict:
        if self.fail:
            raise RuntimeError("recusado")
        self.created.append(payload)
        return {"name": f"cachedContents/{len(self.created)}"}


def _large_payload() -> dict:
    return {
        "systemInstruction": {"parts": [{"text": "regra " * 4000}]},
        "tools": [{"functionDeclarations": []}],
        "contents": [{"role": "user", "parts": [{"text": "oi"}]}],
    }


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    pc.reset_state()


@pytest.mark.asyncio
async def test_large_prefix_becomes_cached_content_and_is_reused() -> None:
    client = _Client()
    body1, used1 = await pc.apply_cached_content(
        client, "gemini-2.5-pro", _large_payload()
    )
    body2, used2 = await pc.apply_cached_content(
        client, "gemini-2.5-pro", _large_payload()
    )

    assert used1 and used2
    assert len(client.created) == 1
    assert client.created[0]["model"] == "models/gemini-2.5-pro"
    assert body1["cachedContent"] == body2["cachedContent"] == "cachedContents/1"
    assert "systemInstruction" not in body1
    assert "tools" not in body1
    assert dig(body1, "contents", 0, "parts", 0, "text") == "oi"


@pytest.mark.asyncio
async def test_small_prefix_does_not_use_cache() -> None:
    client = _Client()
    payload: JsonObject = {
        "systemInstruction": {"parts": [{"text": "curto"}]},
        "contents": [],
    }

    body, used = await pc.apply_cached_content(client, "gemini-2.5-pro", payload)

    assert used is False
    assert body is payload
    assert client.created == []


@pytest.mark.asyncio
async def test_creation_failure_falls_back_and_does_not_retry_soon() -> None:
    client = _Client(fail=True)
    payload = _large_payload()

    body, used = await pc.apply_cached_content(client, "m", payload, now=0.0)
    assert used is False and body is payload

    ok = _Client()
    _, used = await pc.apply_cached_content(ok, "m", payload, now=10.0)
    assert used is False and ok.created == []
    _, used = await pc.apply_cached_content(ok, "m", payload, now=400.0)
    assert used is True


@pytest.mark.asyncio
async def test_expired_cache_is_recreated() -> None:
    client = _Client()
    await pc.apply_cached_content(client, "m", _large_payload(), now=0.0)
    await pc.apply_cached_content(client, "m", _large_payload(), now=4000.0)
    assert len(client.created) == 2


@pytest.mark.asyncio
async def test_disabled_in_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.settings import settings

    monkeypatch.setattr(settings, "prompt_cache_enabled", False)
    client = _Client()
    payload = _large_payload()

    body, used = await pc.apply_cached_content(client, "m", payload)

    assert used is False and body is payload and client.created == []


class _GeminiFake:
    """Client falso: recusa chamadas com cachedContent, aceita as sem."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create_cached_content(self, payload: dict) -> dict:
        return {"name": "cachedContents/9"}

    async def generate_content(self, model: str, payload: dict) -> dict:
        from backend.llm.google.client import GoogleGenAIResponseError

        self.calls.append(payload)
        if "cachedContent" in payload:
            raise GoogleGenAIResponseError("cache expirado")
        return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    async def stream_generate_content(self, model: str, payload: dict):
        from backend.llm.google.client import GoogleGenAIResponseError

        self.calls.append(payload)
        if "cachedContent" in payload:
            raise GoogleGenAIResponseError("cache expirado")
        yield {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}


def _large_messages() -> list[VMessage]:
    from backend.vtypes.message import MessageRole, text_message

    return [
        text_message(MessageRole.SYSTEM, "regra " * 4000),
        text_message(MessageRole.USER, "oi"),
    ]


@pytest.mark.asyncio
async def test_agenerate_retries_without_cache_when_api_rejects_it() -> None:
    from backend.llm.google.chat_client import GoogleChatClient

    fake = _GeminiFake()
    client = GoogleChatClient("gemini-2.5-pro", fake)  # ty: ignore[invalid-argument-type]

    response = await client.agenerate(_large_messages())

    assert response.text() == "ok"
    assert "cachedContent" in fake.calls[0]
    assert "cachedContent" not in fake.calls[1]
    assert "systemInstruction" in fake.calls[1]


@pytest.mark.asyncio
async def test_astream_retries_without_cache_when_api_rejects_it() -> None:
    from backend.llm.google.chat_client import GoogleChatClient

    fake = _GeminiFake()
    client = GoogleChatClient("gemini-2.5-pro", fake)  # ty: ignore[invalid-argument-type]

    texts = [
        chunk.delta_text
        async for chunk in client.astream(_large_messages())
        if chunk.delta_text
    ]

    assert texts == ["ok"]
    assert "cachedContent" in fake.calls[0]
    assert "cachedContent" not in fake.calls[1]
