"""Substituição das dependências do motor nativo nos testes."""

from __future__ import annotations

from types import ModuleType

import pytest

from backend.engine.hitl import ApprovalGate
from backend.engine.subagents import SubagentSpec
from backend.llm.base import ChatClient
from backend.persistence.native.session_store import SessionStore
from backend.services import agent_factory
from backend.services.agent_factory import NativeAgent
from backend.tools.registry import ToolRegistry
from tests.chat_fakes import ScriptedChatClient, texto_chunk


class RecordingChatClientFactory:
    """Substitui ``FallbackChatClient``: registra cada ``primary_model_id`` e
    devolve o próximo cliente da lista (o último quando a lista acaba)."""

    def __init__(self, clients: list[ChatClient]) -> None:
        self._clients = clients
        self.calls: list[str] = []

    def __call__(self, primary_model_id: str = "") -> ChatClient:
        self.calls.append(primary_model_id)
        indice = min(len(self.calls) - 1, len(self._clients) - 1)
        return self._clients[indice]


def patch_native_engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    module: ModuleType,
    session_store: SessionStore,
    tool_registry: ToolRegistry | None = None,
    subagent_catalog: dict[str, SubagentSpec] | None = None,
    system_prompt: str = "system prompt de teste",
    chat_client: ChatClient | list[ChatClient] | None = None,
) -> RecordingChatClientFactory:
    """Aponta ``agent_factory`` e o ``FallbackChatClient`` de ``module`` para
    o motor de teste."""
    native_agent = NativeAgent(
        tool_registry=tool_registry or ToolRegistry(),
        subagent_catalog=subagent_catalog or {},
        system_prompt=system_prompt,
    )
    approval_gate = ApprovalGate(session_store)

    async def get_native_agent(
        user_id: str | None = None,
        chat_mode: bool = False,
        workspace_id: str | None = None,
    ) -> NativeAgent:
        return native_agent

    async def get_session_store() -> SessionStore:
        return session_store

    async def get_approval_gate() -> ApprovalGate:
        return approval_gate

    async def get_store() -> None:
        return None

    monkeypatch.setattr(agent_factory, "get_native_agent", get_native_agent)
    monkeypatch.setattr(agent_factory, "get_session_store", get_session_store)
    monkeypatch.setattr(agent_factory, "get_approval_gate", get_approval_gate)
    monkeypatch.setattr(agent_factory, "get_store", get_store)

    if chat_client is None:
        chat_client = ScriptedChatClient([[texto_chunk("ok")]], repeat=True)
    clients = chat_client if isinstance(chat_client, list) else [chat_client]
    factory = RecordingChatClientFactory(clients)
    monkeypatch.setattr(module, "FallbackChatClient", factory)
    return factory
