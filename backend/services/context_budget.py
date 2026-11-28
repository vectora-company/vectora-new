"""Orçamento de histórico derivado da janela real do modelo.

O limite fixo antigo (``settings.max_context_tokens``) valia igual para um
modelo de 8k e para um de 1M. Aqui o orçamento é uma fração da janela do
modelo, já descontado o schema das tools, que viaja em toda chamada mas não
faz parte do histórico que o planejador mede.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

# Fração da janela reservada ao histórico; o resto fica para schema de tools,
# prompt de sistema, resposta e a margem entre a contagem estimada e a real.
if TYPE_CHECKING:
    from backend.vtypes.services import ToolDefinitions

HISTORY_WINDOW_FRACTION = 0.5
# Teto: histórico de centenas de milhares de tokens encarece cada chamada e
# degrada a atenção do modelo muito antes de a janela acabar.
HISTORY_BUDGET_CAP = 200_000
_CHARS_PER_TOKEN = 4


def model_context_window(model: str | None) -> int | None:
    """Janela do modelo, tolerando ids com prefixo de roteador (``x/y/modelo``)."""
    if not model:
        return None
    from backend.settings import get_context_window

    name = model.strip()
    candidates = [name]
    if "/" in name:
        candidates.append(name.rsplit("/", 1)[1])
    for candidate in candidates:
        window = get_context_window(candidate)
        # 128k é o padrão para modelo desconhecido; só vale se o id for conhecido.
        if window != 128_000 or _is_known(candidate):
            return window
    return None


def _is_known(model: str) -> bool:
    from backend.settings import MODEL_CONTEXT_WINDOWS

    return model in MODEL_CONTEXT_WINDOWS


def estimate_tools_tokens(tools: ToolDefinitions) -> int:
    """Estimativa do custo em tokens do schema das tools enviadas."""
    total = 0
    for tool in tools:
        try:
            total += len(json.dumps(tool, default=str))
        except (TypeError, ValueError):
            total += len(str(tool))
    return total // _CHARS_PER_TOKEN


def resolve_history_budget(
    *,
    model: str | None,
    override: int | None,
    default: int,
    tools_tokens: int = 0,
) -> int:
    """Quantos tokens de histórico cabem nesta chamada.

    - ``override`` explícito do cliente vence sempre.
    - Modelo com janela conhecida: metade da janela menos o schema das tools,
      limitada por ``HISTORY_BUDGET_CAP`` e nunca abaixo de ``default``.
    - Modelo desconhecido: ``default``.
    """
    if override is not None:
        return override
    window = model_context_window(model)
    if window is None:
        return default
    budget = int(window * HISTORY_WINDOW_FRACTION) - tools_tokens
    return max(default, min(budget, HISTORY_BUDGET_CAP))


# Nunca abaixo de 1: Ollama e outros contam só os tokens avaliados (sem os que
# vieram do cache), então uma contagem menor que a estimativa não prova folga.
_MIN_CALIBRATION = 1.0
_MAX_CALIBRATION = 3.0


def calibration_ratio(real_prompt_tokens: int, estimated_prompt_tokens: int) -> float:
    """Quanto o provedor contou a mais (>1) ou a menos (<1) que a estimativa local.

    Só reduz o orçamento (nunca o amplia) e é limitada para uma chamada atípica
    (imagens) não distorcer o orçamento.
    """
    if real_prompt_tokens <= 0 or estimated_prompt_tokens <= 0:
        return 1.0
    ratio = real_prompt_tokens / estimated_prompt_tokens
    return max(_MIN_CALIBRATION, min(ratio, _MAX_CALIBRATION))


def calibrated_budget(budget: int, ratio: float, *, floor: int) -> int:
    """Orçamento em tokens estimados que equivale ao orçamento real pedido.

    Se o provedor conta 30% a mais que a estimativa local, o histórico precisa
    caber em ``budget / 1.3`` na régua local para caber em ``budget`` na real.
    """
    return max(floor, int(budget / ratio))
