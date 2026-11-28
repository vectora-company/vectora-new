from __future__ import annotations

import asyncio
import json
import logging
import sys

import pytest

from backend.services import log_setup
from backend.services.log_setup import JSONFormatter, TextFormatter


def test_text_formatter_exposes_safe_tool_context_and_redacts_credentials() -> None:
    record = logging.LogRecord(
        "backend.tools.fs",
        logging.INFO,
        __file__,
        1,
        "terminal_command_executed",
        (),
        None,
    )
    record.command = "gh api --header 'Authorization: Bearer super-secret'"
    record.exit_code = 0
    record.unlisted_secret = "must not be emitted"

    rendered = TextFormatter().format(record)

    assert "command=\"gh api --header 'Authorization: Bearer [REDACTED]'\"" in rendered
    assert "exit_code=0" in rendered
    assert "super-secret" not in rendered
    assert "unlisted_secret" not in rendered


def test_json_formatter_emits_safe_context_without_raw_token() -> None:
    record = logging.LogRecord(
        "backend.tools.github",
        logging.INFO,
        __file__,
        1,
        "github_api_request_started",
        (),
        None,
    )
    record.endpoint = "https://api.github.com/repos/o/r?access_token=secret"
    record.repo = "r"
    record.authorization = "Bearer secret"

    payload = json.loads(JSONFormatter().format(record))

    assert (
        payload["endpoint"]
        == "https://api.github.com/repos/o/r?access_token=%5BREDACTED%5D"
    )
    assert payload["repo"] == "r"
    assert "authorization" not in payload
    assert "secret" not in json.dumps(payload)


def test_shutdown_filter_suprime_cancelled_error_so_apos_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filtro = log_setup._ShutdownCancellationFilter()
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "cancelled", (), None
    )
    try:
        raise asyncio.CancelledError
    except asyncio.CancelledError:
        record.exc_info = sys.exc_info()

    monkeypatch.setattr(log_setup, "_shutdown_requested", False)
    assert filtro.filter(record) is True

    log_setup.mark_shutdown_requested()
    assert filtro.filter(record) is False


def test_shutdown_filter_preserva_excecao_real(monkeypatch: pytest.MonkeyPatch) -> None:
    filtro = log_setup._ShutdownCancellationFilter()
    record = logging.LogRecord(
        "uvicorn.error", logging.ERROR, __file__, 1, "failed", (), None
    )
    try:
        raise RuntimeError("falha real")
    except RuntimeError:
        record.exc_info = sys.exc_info()

    monkeypatch.setattr(log_setup, "_shutdown_requested", True)
    assert filtro.filter(record) is True


def test_todo_caminho_de_shutdown_marca_o_encerramento() -> None:
    """Sem a marca o filtro imprime o traceback de CancelledError no shutdown."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "backend"
    for rel in ("main.py", "services/tray.py"):
        text = (root / rel).read_text(encoding="utf-8")
        assert text.count("mark_shutdown_requested()") >= text.count(
            "server.should_exit = True"
        ), rel


def test_shutdown_filter_suprime_o_aviso_de_prazo_do_uvicorn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filtro = log_setup._ShutdownCancellationFilter()
    record = logging.LogRecord(
        "uvicorn.error",
        logging.ERROR,
        __file__,
        1,
        "Cancel %s running task(s), timeout graceful shutdown exceeded",
        (2,),
        None,
    )

    monkeypatch.setattr(log_setup, "_shutdown_requested", False)
    assert filtro.filter(record) is True

    log_setup.mark_shutdown_requested()
    assert filtro.filter(record) is False
