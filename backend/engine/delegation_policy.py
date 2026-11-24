"""Interpreta restrições explícitas do usuário sobre delegação no thread."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable

from backend.vtypes.message import MessageRole, VMessage

_SUBAGENT = r"(?:sub[\s-]?agents?|subagentes?)"
_DISABLE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"\b(?:nao|nunca)\s+(?:use|utilize|chame|delegue)\s+(?:mais\s+)?(?:nenhum(?:a)?\s+)?{_SUBAGENT}\b",
        rf"\bpare\s+de\s+(?:usar|chamar|delegar\s+para)\s+{_SUBAGENT}\b",
        rf"\bsem\s+(?:usar\s+)?{_SUBAGENT}\b",
        rf"\b(?:do\s+not|dont|never)\s+(?:use|call|delegate\s+to)\s+(?:any\s+)?{_SUBAGENT}\b",
        rf"\bno\s+{_SUBAGENT}\b",
    )
)
_ENABLE_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        rf"\b(?:pode|podem|permito|autorizo|volte\s+a)\s+(?:usar|chamar|delegar(?:\s+para)?)\s+{_SUBAGENT}\b",
        rf"\b(?:you\s+may|i\s+allow|allow)\s+(?:using\s+)?{_SUBAGENT}\b",
        rf"\b{_SUBAGENT}\s+(?:are\s+)?allowed\b",
    )
)


def _normalize(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def user_disabled_subagent_delegation(history: Iterable[VMessage]) -> bool:
    """Return the latest explicit user preference for subagent delegation.

    Only direct user messages can change this preference. Tool output and
    assistant text are untrusted evidence, not user authorization. A choice
    remains in effect for the thread until the user explicitly changes it.
    """
    disabled = False
    for message in history:
        if message.role is not MessageRole.USER:
            continue
        text = _normalize(message.model_text())
        if any(pattern.search(text) for pattern in _DISABLE_PATTERNS):
            disabled = True
        elif any(pattern.search(text) for pattern in _ENABLE_PATTERNS):
            disabled = False
    return disabled
