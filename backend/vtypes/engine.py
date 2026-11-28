"""Contratos do loop de conversa e dos subagentes."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, TypedDict

from backend.vtypes.contracts import JsonObject

if TYPE_CHECKING:
    from backend.tools.context import ToolContext
    from backend.vtypes.message import VMessage

type ApprovalPredicate = Callable[[str, ToolContext, JsonObject, list[VMessage]], bool]
"""Decide se uma chamada de ferramenta pausa o turno para aprovação."""


class ToolCallFragment(TypedDict):
    """Chamada de ferramenta ainda sendo recebida em fragmentos."""

    id: str | None
    name: str | None
    args_fragment: str


class NonToolReview(TypedDict):
    """Revisão pedida por um consumidor que não usa chamadas de ferramenta."""

    workspace_id: str
    consumer: str
    pattern_id: str
    decision_id: str
    state_version: str
    reason: str
    status: str


class ToolDecisionFields(TypedDict, total=False):
    """Decisão automática anexada ao início de uma ferramenta."""

    decision: str | None
    decision_reason: str | None
    decision_provider: str | None
    decision_model: str | None
    decision_confidence: float | None
