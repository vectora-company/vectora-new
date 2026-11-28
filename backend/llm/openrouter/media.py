"""Imagem e TTS do OpenRouter — formatos de retorno **opostos**.

- ``POST /images`` → ``data[].b64_json``: base64 dentro de JSON, precisa ser
  decodificado antes de gravar.
- ``POST /audio/speech`` → bytestream binário: usar direto.

Por isso são funções separadas sobre métodos separados do cliente
(``post_json`` e ``post_bytes``). Unificar quebra um dos dois: a imagem vira
arquivo de base64 em texto, ou o áudio vira lixo decodificado.
"""

from __future__ import annotations

import base64
import binascii
import logging
import time
from typing import TypedDict

import httpx

from backend.llm.openrouter.client import OpenRouterClient, OpenRouterResponseError
from backend.vtypes.contracts import JsonObject

logger = logging.getLogger(__name__)


async def generate_image_bytes(
    client: OpenRouterClient,
    *,
    model: str,
    prompt: str,
    n: int | None = None,
    size: str | None = None,
    aspect_ratio: str | None = None,
    output_format: str | None = None,
    quality: str | None = None,
    seed: int | None = None,
) -> bytes:
    """Gera uma imagem e devolve os bytes já decodificados."""
    payload: JsonObject = {"model": model, "prompt": prompt}
    # Campo não pedido fica ausente, nunca `null` — parte dos providers
    # roteados rejeita o campo nulo com 400.
    opcionais = {
        "n": n,
        "size": size,
        "aspect_ratio": aspect_ratio,
        "output_format": output_format,
        "quality": quality,
        "seed": seed,
    }
    payload.update({k: v for k, v in opcionais.items() if v is not None})

    resposta = await client.post_json("/images", payload)
    dados = resposta.get("data")
    if not isinstance(dados, list) or not dados:
        msg = "OpenRouter devolveu `data` vazio em /images — nenhuma imagem gerada"
        raise OpenRouterResponseError(msg)

    b64 = dados[0].get("b64_json")
    if not b64:
        # Filtro do provider devolve `data` sem imagem. Sem este corte grava
        # arquivo de 0 byte, que aparece como artifact quebrado na UI.
        msg = (
            "OpenRouter devolveu item de /images sem `b64_json` — provável "
            "recusa do provider roteado"
        )
        raise OpenRouterResponseError(msg)

    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        msg = f"`b64_json` de /images não é base64 válido: {exc}"
        raise OpenRouterResponseError(msg) from exc


class SpeechModel(TypedDict):
    """Modelo de fala com as vozes que oferece."""

    id: str
    name: str
    voices: list[str]


_SPEECH_MODELS_URL = "https://openrouter.ai/api/v1/models?output_modalities=speech"
_FALLBACK_VOICE = "alloy"
_LIST_TTL_S = 3600.0
_speech_models_cache: tuple[float, list[SpeechModel]] | None = None


async def list_speech_models() -> list[SpeechModel]:
    """Modelos de fala do OpenRouter com as vozes de cada um (cache de 1 h).

    Devolve ``[]`` quando a listagem não está acessível.
    """
    global _speech_models_cache
    now = time.monotonic()
    if _speech_models_cache is not None and now - _speech_models_cache[0] < _LIST_TTL_S:
        return _speech_models_cache[1]
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            res = await http.get(_SPEECH_MODELS_URL)
        res.raise_for_status()
        models: list[SpeechModel] = [
            {
                "id": str(item["id"]),
                "name": str(item.get("name") or item["id"]),
                "voices": [str(v) for v in item.get("supported_voices") or []],
            }
            for item in res.json().get("data", [])
            if isinstance(item, dict) and item.get("id")
        ]
    except Exception:
        logger.info("openrouter: lista de modelos de fala indisponível", exc_info=True)
        return []
    _speech_models_cache = (now, models)
    return models


async def default_speech_voice(model: str) -> str:
    """Primeira voz que o OpenRouter declara para o modelo de fala.

    Cada modelo tem o próprio conjunto de vozes (``supported_voices``); usar
    uma voz de outro modelo dá 400. Sem informação, cai para ``alloy``.
    """
    for item in await list_speech_models():
        if item["id"] == model and item["voices"]:
            return str(item["voices"][0])
    return _FALLBACK_VOICE


async def synthesize_speech_bytes(
    client: OpenRouterClient,
    *,
    model: str,
    text: str,
    voice: str,
    response_format: str | None = None,
    speed: float | None = None,
) -> bytes:
    """Sintetiza voz e devolve o bytestream **cru**.

    Nada de `b64decode` aqui: o endpoint devolve o áudio binário direto.
    """
    payload: JsonObject = {"model": model, "input": text, "voice": voice}
    if response_format is not None:
        payload["response_format"] = response_format
    if speed is not None:
        payload["speed"] = speed

    return await client.post_bytes("/audio/speech", payload)
