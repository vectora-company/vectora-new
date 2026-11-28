"""Janelas do sistema e canais locais usados pelos serviços de desktop."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Protocol, TypedDict, runtime_checkable

if TYPE_CHECKING:
    import asyncio


class NativeWindow(Protocol):
    """Janela devolvida pelo ``pygetwindow``."""

    @property
    def title(self) -> str: ...
    @property
    def left(self) -> int: ...
    @property
    def top(self) -> int: ...
    @property
    def width(self) -> int: ...
    @property
    def height(self) -> int: ...
    @property
    def isMinimized(self) -> bool: ...  # noqa: N802 — nome do pygetwindow

    def activate(self) -> None: ...


class WindowAdapter(Protocol):
    """Funções do ``pygetwindow`` usadas pelo registro de janelas."""

    def getAllWindows(self) -> list[NativeWindow]: ...  # noqa: N802

    def getActiveWindow(self) -> NativeWindow | None: ...  # noqa: N802


class WindowCapabilities(TypedDict):
    platform: str
    window_control: bool
    reason: str | None


class PipeServer(Protocol):
    def close(self) -> None: ...


@runtime_checkable
class PipeServingLoop(Protocol):
    """Parte do ``ProactorEventLoop`` que serve named pipes."""

    def start_serving_pipe(
        self, protocol_factory: Callable[[], asyncio.Protocol], address: str
    ) -> Awaitable[list[PipeServer]]: ...


class LlamacppStatus(TypedDict):
    """Estado do processo llama.cpp iniciado pela aplicação."""

    running: bool
    pid: int | None
    executable: str | None
    model: str | None
    host: str | None
    port: int | None
    persisted_pid: int | None
    stale_state: bool


@runtime_checkable
class AwaitablePipeServer(Protocol):
    """Servidor de pipe que permite esperar o fechamento."""

    def wait_closed(self) -> Awaitable[None]: ...


class LlamacppStartResult(TypedDict):
    """Modelo iniciado e confirmado pelo assistente de configuração."""

    running: bool
    executable: str
    model: str
    host: str
    port: int
    model_tag: str
    confirmed: bool
