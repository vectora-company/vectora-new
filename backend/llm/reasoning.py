"""Esforço de raciocínio da execução corrente e o que cada provider aceita.

O loop publica o esforço escolhido na UI (``low``, ``medium``, ``high``,
``max``) e cada client o traduz para o parâmetro do seu provider. Sem esforço,
Anthropic fica sem thinking (custa tokens); OpenAI e Gemini pedem o resumo e
os pensamentos, que não alteram o custo do prompt.
"""

from __future__ import annotations

from contextvars import ContextVar

reasoning_effort: ContextVar[str | None] = ContextVar("reasoning_effort", default=None)

# Orçamento de tokens de thinking por esforço (Anthropic e Gemini).
THINKING_BUDGETS: dict[str, int] = {
    "low": 2048,
    "medium": 8192,
    "high": 16384,
    "max": 32768,
}
# A API da OpenAI só conhece estes três.
OPENAI_EFFORTS: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "high",
}
_OPENAI_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")
_GEMINI_THINKING_PREFIXES = ("gemini-2.5", "gemini-3")


from backend.vtypes.contracts import JsonObject


def thinking_budget(effort: str | None) -> int | None:
    """Orçamento de thinking para o esforço, ou ``None`` se não houver esforço."""
    return THINKING_BUDGETS.get(effort or "")


def openai_reasoning(model: str, effort: str | None) -> JsonObject | None:
    """Parâmetro ``reasoning`` da Responses API, só para modelos de raciocínio."""
    if not model.startswith(_OPENAI_REASONING_PREFIXES):
        return None
    params: JsonObject = {"summary": "auto"}
    if effort in OPENAI_EFFORTS:
        params["effort"] = OPENAI_EFFORTS[effort]
    return params


def gemini_thinking_config(model: str, effort: str | None) -> JsonObject | None:
    """``thinkingConfig`` do Gemini, só para os modelos que expõem pensamentos."""
    if not model.startswith(_GEMINI_THINKING_PREFIXES):
        return None
    config: JsonObject = {"includeThoughts": True}
    budget = thinking_budget(effort)
    if budget is not None:
        config["thinkingBudget"] = budget
    return config
