"""`--no-services`: as rotas que dependem do Services recusam antes de tocar a rede."""

from __future__ import annotations

import socket
from pathlib import Path

import aiohttp
import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.server import create_app


class _NetworkTouchedError(AssertionError):
    pass


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """App completo, sem Services e com qualquer saída de rede proibida."""
    monkeypatch.setenv("VECTORA_NO_SERVICES", "1")
    monkeypatch.setenv("VECTORA_AUTH_REQUIRED", "false")
    monkeypatch.setenv("VECTORA_HOME", str(tmp_path))
    monkeypatch.delenv("VECTORA_OAUTH_BROKER_URL", raising=False)

    def _proibido(*_args, **_kwargs):
        raise _NetworkTouchedError("acesso à rede com --no-services")

    monkeypatch.setattr(httpx.AsyncClient, "send", _proibido)
    monkeypatch.setattr(httpx.Client, "send", _proibido)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", _proibido)
    monkeypatch.setattr(socket.socket, "connect", _proibido)
    return TestClient(create_app(serve_static=False), raise_server_exceptions=False)


SEM_SERVICES = "Services desabilitado nesta execução"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/license/connect", {"email": "a@b.c", "password": "x"}),
        ("POST", "/license/validate-token", {"token": "vst_x"}),
        ("POST", "/license/oauth/init", None),
        ("GET", "/license/oauth/poll?state=abc", None),
        ("POST", "/license/portal", None),
        ("POST", "/gateway/revoke", None),
    ],
)
def test_rota_do_services_responde_503_sem_tocar_a_rede(
    client, method: str, path: str, body
) -> None:
    resposta = client.request(method, path, json=body)

    assert resposta.status_code == 503, resposta.text
    assert SEM_SERVICES in resposta.json()["detail"]


def test_status_do_gateway_diz_que_esta_desligado_pela_opcao(client) -> None:
    resposta = client.get("/gateway/status")

    assert resposta.status_code == 200
    assert resposta.json() == {
        "connected": False,
        "state": "disabled",
        "detail": "--no-services",
    }


def test_status_da_licenca_informa_que_o_services_esta_desligado(client) -> None:
    resposta = client.get("/license/status")

    assert resposta.status_code == 200
    assert resposta.json()["services_enabled"] is False


def test_validar_a_licenca_usa_so_o_cache_local_sem_rede(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VECTORA_TOKEN", "um-token-configurado")

    resposta = client.post("/license/validate")

    assert resposta.status_code == 200
    # Não levantou _NetworkTouchedError: nenhuma chamada remota foi tentada.
    assert resposta.json()["valid"] in {True, False}


def test_login_social_nao_consulta_o_broker_do_services(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from backend.api.handlers import oauth

    consultas: list[str] = []

    async def _providers():
        consultas.append("broker")
        return {"github"}

    monkeypatch.setattr(oauth, "_broker_providers", _providers)

    resposta = client.get("/auth/github", follow_redirects=False)

    # Sem Services não existe OAuth centralizado: o broker nunca é consultado;
    # sobra o OAuth local do provedor (redireciona) ou a recusa 503.
    assert consultas == []
    assert resposta.status_code in {302, 307, 503}


def test_com_services_ligado_o_status_informa_true(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VECTORA_NO_SERVICES", raising=False)
    monkeypatch.setenv("VECTORA_AUTH_REQUIRED", "false")
    monkeypatch.setenv("VECTORA_HOME", str(tmp_path))
    app_client = TestClient(create_app(serve_static=False))

    assert app_client.get("/license/status").json()["services_enabled"] is True
