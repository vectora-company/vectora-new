"""A descrição de um DSN em log ou terminal nunca expõe as credenciais."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.storage.dsn import describe_dsn


def test_describe_dsn_mantem_destino_sem_credenciais() -> None:
    described = describe_dsn(
        "postgresql://usuario:senha-secreta@db.exemplo.com:6543/vectora?password=x"
    )

    assert described == "db.exemplo.com:6543/vectora"


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        "senha-secreta",
        "postgresql://usuario:senha-secreta/resto@db.exemplo.com/vectora",
        "postgresql://usuario:12/senha-secreta@db.exemplo.com/vectora",
        "postgresql://usuario:senha-secreta#resto@db.exemplo.com/vectora",
    ],
)
def test_describe_dsn_ambiguo_nao_devolve_trechos(dsn: str) -> None:
    assert describe_dsn(dsn) == "<dsn não reconhecido>"


@pytest.mark.parametrize("recipe", ["neon", "supabase"])
def test_receita_registra_configuracao_sem_dados_do_dsn(
    recipe: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = MagicMock(spec=SimpleNamespace(storage_mode="", postgres_dsn=""))
    if recipe == "neon":
        from backend.storage.recipes.neon import configure_settings

        with caplog.at_level(logging.INFO):
            configure_settings(
                settings, host="db.exemplo.com", user="u", password="senha-secreta"
            )
    else:
        from backend.storage.recipes.supabase import configure_settings

        with caplog.at_level(logging.INFO):
            configure_settings(
                settings, host="db.exemplo.com", password="senha-secreta"
            )

    assert "storage_mode=complete" in caplog.text
    assert "senha-secreta" not in caplog.text
    assert "exemplo" not in caplog.text
