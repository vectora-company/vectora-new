"""Contratos do host de extensões."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from collections.abc import Collection

    from backend.tools.context import ToolContext
    from backend.vtypes.contracts import JsonObject


class ExtensionClient(Protocol):
    """Cliente MCP de uma extensão ativa."""

    async def connect(
        self, connections: dict[str, JsonObject], *, strict: bool = False
    ) -> None: ...

    def tools(self) -> Collection[str]: ...

    async def call_tool(
        self,
        tool_name: str,
        /,
        arguments: JsonObject,
        ctx: ToolContext | None = None,
    ) -> str: ...

    async def aclose(self) -> None: ...


class ExtensionHealth(TypedDict):
    state: str
    running: bool
    tools: list[str]
