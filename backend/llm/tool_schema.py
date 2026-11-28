"""Ferramentas como JSON no corpo das requisições aos provedores."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.tools.registry import ToolSpec
    from backend.vtypes.contracts import JsonObject


def chat_completions_tool(spec: ToolSpec) -> JsonObject:
    """Formato ``function`` usado pelas APIs compatíveis com Chat Completions."""
    function = spec.openai_schema()["function"]
    return {
        "type": "function",
        "function": {
            "name": function["name"],
            "description": function["description"],
            "parameters": function["parameters"],
        },
    }
