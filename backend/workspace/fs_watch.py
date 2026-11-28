"""Observer de arquivos que não vigia diretórios pesados.

O ``Observer`` padrão do watchdog registra um watch de inotify para cada
subdiretório do workspace, inclusive ``node_modules``, ``.git`` e pastas de
build. Num workspace grande (ou numa pasta que reúne vários repositórios) isso
estoura a fila do kernel, e o watchdog reconstrói todos os watches a cada
estouro. Aqui os diretórios ignorados nunca recebem watch, então os eventos
deles nem chegam ao processo.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver

logger = logging.getLogger(__name__)

#: ``.git`` fica de fora de propósito: HEAD, index e refs avisam o workbench Git
#: de commits, checkouts e stages feitos por fora. Só o conteúdo pesado do
#: ``.git`` é podado (ver ``IGNORED_RELATIVE_PREFIXES``).
IGNORED_DIR_NAMES: frozenset[str] = frozenset(
    {
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        ".tox",
        ".next",
        ".nuxt",
        ".turbo",
        ".cache",
        ".gradle",
        ".idea",
        "dist",
        "build",
        "target",
        "coverage",
    }
)

#: Subdiretórios do ``.git`` que só geram ruído (objetos, logs, módulos).
IGNORED_RELATIVE_PREFIXES: tuple[str, ...] = (
    ".git/objects",
    ".git/logs",
    ".git/lfs",
    ".git/modules",
)


def is_ignored_path(
    path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    root: str | bytes | os.PathLike[str] | os.PathLike[bytes] | None = None,
) -> bool:
    """Diz se algum componente do caminho é um diretório ignorado.

    Com ``root``, só os componentes abaixo dele contam: um workspace dentro de
    uma pasta chamada ``build`` continua observável.
    """
    text = os.fsdecode(path)
    if root is not None:
        base = os.fsdecode(root)
        with contextlib.suppress(ValueError):
            text = os.path.relpath(text, base)
        if text.startswith(".."):
            return False
    parts = Path(text).parts
    if any(part in IGNORED_DIR_NAMES for part in parts):
        return True
    relative = "/".join(parts)
    return any(
        relative == prefix or relative.startswith(prefix + "/")
        for prefix in IGNORED_RELATIVE_PREFIXES
    )


class _OverflowWarningThrottle(logging.Filter):
    """Deixa passar no máximo um aviso de overflow do inotify por minuto."""

    def __init__(self, interval_s: float = 60.0) -> None:
        super().__init__()
        self._interval_s = interval_s
        self._last = 0.0
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        if "overflow" not in record.getMessage().lower():
            return True
        with self._lock:
            now = time.monotonic()
            if now - self._last < self._interval_s:
                return False
            self._last = now
            return True


def install_watchdog_log_throttle() -> None:
    """Limita o spam de avisos de overflow dos loggers do watchdog."""
    throttle = _OverflowWarningThrottle()
    for name in ("watchdog.observers.inotify_c", "watchdog.observers.inotify"):
        target = logging.getLogger(name)
        if not any(isinstance(f, _OverflowWarningThrottle) for f in target.filters):
            target.addFilter(throttle)


def _build_pruned_observer() -> BaseObserver | None:
    """Observer Linux cujo inotify pula diretórios ignorados, ou ``None``."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        from watchdog.observers.api import BaseObserver
        from watchdog.observers.inotify import InotifyEmitter, InotifyWatchGroup
    except ImportError:
        return None

    class _PrunedWatchGroup(InotifyWatchGroup):  # type: ignore[misc]
        def _add_all_callbacks(self, path: bytes) -> None:
            self._add_callback(path)
            if not (Path(os.fsdecode(path)).is_dir() and self.is_recursive):
                return
            for root, dirnames, _ in os.walk(path, followlinks=self.follow_symlink):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if not is_ignored_path(
                        Path(os.fsdecode(root)) / os.fsdecode(d), os.fsdecode(path)
                    )
                ]
                for dirname in dirnames:
                    full_path = os.fsencode(
                        Path(os.fsdecode(root)) / os.fsdecode(dirname)
                    )
                    if (
                        not self.follow_symlink
                        and Path(os.fsdecode(full_path)).is_symlink()
                    ):
                        continue
                    self._add_callback(full_path)

        def _add_callback(self, path: bytes) -> None:
            if path != self.path and is_ignored_path(path, self.path):
                return
            super()._add_callback(path)

    class _PrunedEmitter(InotifyEmitter):  # type: ignore[misc]
        def on_thread_start(self) -> None:
            self._inotify = _PrunedWatchGroup(
                self._inotify_fd,
                os.fsencode(self.watch.path),
                is_recursive=self.watch.is_recursive,
                event_mask=self.get_event_mask_from_filter(),
                follow_symlink=self.watch.follow_symlink,
            )

    return BaseObserver(emitter_class=_PrunedEmitter)


def create_observer() -> BaseObserver:
    """Cria o observer do workspace, podado quando o watchdog permite."""
    install_watchdog_log_throttle()
    try:
        observer = _build_pruned_observer()
    except Exception:
        logger.warning("fs_watch: observer podado indisponível; usando o padrão")
        observer = None
    if observer is None:
        from watchdog.observers import Observer

        observer = Observer()
    return observer
