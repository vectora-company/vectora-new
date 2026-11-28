"""``GET /usage/providers`` — consumo real por provider configurado.

O produto expunha zero consumo: o medidor da appbar mostrava só a janela de
contexto da sessão. Os dados existem e são baratos — Tavily ``GET /usage``,
OpenRouter ``GET /credits`` e ``GET /key``.

Dois invariantes:

- Provider que falha aparece **com o erro**, nunca zerado. Um "0 crédito"
  falso é pior que um "não consegui consultar".
- A falha **não** é cacheada: cachear prenderia o usuário na mensagem de erro
  por minutos depois de a rede voltar.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.vtypes.api import ProviderUsage, ProviderUsageList
from backend.vtypes.contracts import JsonObject, JsonValue
from backend.vtypes.services import UsageInsight

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    type UsageQuery = Callable[[], Coroutine[None, None, ProviderUsage]]

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/usage", tags=["usage"])


class MediaQuotaResponse(BaseModel):
    period: str
    used: int
    limit: int
    remaining: int


def _require_user(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is None or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Não autenticado.")
    return str(user.id)


#: O popover abre a cada mensagem; sem cache seria uma chamada por abertura.
_CACHE_TTL_S = 180.0

_cache: dict[str, tuple[float, ProviderUsage]] = {}


def clear_usage_cache() -> None:
    _cache.clear()


def _num(valor: JsonValue) -> float | None:
    """Converte pra float, ou `None` quando o provider não mandou o campo.

    Diferente de `0.0`: ausência de dado não é consumo zero.
    """
    if not isinstance(valor, int | float | str):
        return None
    try:
        return float(valor)
    except ValueError:
        return None


def _menos(a: float | None, b: float | None) -> float | None:
    return a - b if a is not None and b is not None else None


def _entrada(
    provider: str,
    label: str,
    *,
    used: float | None = None,
    limit: float | None = None,
    remaining: float | None = None,
    plan: str | None = None,
    unit: str = "credits",
    error: str | None = None,
) -> ProviderUsage:
    return {
        "provider": provider,
        "label": label,
        "used": used,
        "limit": limit,
        "remaining": remaining,
        "plan": plan,
        "unit": unit,
        "error": error,
    }


async def _tavily_usage() -> ProviderUsage:
    from backend.settings import settings
    from backend.tools.tavily.client import TavilyClient

    client = TavilyClient(api_key=settings.tavily_api_key or "")
    try:
        bruto = await client.usage()
    finally:
        await client.aclose()

    key = bruto.get("key")
    conta = bruto.get("account")
    if not isinstance(key, dict):
        key = {}
    if not isinstance(conta, dict):
        conta = {}
    plano = conta.get("current_plan")
    usado = _num(key.get("usage"))
    teto = _num(key.get("limit"))
    return _entrada(
        "tavily",
        "Tavily",
        used=usado,
        limit=teto,
        remaining=_menos(teto, usado),
        plan=plano if isinstance(plano, str) else None,
    )


async def _openrouter_usage() -> ProviderUsage:
    from backend.llm.openrouter.client import OpenRouterClient
    from backend.settings import settings

    client = OpenRouterClient(api_key=settings.openrouter_api_key or "")
    try:
        # São dois endpoints: `/credits` tem o saldo, `/key` os limites da
        # chave. Mostrar só um deixa o número sem contexto.
        creditos = await client.get_json("/credits")
        chave = await client.get_json("/key")
    finally:
        await client.aclose()

    dados = _data_section(creditos)
    total = _num(dados.get("total_credits"))
    usado = _num(dados.get("total_usage"))
    limites = _data_section(chave)

    entrada = _entrada(
        "openrouter",
        "OpenRouter",
        used=usado,
        limit=total,
        remaining=_menos(total, usado),
        unit="usd",
    )
    # Limites da própria chave, distintos do saldo da conta: uma key pode ter
    # teto menor que o crédito disponível.
    entrada["key_limit"] = _num(limites.get("limit"))
    entrada["key_remaining"] = _num(limites.get("limit_remaining"))
    return entrada


def _data_section(payload: JsonObject) -> JsonObject:
    data = payload.get("data")
    return data if isinstance(data, dict) and data else payload


def _configurados() -> list[tuple[str, str, UsageQuery]]:
    from backend.settings import settings

    ativos: list[tuple[str, str, UsageQuery]] = []
    if (settings.tavily_api_key or "").strip():
        ativos.append(("tavily", "Tavily", _tavily_usage))
    if (settings.openrouter_api_key or "").strip():
        ativos.append(("openrouter", "OpenRouter", _openrouter_usage))
    return ativos


async def collect_provider_usage() -> list[ProviderUsage]:
    """Consulta em paralelo o consumo de cada provider configurado.

    Resultado bom vai pro cache; falha **não** vai — cachear prenderia o
    usuário na mensagem de erro mesmo depois de a rede voltar.
    """
    ativos = _configurados()
    if not ativos:
        return []

    agora = time.monotonic()
    saida: list[ProviderUsage] = []
    a_consultar: list[tuple[str, str, UsageQuery]] = []

    for nome, label, fn in ativos:
        em_cache = _cache.get(nome)
        if em_cache and agora - em_cache[0] < _CACHE_TTL_S:
            saida.append(em_cache[1])
        else:
            a_consultar.append((nome, label, fn))

    if not a_consultar:
        return saida

    resultados = await asyncio.gather(
        *(fn() for _n, _l, fn in a_consultar), return_exceptions=True
    )

    for (nome, label, _fn), resultado in zip(a_consultar, resultados, strict=True):
        if isinstance(resultado, BaseException):
            logger.warning(
                "usage: falha ao consultar consumo",
                extra={"provider": nome},
                exc_info=resultado,
            )
            saida.append(_entrada(nome, label, error=str(resultado)))
            continue
        _cache[nome] = (agora, resultado)
        saida.append(resultado)

    return saida


@router.get("/providers")
async def get_provider_usage() -> ProviderUsageList:
    """Consumo por provider — alimenta o medidor da appbar."""
    return {"providers": await collect_provider_usage()}


@router.get("/media", response_model=MediaQuotaResponse)
async def get_media_quota(
    response: Response,
    user_id: Annotated[str, Depends(_require_user)],
) -> MediaQuotaResponse:
    """Retorna a quota mensal de mídia do usuário autenticado."""
    from backend.services.media_quota import media_quota

    response.headers["Cache-Control"] = "no-store"
    return MediaQuotaResponse.model_validate(await media_quota.summary(user_id))


@router.get("/insights/weekly")
async def get_weekly_insight(
    request: Request, weeks: Annotated[int, Query()] = 1
) -> UsageInsight:
    """Retorna somente agregados técnicos da conta autenticada."""
    from backend.api.handlers.threads import _user_id
    from backend.services.usage_insights import get_usage_database, usage_insight_store
    from backend.workspace.runtime_settings import runtime_settings

    if weeks not in {1, 2, 4}:
        raise HTTPException(status_code=422, detail="weeks deve ser 1, 2 ou 4")
    start, end = usage_insight_store.window_bounds(weeks)
    user_id = _user_id(request)
    if (
        runtime_settings.get_frontend_prefs(user_id).get("weeklyInsightEnabled")
        is not True
    ):
        return {
            "window_weeks": weeks,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "event_count": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_cents": None,
            "unknown_cost_events": 0,
            "most_used_model": None,
            "tools": [],
        }

    db = await get_usage_database()
    return await usage_insight_store.aggregate(db, user_id=user_id, weeks=weeks)
