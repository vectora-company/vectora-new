"""Catálogo de Decision Models: cada modelo declara os providers que o servem."""

from types import TracebackType

import pytest

from backend.llm.systemone import catalog as catalog_module
from backend.llm.systemone.catalog import (
    DECISION_MODEL_CATALOG,
    decision_catalog,
    live_decision_catalog,
)


def test_todas_as_familias_pedidas_estao_no_catalogo() -> None:
    families = {entry.family for entry in DECISION_MODEL_CATALOG}
    assert {"jev", "clef", "laya", "gemini", "openai"} <= families


def test_jev_lista_api_oficial_e_openrouter() -> None:
    jev = next(e for e in DECISION_MODEL_CATALOG if e.id == "jev-latest")
    ids = {t.id: t for t in jev.transports}
    assert ids["typesafe-api"].supported
    assert ids["typesafe-api"].model_id == "jev-latest"
    assert ids["openrouter"].supported
    assert ids["openrouter"].provider == "openrouter-api"
    assert ids["openrouter"].model_id == "~typesafe/jev-latest"


def test_clef_aceita_api_local_e_roteadores() -> None:
    clef = next(e for e in DECISION_MODEL_CATALOG if e.family == "clef")
    ids = {t.id for t in clef.transports}
    assert {
        "cloudflare-workers-ai",
        "llamacpp",
        "9router",
        "omniroute",
        "openrouter",
    } <= ids


def test_apis_proprias_sem_adaptador_nao_sao_selecionaveis() -> None:
    gemini = next(e for e in DECISION_MODEL_CATALOG if e.family == "gemini")
    by_id = {t.id: t for t in gemini.transports}
    assert not by_id["google-api"].supported
    assert by_id["openrouter"].supported
    assert by_id["openrouter"].model_id == "google/gemini-decision-1"


def test_catalogo_inclui_modelos_so_do_openrouter() -> None:
    slugs = {
        t.model_id
        for e in DECISION_MODEL_CATALOG
        for t in e.transports
        if t.provider == "openrouter-api"
    }
    assert {
        "openai/gpt-6-luna-decisions",
        "perplexity/pplx-decider-v1.2",
        "liquid/d1.1",
        "nace-ai/drex-max",
        "cloudflare/clef-flash",
    } <= slugs


@pytest.mark.asyncio
async def test_lista_ao_vivo_acrescenta_modelos_novos_e_cai_no_estatico(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_module, "_live_cache", None)

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict:
            return {
                "data": [
                    {"id": "novo/decider-9", "name": "Novo: Decider 9"},
                    {"id": "liquid/d1", "name": "LiquidAI: d1"},
                    {"id": "~novo/latest", "name": "Novo: Latest"},
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

    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _Client)
    live = await live_decision_catalog()
    ids = [m.id for m in live.models]
    assert "novo/decider-9" in ids
    assert ids.count("liquid/d1") == 1
    assert "~novo/latest" not in ids
    novo = next(m for m in live.models if m.id == "novo/decider-9")
    assert novo.label == "Decider 9"
    assert novo.transports[0].provider == "openrouter-api"

    monkeypatch.setattr(catalog_module, "_live_cache", None)

    class _Broken(_Client):
        async def get(self, _url: str) -> _Resp:
            raise RuntimeError("sem rede")

    monkeypatch.setattr(catalog_module.httpx, "AsyncClient", _Broken)
    fallback = await live_decision_catalog()
    assert [m.id for m in fallback.models] == [m.id for m in DECISION_MODEL_CATALOG]


def test_todo_modelo_tem_ao_menos_um_provider() -> None:
    assert all(entry.transports for entry in decision_catalog().models)
