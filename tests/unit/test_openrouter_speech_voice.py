"""Voz padrão por modelo de fala do OpenRouter (cada modelo tem as suas vozes)."""

from types import TracebackType

import pytest

from backend.llm.openrouter import media


class _Resp:
    def raise_for_status(self) -> None: ...

    def json(self) -> dict:
        return {
            "data": [
                {
                    "id": "hexgrad/kokoro-82m",
                    "supported_voices": ["af_alloy", "af_bella"],
                },
                {"id": "google/gemini-3.8-flash-tts", "supported_voices": ["Zephyr"]},
            ]
        }


class _Client:
    def __init__[**P](self, *_a: P.args, **_k: P.kwargs) -> None: ...

    async def __aenter__(self) -> "_Client":
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None: ...

    async def get(self, _url: str) -> _Resp:
        return _Resp()


@pytest.mark.asyncio
async def test_usa_a_primeira_voz_declarada_pelo_modelo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media, "_speech_models_cache", None)
    monkeypatch.setattr(media.httpx, "AsyncClient", _Client)

    assert await media.default_speech_voice("hexgrad/kokoro-82m") == "af_alloy"
    assert await media.default_speech_voice("google/gemini-3.8-flash-tts") == "Zephyr"


@pytest.mark.asyncio
async def test_modelo_desconhecido_ou_sem_rede_cai_em_alloy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media, "_speech_models_cache", None)
    monkeypatch.setattr(media.httpx, "AsyncClient", _Client)
    assert await media.default_speech_voice("novo/modelo") == "alloy"

    class _Broken(_Client):
        async def get(self, _url: str) -> _Resp:
            raise RuntimeError("sem rede")

    monkeypatch.setattr(media, "_speech_models_cache", None)
    monkeypatch.setattr(media.httpx, "AsyncClient", _Broken)
    assert await media.default_speech_voice("hexgrad/kokoro-82m") == "alloy"


@pytest.mark.asyncio
async def test_lista_de_modelos_de_fala_traz_as_vozes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(media, "_speech_models_cache", None)
    monkeypatch.setattr(media.httpx, "AsyncClient", _Client)

    models = await media.list_speech_models()

    assert [m["id"] for m in models] == [
        "hexgrad/kokoro-82m",
        "google/gemini-3.8-flash-tts",
    ]
    assert models[0]["voices"] == ["af_alloy", "af_bella"]
