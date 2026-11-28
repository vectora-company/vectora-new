"""LazyLock + terminate_gracefully — padrões compartilhados entre
nats_sidecar.py e electron_sidecar.py (extraídos porque estavam
duplicados palavra-por-palavra nos dois módulos)."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Coroutine
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.subprocess_sidecar_utils import (
    LazyLock,
    _terminate_windows_tree,
    sidecar_process_options,
    terminate_gracefully,
)


def test_sidecar_process_options_isolates_windows_console_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.services.subprocess_sidecar_utils.sys.platform", "win32"
    )

    options = sidecar_process_options()

    assert options["creationflags"] > 0
    assert "start_new_session" not in options


def test_sidecar_process_options_isolates_a_posix_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.services.subprocess_sidecar_utils.sys.platform", "linux"
    )

    assert sidecar_process_options() == {"start_new_session": True}


class TestLazyLock:
    @pytest.mark.asyncio
    async def test_get_cria_o_lock_sob_demanda_e_reusa(self) -> None:
        lazy = LazyLock()
        lock1 = lazy.get()
        lock2 = lazy.get()
        assert lock1 is lock2

    def test_reset_solta_a_referencia(self) -> None:
        lazy = LazyLock()
        original = lazy.get()
        lazy.reset()
        assert lazy.get() is not original

    @pytest.mark.asyncio
    async def test_get_funciona_como_lock_de_verdade(self) -> None:
        lazy = LazyLock()
        async with lazy.get():
            assert lazy.get().locked() is True
        assert lazy.get().locked() is False


class TestTerminateGracefully:
    @pytest.mark.asyncio
    async def test_taskkill_tem_timeout_e_ainda_encerra_o_killer(self) -> None:
        killer = MagicMock()
        killer.wait = AsyncMock(return_value=None)
        logger = MagicMock()
        wait_for_timeouts: list[float] = []

        def _wait_for_with_timeout(
            awaitable: Coroutine[None, None, None], *, timeout: float
        ) -> Coroutine[None, None, None]:
            wait_for_timeouts.append(timeout)

            async def _consume() -> None:
                if len(wait_for_timeouts) == 1:
                    awaitable.close()
                    raise TimeoutError
                await awaitable

            return _consume()

        with (
            patch("backend.services.subprocess_sidecar_utils.sys.platform", "win32"),
            patch(
                "backend.services.subprocess_sidecar_utils.shutil.which",
                return_value="taskkill",
            ),
            patch(
                "backend.services.subprocess_sidecar_utils.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=killer),
            ),
            patch(
                "backend.services.subprocess_sidecar_utils.asyncio.wait_for",
                new=_wait_for_with_timeout,
            ),
        ):
            await _terminate_windows_tree(123, logger, "x", 0.01)

        assert wait_for_timeouts == [0.01, 0.01]
        killer.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_termina_gracioso_dentro_do_timeout(self) -> None:
        proc = MagicMock()
        proc.pid = 123
        proc.terminate = MagicMock()
        proc.wait = AsyncMock(return_value=None)
        logger = logging.getLogger("test")

        with (
            patch("backend.services.subprocess_sidecar_utils.sys.platform", "win32"),
            patch(
                "backend.services.subprocess_sidecar_utils._terminate_windows_tree",
                new=AsyncMock(),
            ),
        ):
            await terminate_gracefully(
                proc, timeout_seconds=5.0, logger=logger, log_prefix="x"
            )

        proc.terminate.assert_called_once()
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_chama_kill(self) -> None:
        # Erro/borda: processo não morre a tempo do shutdown gracioso —
        # kill() é chamado, sem propagar TimeoutError.
        proc = MagicMock()
        proc.pid = 123
        proc.terminate = MagicMock()

        async def _never_returns() -> None:
            await asyncio.sleep(10)

        proc.wait = AsyncMock(side_effect=_never_returns)
        logger = logging.getLogger("test")

        with (
            patch("backend.services.subprocess_sidecar_utils.sys.platform", "win32"),
            patch(
                "backend.services.subprocess_sidecar_utils._terminate_windows_tree",
                new=AsyncMock(),
            ),
        ):
            await terminate_gracefully(
                proc, timeout_seconds=0.01, logger=logger, log_prefix="x"
            )

        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_excecao_inesperada_e_logada_nao_propagada(self) -> None:
        proc = MagicMock()
        proc.pid = 123
        proc.terminate = MagicMock(side_effect=RuntimeError("boom"))
        logger = MagicMock()

        with (
            patch("backend.services.subprocess_sidecar_utils.sys.platform", "win32"),
            patch(
                "backend.services.subprocess_sidecar_utils._terminate_windows_tree",
                new=AsyncMock(),
            ),
        ):
            await terminate_gracefully(
                proc, timeout_seconds=5.0, logger=logger, log_prefix="x"
            )

        logger.warning.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_lookup_error_e_tratado_como_esperado_nao_warning(
        self,
    ) -> None:
        # Regressão: processo que já saiu sozinho antes do terminate() (comum
        # sob CancelledError encadeado do shutdown do lifespan) não deve virar
        # warning com traceback completo — é caso esperado, idempotente.
        proc = MagicMock()
        proc.pid = 123
        proc.terminate = MagicMock(side_effect=ProcessLookupError())
        logger = MagicMock()

        with (
            patch("backend.services.subprocess_sidecar_utils.sys.platform", "win32"),
            patch(
                "backend.services.subprocess_sidecar_utils._terminate_windows_tree",
                new=AsyncMock(),
            ),
        ):
            await terminate_gracefully(
                proc, timeout_seconds=5.0, logger=logger, log_prefix="x"
            )

        logger.debug.assert_called_once()
        logger.warning.assert_not_called()

    @pytest.mark.asyncio
    async def test_posix_shutdown_encerra_grupo_de_processos_do_sidecar(self) -> None:
        proc = MagicMock()
        proc.pid = 43210
        proc.wait = AsyncMock(return_value=None)
        logger = MagicMock()

        with (
            patch("backend.services.subprocess_sidecar_utils.sys.platform", "linux"),
            patch(
                "backend.services.subprocess_sidecar_utils._terminate_windows_tree",
                new=AsyncMock(),
            ),
            patch("backend.services.subprocess_sidecar_utils.os.killpg") as killpg,
            patch(
                "backend.services.subprocess_sidecar_utils._wait_posix_group_exit",
                new=AsyncMock(return_value=None),
            ),
        ):
            await terminate_gracefully(
                proc, timeout_seconds=5.0, logger=logger, log_prefix="electron"
            )

        killpg.assert_called_once_with(43210, signal.SIGTERM)
        proc.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_posix_shutdown_forca_grupo_que_nao_encerra(self) -> None:
        proc = MagicMock()
        proc.pid = 43210
        proc.wait = AsyncMock(return_value=None)
        logger = MagicMock()

        with (
            patch("backend.services.subprocess_sidecar_utils.sys.platform", "linux"),
            patch(
                "backend.services.subprocess_sidecar_utils._terminate_windows_tree",
                new=AsyncMock(),
            ),
            patch("backend.services.subprocess_sidecar_utils.os.killpg") as killpg,
            patch(
                "backend.services.subprocess_sidecar_utils._wait_posix_group_exit",
                new=AsyncMock(side_effect=TimeoutError),
            ),
        ):
            await terminate_gracefully(
                proc, timeout_seconds=5.0, logger=logger, log_prefix="electron"
            )

        assert [call.args for call in killpg.call_args_list] == [
            (43210, signal.SIGTERM),
            (43210, signal.SIGKILL),
        ]
