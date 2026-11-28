"""Fakes compartilhados do motor nativo: cliente de chat, chunks e tools."""

from __future__ import annotations

from collections.abc import AsyncIterator

from backend.tools.registry import TOOL_REGISTRY, ToolSpec
from backend.vtypes.message import ToolCallChunk, VMessage, VMessageChunk


class ScriptedChatClient:
    """``ChatClient`` fake: cada ``astream`` consome o próximo turno do roteiro.

    Com ``exc``, toda chamada levanta a exceção dada em vez de produzir chunks
    (provider fora do ar). Com ``repeat``, o último turno se repete.
    """

    def __init__(
        self,
        turnos: list[list[VMessageChunk]] | None = None,
        *,
        exc: Exception | None = None,
        repeat: bool = False,
    ) -> None:
        self._turnos = turnos or []
        self._exc = exc
        self._repeat = repeat
        self.chamadas = 0

    async def astream(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[VMessageChunk]:
        self.chamadas += 1
        if self._exc is not None:
            raise self._exc
        indice = self.chamadas - 1
        if self._repeat:
            indice = min(indice, len(self._turnos) - 1)
        for chunk in self._turnos[indice]:
            yield chunk

    async def agenerate(
        self,
        messages: list[VMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> VMessage:
        msg = "não usado (astream-only)"
        raise NotImplementedError(msg)


def texto_chunk(texto: str) -> VMessageChunk:
    return VMessageChunk(delta_text=texto)


def tool_call_chunk(*, index: int, id: str, name: str, args: str) -> VMessageChunk:  # noqa: A002
    return VMessageChunk(
        tool_call_chunks=[
            ToolCallChunk(index=index, id=id, name=name, args_fragment=args)
        ]
    )


def require_spec(name: str) -> ToolSpec:
    """``TOOL_REGISTRY.get`` sem o ``None``: a tool já deve estar registrada."""
    spec = TOOL_REGISTRY.get(name)
    if spec is None:
        msg = f"tool '{name}' não registrada: setup do teste está errado"
        raise AssertionError(msg)
    return spec
