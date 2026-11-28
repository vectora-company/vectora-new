"""Cache explícito de contexto do Gemini (``cachedContents``).

Prompt de sistema e schema de tools são idênticos a cada chamada do loop. Quando
passam do mínimo aceito pela API, viram um ``cachedContent`` reaproveitado pelas
chamadas seguintes (tokens em cache custam bem menos e não são reprocessados).
Qualquer falha cai no envio normal: o cache é só otimização.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Protocol

from backend.vtypes.contracts import JsonObject

logger = logging.getLogger(__name__)

# A API recusa prefixos pequenos (mínimo de 1.024 a 4.096 tokens conforme o
# modelo); o maior valor evita tentativas que a API rejeitaria.
MIN_PREFIX_TOKENS = 4096
_CHARS_PER_TOKEN = 4
CACHE_TTL_SECONDS = 3600
# Renova antes do vencimento real para uma chamada longa não usar um cache que
# expira no meio dela.
_REUSE_FOR_SECONDS = CACHE_TTL_SECONDS - 600
_RETRY_AFTER_FAILURE_SECONDS = 300
_MAX_ENTRIES = 64


class CacheClient(Protocol):
    """O que o cache precisa do client HTTP do Gemini."""

    async def create_cached_content(self, payload: dict) -> dict: ...


@dataclass(slots=True)
class _Entry:
    name: str | None
    expires_at: float


_entries: dict[str, _Entry] = {}


def reset_state() -> None:
    """Esquece os caches conhecidos (usado nos testes)."""
    _entries.clear()


def _prefix(payload: JsonObject) -> JsonObject:
    return {
        key: payload[key] for key in ("systemInstruction", "tools") if key in payload
    }


def _key(model: str, prefix: JsonObject) -> str:
    raw = json.dumps(prefix, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{model}\0{raw}".encode()).hexdigest()


def prefix_tokens(payload: JsonObject) -> int:
    """Estimativa do tamanho do prefixo cacheável (system + tools)."""
    raw = json.dumps(_prefix(payload), ensure_ascii=False, default=str)
    return len(raw) // _CHARS_PER_TOKEN


def invalidate(payload: JsonObject, model: str) -> None:
    _entries.pop(_key(model, _prefix(payload)), None)


async def apply_cached_content(
    client: CacheClient,
    model: str,
    payload: JsonObject,
    *,
    now: float | None = None,
) -> tuple[JsonObject, bool]:
    """Devolve ``(payload, usou_cache)``.

    Com cache, ``systemInstruction`` e ``tools`` saem do corpo e entra
    ``cachedContent``. Sem cache disponível, o corpo segue como veio.
    """
    from backend.settings import settings

    if not settings.prompt_cache_enabled:
        return payload, False
    prefix = _prefix(payload)
    if not prefix or prefix_tokens(payload) < MIN_PREFIX_TOKENS:
        return payload, False

    moment = time.monotonic() if now is None else now
    key = _key(model, prefix)
    entry = _entries.get(key)
    if entry is None or entry.expires_at <= moment:
        entry = await _create(client, model, prefix, key, moment)
    if entry.name is None:
        return payload, False

    body = {k: v for k, v in payload.items() if k not in prefix}
    body["cachedContent"] = entry.name
    return body, True


async def _create(
    client: CacheClient,
    model: str,
    prefix: JsonObject,
    key: str,
    moment: float,
) -> _Entry:
    try:
        created = await client.create_cached_content(
            {
                "model": f"models/{model}",
                "ttl": f"{CACHE_TTL_SECONDS}s",
                **prefix,
            }
        )
        name = created.get("name")
        entry = _Entry(
            name=str(name) if name else None,
            expires_at=moment
            + (_REUSE_FOR_SECONDS if name else _RETRY_AFTER_FAILURE_SECONDS),
        )
    except Exception:
        logger.warning(
            "gemini: não foi possível criar o cache de contexto; envio normal",
            exc_info=True,
        )
        entry = _Entry(name=None, expires_at=moment + _RETRY_AFTER_FAILURE_SECONDS)
    if len(_entries) >= _MAX_ENTRIES:
        _entries.pop(next(iter(_entries)))
    _entries[key] = entry
    return entry
