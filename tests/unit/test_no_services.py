"""O modo sem Services bloqueia integrações antes do acesso à rede."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.main import _build_parser


@pytest.mark.parametrize("command", ["start", "web"])
def test_cli_accepts_no_services(command: str) -> None:
    assert _build_parser().parse_args([command, "--no-services"]).no_services
    assert not _build_parser().parse_args([command]).no_services


@pytest.mark.asyncio
async def test_no_services_never_validates_or_overwrites_license(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import license

    monkeypatch.setenv("VECTORA_NO_SERVICES", "1")
    monkeypatch.delenv("VECTORA_LICENSE_BYPASS", raising=False)
    monkeypatch.setattr(license, "_read_cache", lambda: None)
    remote = Mock(side_effect=AssertionError("remote license request"))
    write = Mock(side_effect=AssertionError("cache mutation"))
    monkeypatch.setattr(license, "_validate_remote", remote)
    monkeypatch.setattr(license, "_write_cache", write)
    monkeypatch.setattr(license, "_get_token", lambda: "configured-token")
    result = await license.validate_license_async(force=True)
    assert result.tier == "free"
    remote.assert_not_called()
    write.assert_not_called()


def test_gateway_does_not_schedule_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.services.gateway import GatewayClient

    monkeypatch.setenv("VECTORA_NO_SERVICES", "1")
    client = GatewayClient("wss://example.invalid", token_path=tmp_path / "token")
    client.start()
    assert client._task is None


@pytest.mark.asyncio
async def test_hosted_catalogs_do_not_construct_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.services import registry_client

    monkeypatch.setenv("VECTORA_NO_SERVICES", "1")
    monkeypatch.setattr(registry_client, "_read_cache", lambda kind: None)
    monkeypatch.setattr(
        registry_client.httpx,
        "AsyncClient",
        Mock(side_effect=AssertionError("network")),
    )
    assert await registry_client.fetch_catalog("skills") == []
    assert (
        await registry_client.fetch_catalog_status("skills")
    ).status == "unavailable"


def test_oauth_broker_is_disabled_despite_configured_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from backend.api.handlers import oauth

    monkeypatch.setenv("VECTORA_NO_SERVICES", "1")
    monkeypatch.setenv("VECTORA_OAUTH_BROKER_URL", "https://example.invalid")
    monkeypatch.setenv("VECTORA_OAUTH_SECRET", "configured")
    assert not oauth._broker_enabled()
