"""Contratos do pseudo-terminal e dos processos auxiliares."""

from __future__ import annotations

from typing import Literal, NotRequired, Protocol, TypedDict


class PtyProcess(Protocol):
    """Processo de PTY do ``ptyprocess`` ou do ``pywinpty``.

    A escrita recebe ``bytes`` no ``ptyprocess`` e ``str`` no ``pywinpty``.
    """

    def read(self, size: int, /) -> bytes | str: ...

    def write(self, data: bytes | str, /) -> int | None: ...

    def isalive(self) -> bool: ...

    def setwinsize(self, rows: int, cols: int, /) -> None: ...

    def terminate(self, force: bool = False) -> bool | None: ...


class PtyBackend(Protocol):
    """Classe que abre processos de PTY na plataforma."""

    def spawn(
        self,
        argv: list[str],
        *,
        dimensions: tuple[int, int],
        cwd: str | None,
        env: dict[str, str],
    ) -> PtyProcess: ...


class ScrollbackWindow(TypedDict):
    """Trecho do scrollback lido por cursor, sem consumir a saída."""

    data: bytes
    cursor: int
    alive: bool
    truncated: bool
    has_more: bool


class TerminalInputResult(TypedDict):
    """Resultado de uma escrita idempotente no terminal."""

    status: Literal["accepted", "duplicate", "error"]
    code: NotRequired[str]
    terminal_id: NotRequired[str]


class SidecarProcessOptions(TypedDict, total=False):
    """Opções de criação de processo auxiliar por plataforma."""

    creationflags: int
    start_new_session: bool
