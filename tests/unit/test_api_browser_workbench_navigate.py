"""Navegação do navegador remoto entregue pelo Chromium do Electron."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.api.handlers import browser_workbench


@pytest.mark.asyncio
async def test_navegacao_espera_a_url_mudar(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL anterior vai como argumento da função avaliada na página."""
    page = MagicMock(url="https://example.com/a")
    page.wait_for_function = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    shell = MagicMock(evaluate=AsyncMock(return_value={"ok": True}))
    request = MagicMock()
    request.state.user.id = "u1"
    monkeypatch.setattr(browser_workbench, "_page", AsyncMock(return_value=page))
    monkeypatch.setattr(browser_workbench, "_shell_page", AsyncMock(return_value=shell))
    monkeypatch.setattr(browser_workbench, "_state", AsyncMock(return_value={}))
    monkeypatch.setitem(browser_workbench._view_ids, ("u1", "tab"), 7)

    await browser_workbench.navigate(
        request,
        "tab",
        browser_workbench.BrowserNavigation(url="https://example.com/b"),
    )

    page.wait_for_function.assert_awaited_once_with(
        "previous => location.href !== previous",
        arg="https://example.com/a",
        timeout=10_000,
    )
