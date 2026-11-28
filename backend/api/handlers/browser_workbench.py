"""Browser visual remoto usando o Chromium empacotado pelo Electron.

O Vectora Web não tem a ponte IPC do renderer Electron. O backend usa o CDP
para acessar o renderer oculto do Electron, cria WebContentsViews pela ponte
IPC já existente e entrega frames ao browser que hospeda a SPA.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

router = APIRouter(prefix="/browser-workbench", tags=["browser-workbench"])
_sessions: dict[tuple[str, str], Page] = {}
_view_ids: dict[tuple[str, str], int] = {}
_viewports: dict[tuple[str, str], tuple[int, int]] = {}
_playwright: Playwright | None = None
_browser: Browser | None = None
_lock = asyncio.Lock()
_view_create_lock = asyncio.Lock()
if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright

_TAB_ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


from backend.vtypes.api import BrowserTabState


async def close_all() -> None:
    """Release pages and the CDP connection during backend shutdown."""
    global _playwright, _browser
    view_ids = tuple(_view_ids.items())
    _sessions.clear()
    _view_ids.clear()
    _viewports.clear()
    if view_ids and _browser is not None:
        try:
            shell = await _shell_page()
            for _key, view_id in view_ids:
                with suppress(Exception):
                    await shell.evaluate(
                        "viewId => window.vectora.browserView.destroyView(viewId)",
                        view_id,
                    )
        except Exception:
            pass
    if _playwright is not None:
        with suppress(Exception):
            await _playwright.stop()
    _playwright = None
    _browser = None


def _owner(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is None or not getattr(user, "id", None):
        raise HTTPException(status_code=401, detail="Não autenticado")
    return str(user.id)


def _validate_tab_id(tab_id: str) -> None:
    if not _TAB_ID.fullmatch(tab_id):
        raise HTTPException(status_code=400, detail="Identificador de aba inválido")


async def _get_browser() -> Browser:
    global _playwright, _browser
    async with _lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        from playwright.async_api import async_playwright

        from backend.browser.cdp import electron_cdp_endpoint, electron_cdp_headers

        if _playwright is not None:
            await _playwright.stop()
        _playwright = await async_playwright().start()
        endpoint = electron_cdp_endpoint()
        headers = electron_cdp_headers()
        _browser = await _playwright.chromium.connect_over_cdp(
            endpoint, headers=headers, timeout=10_000
        )
        return _browser


async def _shell_page() -> Page:
    browser = await _get_browser()
    if not browser.contexts:
        raise RuntimeError("O Chromium do Electron não expôs um contexto")
    for page in browser.contexts[0].pages:
        if page.url.startswith("vectora-app://app/"):
            return page
    raise RuntimeError("O renderer IPC do Electron não está disponível")


async def _page(
    request: Request,
    tab_id: str,
    *,
    create: bool = True,
    profile_id: str = "default",
) -> Page:
    _validate_tab_id(tab_id)
    key = (_owner(request), tab_id)
    page = _sessions.get(key)
    if page is not None and not page.is_closed():
        return page
    if not create:
        raise HTTPException(status_code=404, detail="Aba do navegador não encontrada")
    try:
        async with _view_create_lock:
            browser = await _get_browser()
            if not browser.contexts:
                raise RuntimeError("O Chromium do Electron não expôs um contexto")
            shell = await _shell_page()
            context = browser.contexts[0]
            before = set(context.pages)
            view_id = await shell.evaluate(
                "options => window.vectora.browserView.createView(options)",
                {"profileId": profile_id, "kind": "tab", "zoomPercent": 100},
            )
            page = None
            for _ in range(50):
                page = next(
                    (
                        candidate
                        for candidate in context.pages
                        if candidate not in before
                    ),
                    None,
                )
                if page is not None:
                    break
                await asyncio.sleep(0.1)
            if page is None:
                await shell.evaluate(
                    "viewId => window.vectora.browserView.destroyView(viewId)",
                    view_id,
                )
                raise RuntimeError("O Chromium não publicou a nova aba via CDP")
            await shell.evaluate(
                "({viewId, bounds}) => { window.vectora.browserView.setBounds(viewId, bounds); window.vectora.browserView.setVisible(viewId, true); }",
                {
                    "viewId": view_id,
                    "bounds": {"x": 0, "y": 0, "width": 1280, "height": 800},
                },
            )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="O Chromium do Electron não está disponível para o Vectora Web",
        ) from exc
    _sessions[key] = page
    _view_ids[key] = view_id
    _viewports[key] = (1280, 800)
    return page


class BrowserInput(BaseModel):
    kind: str
    x: float | None = Field(default=None, ge=0, le=1)
    y: float | None = Field(default=None, ge=0, le=1)
    delta_x: float = 0
    delta_y: float = 0
    text: str | None = Field(default=None, max_length=20_000)
    key: str | None = Field(default=None, max_length=40)


class BrowserNavigation(BaseModel):
    url: str = Field(max_length=4096)


class BrowserOpen(BaseModel):
    profile_id: str = Field(
        default="default",
        max_length=256,
        pattern=r"^(?:default|session-[A-Za-z0-9_-]+)$",
    )


class BrowserViewport(BaseModel):
    width: int = Field(ge=320, le=3840)
    height: int = Field(ge=240, le=2160)


@router.post("/{tab_id}/open")
async def open_tab(
    request: Request, tab_id: str, body: BrowserOpen | None = None
) -> BrowserTabState:
    page = await _page(
        request, tab_id, profile_id=body.profile_id if body else "default"
    )
    return await _state(page)


@router.get("/{tab_id}/state")
async def get_state(request: Request, tab_id: str) -> BrowserTabState:
    return await _state(await _page(request, tab_id, create=False))


async def _state(page: Page) -> BrowserTabState:
    can_go_back = False
    can_go_forward = False
    try:
        cdp = await page.context.new_cdp_session(page)
        history = await cdp.send("Page.getNavigationHistory")
        current = history["currentIndex"]
        entries = history["entries"]
        can_go_back = current > 0
        can_go_forward = current < len(entries) - 1
        await cdp.detach()
    except Exception:
        pass
    return {
        "url": page.url,
        "title": await page.title(),
        "loading": False,
        "can_go_back": can_go_back,
        "can_go_forward": can_go_forward,
    }


@router.get("/{tab_id}/frame")
async def get_frame(request: Request, tab_id: str) -> Response:
    page = await _page(request, tab_id, create=False)
    try:
        image = await page.screenshot(type="jpeg", quality=72, timeout=5_000)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Não foi possível capturar a página"
        ) from exc
    return Response(
        content=image, media_type="image/jpeg", headers={"Cache-Control": "no-store"}
    )


@router.post("/{tab_id}/navigate")
async def navigate(
    request: Request, tab_id: str, body: BrowserNavigation
) -> BrowserTabState:
    url = body.url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(
            status_code=400, detail="Somente URLs HTTP e HTTPS são aceitas"
        )
    page = await _page(request, tab_id)
    previous_url = page.url
    try:
        shell = await _shell_page()
        result = await shell.evaluate(
            "({viewId, url}) => window.vectora.browserView.navigate(viewId, url)",
            {"viewId": _view_ids[(_owner(request), tab_id)], "url": url},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Falha ao navegar no Chromium"
        ) from exc
    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail=result.get("error", "O Chromium recusou a navegação"),
        )
    if previous_url != url:
        try:
            await page.wait_for_function(
                "previous => location.href !== previous",
                arg=previous_url,
                timeout=10_000,
            )
        except Exception as exc:
            if page.url == previous_url:
                raise HTTPException(
                    status_code=502, detail="A navegação não chegou ao Chromium"
                ) from exc
    # A página pode continuar carregando recursos lentos; o frame acompanha
    # o documento assim que o URL muda.
    with suppress(Exception):
        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    return await _state(page)


@router.post("/{tab_id}/viewport")
async def set_viewport(
    request: Request, tab_id: str, body: BrowserViewport
) -> dict[str, bool]:
    page = await _page(request, tab_id, create=False)
    shell = await _shell_page()
    await shell.evaluate(
        "({viewId, bounds}) => window.vectora.browserView.setBounds(viewId, bounds)",
        {
            "viewId": _view_ids[(_owner(request), tab_id)],
            "bounds": {"x": 0, "y": 0, "width": body.width, "height": body.height},
        },
    )
    _viewports[(_owner(request), tab_id)] = (body.width, body.height)
    return {"ok": True}


@router.post("/{tab_id}/input")
async def send_input(
    request: Request, tab_id: str, body: BrowserInput
) -> dict[str, bool]:
    page = await _page(request, tab_id, create=False)
    viewport = _viewports.get((_owner(request), tab_id), (1280, 800))
    try:
        if body.kind == "click" and body.x is not None and body.y is not None:
            await page.mouse.click(body.x * viewport[0], body.y * viewport[1])
        elif body.kind == "move" and body.x is not None and body.y is not None:
            await page.mouse.move(body.x * viewport[0], body.y * viewport[1])
        elif body.kind == "wheel":
            await page.mouse.wheel(body.delta_x, body.delta_y)
        elif body.kind == "text" and body.text is not None:
            await page.keyboard.insert_text(body.text)
        elif body.kind == "key" and body.key:
            await page.keyboard.press(body.key)
        else:
            raise HTTPException(status_code=400, detail="Entrada do navegador inválida")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail="Falha ao encaminhar entrada ao Chromium"
        ) from exc
    return {"ok": True}


@router.post("/{tab_id}/history/{direction}")
async def history(request: Request, tab_id: str, direction: str) -> BrowserTabState:
    if direction not in {"back", "forward", "reload", "stop"}:
        raise HTTPException(status_code=400, detail="Ação de histórico inválida")
    page = await _page(request, tab_id, create=False)
    try:
        shell = await _shell_page()
        view_id = _view_ids[(_owner(request), tab_id)]
        method = {
            "back": "goBack",
            "forward": "goForward",
            "reload": "reload",
            "stop": "stop",
        }[direction]
        await shell.evaluate(
            "({viewId, method}) => window.vectora.browserView[method](viewId)",
            {"viewId": view_id, "method": method},
        )
    except Exception:
        pass
    return await _state(page)


@router.delete("/{tab_id}")
async def close_tab(request: Request, tab_id: str) -> dict[str, bool]:
    _validate_tab_id(tab_id)
    key = (_owner(request), tab_id)
    _sessions.pop(key, None)
    view_id = _view_ids.pop(key, None)
    _viewports.pop(key, None)
    if view_id is not None:
        try:
            shell = await _shell_page()
            await shell.evaluate(
                "viewId => window.vectora.browserView.destroyView(viewId)", view_id
            )
        except Exception:
            pass
    return {"ok": True}
