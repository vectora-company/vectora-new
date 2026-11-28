"""Cache por fonte do catálogo: falha vale 15 s, resposta vale 10 minutos."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from backend.services import model_catalog

URL = "http://localhost:20128/api/v1/models"


@pytest.fixture(autouse=True)
def _limpa_cache() -> Generator[None]:
    model_catalog.invalidate_model_catalog()
    yield
    model_catalog.invalidate_model_catalog()


def _client(*responses: MagicMock | httpx.Response | Exception) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(side_effect=list(responses))
    return client


def _ok(payload: dict) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = payload
    return response


@pytest.mark.asyncio
async def test_fonte_que_respondeu_nao_e_reconsultada_dentro_do_ttl() -> None:
    client = _client(_ok({"data": [1]}), _ok({"data": [2]}))

    first = await model_catalog._get_json(client, URL)
    second = await model_catalog._get_json(client, URL)

    assert first == second == {"data": [1]}
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_fonte_que_falhou_e_reconsultada_depois_de_15s_e_passa_a_listar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(model_catalog.time, "monotonic", lambda: clock[0])
    client = _client(httpx.ConnectError("refused"), _ok({"data": ["omni"]}))

    assert await model_catalog._get_json(client, URL) is None
    # Dentro da janela de falha a fonte não é reconsultada.
    clock[0] += model_catalog._SOURCE_FAILURE_TTL_S - 1
    assert await model_catalog._get_json(client, URL) is None
    assert client.get.await_count == 1
    # Passada a janela, o provider que subiu depois aparece.
    clock[0] += 2
    assert await model_catalog._get_json(client, URL) == {"data": ["omni"]}
    assert client.get.await_count == 2


@pytest.mark.asyncio
async def test_fonte_que_respondeu_so_expira_depois_de_10_minutos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [1000.0]
    monkeypatch.setattr(model_catalog.time, "monotonic", lambda: clock[0])
    client = _client(_ok({"data": [1]}), _ok({"data": [2]}))

    await model_catalog._get_json(client, URL)
    clock[0] += model_catalog._SOURCE_OK_TTL_S - 1
    assert await model_catalog._get_json(client, URL) == {"data": [1]}
    clock[0] += 2
    assert await model_catalog._get_json(client, URL) == {"data": [2]}


@pytest.mark.asyncio
async def test_falha_gera_log_curto_sem_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client(httpx.ConnectError("refused"))

    with caplog.at_level("INFO", logger=model_catalog.logger.name):
        await model_catalog._get_json(client, URL)

    record = next(r for r in caplog.records if "unavailable" in r.getMessage())
    assert "ConnectError" in record.getMessage()
    assert record.exc_info is None


@pytest.mark.asyncio
async def test_http_status_falha_registra_codigo_sem_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = httpx.Response(
        403,
        request=httpx.Request("GET", URL),
        text="sensitive provider response",
    )
    client = _client(response)

    with caplog.at_level("WARNING", logger=model_catalog.logger.name):
        assert await model_catalog._get_json(client, URL) is None

    record = next(
        r for r in caplog.records if "provider endpoint returned" in r.getMessage()
    )
    assert record.levelname == "WARNING"
    assert "HTTP 403" in record.getMessage()
    assert URL in record.getMessage()
    assert "sensitive provider response" not in record.getMessage()
    assert record.exc_info is None


@pytest.mark.asyncio
async def test_chaves_diferentes_nao_compartilham_cache() -> None:
    client = _client(_ok({"data": ["a"]}), _ok({"data": ["b"]}))

    a = await model_catalog._get_json(
        client, URL, headers={"Authorization": "Bearer 1"}
    )
    b = await model_catalog._get_json(
        client, URL, headers={"Authorization": "Bearer 2"}
    )

    assert (a, b) == ({"data": ["a"]}, {"data": ["b"]})
