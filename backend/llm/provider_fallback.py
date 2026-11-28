"""Detecção e classificação de erros dos providers de IA.

O fallback genérico de chat foi removido. O módulo mantém classificadores e
contratos usados por adapters específicos de capability, que reportam as
trocas quando seu próprio contrato permitir.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_QUOTA_MARKERS = ("429", "resource_exhausted", "quota", "rate limit", "rate_limit")
_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "connecttimeout",
    "readtimeout",
    "connection error",
    "connection refused",
    # Sobrecarga momentânea do provider (não é quota do usuário nem erro de
    # app) — confirmado com um 503 real do Gemini: "This model is currently
    # experiencing high demand. Spikes in demand are usually temporary."
    # Anthropic usa "overloaded_error" pro mesmo cenário.
    "503",
    "service unavailable",
    "overloaded",
    "high demand",
)
_PROVIDER_INCOMPATIBLE_MARKERS = (
    # Cohere Command A+ (e variantes): o client nativo sempre inclui
    # `tool_plan` ao serializar uma mensagem de assistente com tool_calls
    # no histórico — alguns deployments do modelo rejeitam esse campo com
    # 400. Nunca resolve no MESMO provider (retry idêntico falha sempre),
    # mas o próximo da cadeia de fallback processa a mesma mensagem sem
    # problema.
    "tool plan` cannot be used with this model",
    "tool_plan",
)
_MULTIMODAL_INCOMPATIBLE_MARKERS = (
    "image content is not supported",
    "image input is not supported",
    "image_url is not supported",
    "unsupported content type: image",
    "does not support image",
    "model does not support vision",
)


class QuotaExhaustedError(Exception):
    """A quota do modelo selecionado foi esgotada."""

    def __init__(
        self,
        message: str,
        *,
        model_id: str | None = None,
        provider: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.model_id = model_id
        self.provider = provider
        self.retry_after = retry_after


def is_quota_error(exc: BaseException) -> bool:
    """True se a exceção indica quota/rate-limit esgotada.

    Checa tipo antes de substring — exceções tipadas de provider (ex.
    `OpenRouterRateLimitError`) são reconhecidas mesmo se a mensagem exata
    mudar; o substring continua como rede de segurança pra exceções não
    tipadas de outros providers (Anthropic/Gemini/Cohere levantam erro
    genérico da SDK, não uma classe própria de rate limit).
    """
    if isinstance(exc, QuotaExhaustedError):
        return True

    from backend.llm.openrouter.client import OpenRouterRateLimitError

    if isinstance(exc, OpenRouterRateLimitError):
        return True

    msg = str(exc).lower()
    return any(marker in msg for marker in _QUOTA_MARKERS)


def is_transient_error(exc: BaseException) -> bool:
    """True se a exceção indica falha transiente (timeout, conexão) — vale tentar o próximo provider."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _TRANSIENT_MARKERS)


def is_provider_incompatible_error(exc: BaseException) -> bool:
    """True se o provider rejeitou a request por incompatibilidade permanente
    entre o histórico e o schema do modelo (ex.: Cohere recusando `tool_plan`).
    Não é transiente (o MESMO provider falha sempre com a mesma mensagem),
    mas ainda vale trocar pro próximo da cadeia — a mensagem em si é válida."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _PROVIDER_INCOMPATIBLE_MARKERS)


def is_multimodal_incompatible_error(exc: BaseException) -> bool:
    """Return true only for explicit image/vision incompatibility errors."""
    msg = str(exc).lower()
    if is_quota_error(exc) or is_transient_error(exc):
        return False
    return any(marker in msg for marker in _MULTIMODAL_INCOMPATIBLE_MARKERS)


_MEDIA_REJECTION_MARKERS = (
    "document",
    "pdf",
    "input_file",
    "file_data",
    "inline_data",
    "inlinedata",
    "audio",
    "video",
    "mime",
    "media type",
    "unsupported",
    "too large",
    "image",
)


def is_media_rejection_error(exc: BaseException) -> bool:
    """Erro do provider que pode ter sido causado por um arquivo anexado."""
    if is_quota_error(exc) or is_transient_error(exc):
        return False
    msg = str(exc).lower()
    return is_multimodal_incompatible_error(exc) or any(
        marker in msg for marker in _MEDIA_REJECTION_MARKERS
    )


def _provider_has_key(provider: str) -> bool:
    """True se o provider tem credencial configurada (ollama é sempre local).

    ``provider`` chega normalizado com underscore (mesma convenção de
    ``_provider_of``/``chat.py::_build_configurable``), então as chaves do
    keymap usam underscore também — "google-genai" (hífen) nunca bateria
    aqui e o Gemini nunca entraria como fallback de outro provider.
    """
    if provider == "ollama":
        return True
    from backend.settings import settings

    if provider == "nine_router":
        return bool(settings.nine_router_api_key and settings.nine_router_base_url)
    if provider == "dmr":
        return bool(settings.dmr_base_url)
    if provider == "llamacpp":
        # llama.cpp pode ser um sidecar gerenciado (com URL descoberta em
        # runtime) ou um servidor externo configurado pelo usuário. Ambos
        # são providers locais válidos para a cadeia de fallback.
        from backend.services.llamacpp_sidecar import llamacpp_status

        status = llamacpp_status()
        return bool(settings.llamacpp_base_url or status.get("running"))

    keymap = {
        "openai": settings.openai_api_key,
        "google_genai": settings.google_api_key,
        "anthropic": settings.anthropic_api_key,
        "cohere": settings.cohere_api_key,
        "openrouter": settings.openrouter_api_key,
    }
    return bool(keymap.get(provider))


def get_fallback_chain(current_model_id: str) -> list[str]:
    """A cadeia genérica de fallback de chat foi removida.

    Mantém o contrato interno para os adapters que ainda compartilham os
    detectores de erro, mas nunca troca de modelo de forma global ou silenciosa.
    """
    _ = current_model_id
    return []


def _provider_of(model_id: str) -> str:
    return model_id.split(":", 1)[0]


# ---------------------------------------------------------------------------
# Fila de trocas (drenada pelo handler de chat para notificar o frontend)
# ---------------------------------------------------------------------------

_PENDING_SWITCHES: ContextVar[list[dict[str, str]] | None] = ContextVar(
    "pending_switches", default=None
)


def record_switch(from_model: str, to_model: str) -> None:
    lst = _PENDING_SWITCHES.get()
    if lst is None:
        lst = []
        _PENDING_SWITCHES.set(lst)
    lst.append({"from": from_model, "to": to_model})


def drain_switches() -> list[dict[str, str]]:
    """Devolve as trocas pendentes e limpa a fila."""
    lst = _PENDING_SWITCHES.get()
    _PENDING_SWITCHES.set(None)
    return list(lst) if lst else []


async def emit_model_switch_event(from_model: str, to_model: str) -> None:
    """Registra a troca de embeddings/rerank na mesma fila (``record_switch``/
    ``drain_switches``); o handler de chat drena a fila e notifica o frontend.
    """
    record_switch(from_model, to_model)


async def try_with_fallback(  # noqa: UP047
    fn: Callable[[str], Awaitable[T]],
    model_id: str,
    *,
    on_switch: Callable[[str, str], None] | None = None,
) -> T:
    """Executa somente o modelo selecionado e preserva erro de quota tipado."""
    try:
        return await fn(model_id)
    except Exception as exc:
        if not is_quota_error(exc):
            raise
        raise QuotaExhaustedError(
            f"A quota do modelo selecionado ({model_id}) foi esgotada.",
            model_id=model_id,
            provider=_provider_of(model_id),
        ) from exc
