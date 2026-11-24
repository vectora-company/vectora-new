"""Execução isolada do smoke test de uma instalação Electron do Vectora."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import signal
import subprocess  # nosec B404
import tempfile
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

RunStatus = Literal["passed", "failed", "blocked", "skipped", "not_run"]
_SENSITIVE_ENV = re.compile(
    r"(?:API[_-]?KEY|ACCESS[_-]?KEY|(?:^|_)KEY(?:_|$)|AUTH|"
    r"CLIENT[_-]?SECRET|CREDENTIAL|COOKIE|LICENSE[_-]?BYPASS|"
    r"PASS(?:WORD|PHRASE)|PRIVATE[_-]?KEY|SECRET|TOKEN)",
    re.IGNORECASE,
)
_WINDOW_READY_MARKER = "VECTORA_ELECTRON_WINDOW_READY"
_SMOKE_PASSED_MARKER = "Vectora startup smoke test passed; closing the app."


class EvaluationDiagnostic(BaseModel):
    """Resultado de uma etapa verificada durante o smoke test."""

    stage: str
    status: RunStatus
    duration_ms: int = Field(ge=0)
    detail: str | None = None


class InstalledSmokeReport(BaseModel):
    """Relatório estruturado de uma inicialização da instalação real."""

    schema_version: Literal[1] = 1
    run_id: str
    scenario_id: str = "installed-app-startup"
    status: RunStatus
    reason: str | None = None
    app_path: str
    started_at: str
    finished_at: str
    duration_ms: int = Field(ge=0)
    exit_code: int | None = None
    profile_isolated: bool = True
    workspace_isolated: bool = True
    host: dict[str, str] = Field(default_factory=dict)
    diagnostics: list[EvaluationDiagnostic] = Field(default_factory=list)


def _build_launch_command(app_path: Path) -> list[str]:
    """Build the argument vector for the installed app's startup scenario."""
    return [str(app_path), "--smoke-test"]


def _resolve_app_executable(candidate: Path) -> tuple[Path | None, str | None]:
    """Resolve a native executable or the executable inside a macOS app bundle."""
    if candidate.is_dir() and candidate.suffix == ".app":
        info_path = candidate / "Contents" / "Info.plist"
        try:
            with info_path.open("rb") as source:
                bundle = plistlib.load(source)
        except (OSError, plistlib.InvalidFileException):
            return None, "O bundle .app não contém um Info.plist válido."
        executable_name = bundle.get("CFBundleExecutable")
        if not isinstance(executable_name, str) or not executable_name:
            return None, "O Info.plist não informa CFBundleExecutable."
        executable = candidate / "Contents" / "MacOS" / executable_name
        if not executable.is_file():
            return None, "O executável do bundle .app não foi encontrado."
        return executable.resolve(), None
    if not candidate.is_file():
        return None, "O executável instalado não existe ou não é um arquivo."
    return candidate.resolve(), None


def _isolated_environment(profile: Path, workspace: Path) -> dict[str, str]:
    """Keep platform display/runtime variables while removing user credentials."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not _SENSITIVE_ENV.search(key)
    }
    home = profile / "home"
    vectora_home = profile / "vectora-home"
    data_home = profile / "xdg-data"
    config_home = profile / "xdg-config"
    cache_home = profile / "xdg-cache"
    for directory in (home, vectora_home, data_home, config_home, cache_home):
        directory.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "VECTORA_HOME": str(vectora_home),
            "VECTORA_RUNTIME_PROFILE": f"evaluation-{uuid4().hex}",
            "VECTORA_EVALUATION_WORKSPACE": str(workspace),
            "XDG_DATA_HOME": str(data_home),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_CACHE_HOME": str(cache_home),
            "TMPDIR": str(profile),
            "TMP": str(profile),
            "TEMP": str(profile),
            "APPDATA": str(profile / "roaming"),
            "LOCALAPPDATA": str(profile / "local"),
        }
    )
    for name in ("APPDATA", "LOCALAPPDATA"):
        Path(env[name]).mkdir(parents=True, exist_ok=True)
    return env


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Stop the app process and descendants after timeout or cancellation."""
    if os.name == "nt":
        if process.poll() is not None:
            return
        taskkill = shutil.which("taskkill")
        if taskkill:
            try:
                subprocess.run(  # noqa: S603  # nosec B603
                    [taskkill, "/PID", str(process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def _report(
    *,
    run_id: str,
    app_path: Path,
    started_at: datetime,
    started_ns: int,
    status: RunStatus,
    reason: str | None,
    exit_code: int | None,
) -> InstalledSmokeReport:
    """Create a terminal report without copying application logs or user data."""
    finished_ns = time.monotonic_ns()
    duration_ms = max(0, (finished_ns - started_ns) // 1_000_000)
    finished_at = datetime.now(UTC)
    return InstalledSmokeReport(
        run_id=run_id,
        status=status,
        reason=reason,
        app_path=str(app_path),
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_ms=duration_ms,
        exit_code=exit_code,
        host={"system": platform.system(), "machine": platform.machine() or "unknown"},
        diagnostics=[
            EvaluationDiagnostic(
                stage="installed_app_startup",
                status=status,
                duration_ms=duration_ms,
                detail=reason,
            )
        ],
    )


def run_installed_smoke(  # noqa: PLR0911
    app_path: str | Path,
    *,
    timeout_seconds: float = 120,
) -> InstalledSmokeReport:
    """Open the installed app in a disposable profile and validate its startup.

    The app's own ``--smoke-test`` path exits only after the backend and
    renderer have reached their ready marker. No provider is invoked and the
    user's checkout and application profile are never used.
    """
    run_id = uuid4().hex
    started_at = datetime.now(UTC)
    started_ns = time.monotonic_ns()
    candidate = Path(app_path).expanduser()
    if timeout_seconds <= 0:
        return _report(
            run_id=run_id,
            app_path=candidate,
            started_at=started_at,
            started_ns=started_ns,
            status="failed",
            reason="O limite de tempo precisa ser positivo.",
            exit_code=None,
        )
    app, resolution_error = _resolve_app_executable(candidate)
    if app is None:
        return _report(
            run_id=run_id,
            app_path=candidate,
            started_at=started_at,
            started_ns=started_ns,
            status="blocked",
            reason=resolution_error,
            exit_code=None,
        )

    try:
        temporary_directory = tempfile.TemporaryDirectory(prefix="vectora-eval-")
    except OSError as exc:
        return _report(
            run_id=run_id,
            app_path=app,
            started_at=started_at,
            started_ns=started_ns,
            status="failed",
            reason=f"Não foi possível criar o ambiente isolado: {exc.strerror or exc}",
            exit_code=None,
        )

    with temporary_directory as temporary:
        profile = Path(temporary) / "profile"
        workspace = Path(temporary) / "workspace"
        try:
            profile.mkdir()
            env = _isolated_environment(profile, workspace)
        except OSError as exc:
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="failed",
                reason=f"Não foi possível preparar o ambiente isolado: {exc.strerror or exc}",
                exit_code=None,
            )
        process: subprocess.Popen[str] | None = None
        reader: threading.Thread | None = None
        markers = {"window_ready": False, "passed": False}
        marker_lock = threading.Lock()
        try:
            process = subprocess.Popen(  # noqa: S603  # nosec B603
                _build_launch_command(app),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=env,
                cwd=workspace,
                start_new_session=os.name != "nt",
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                ),
            )
        except OSError as exc:
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="blocked",
                reason=f"Não foi possível iniciar o app instalado: {exc.strerror or exc}",
                exit_code=None,
            )

        def read_markers() -> None:
            if process is None or process.stdout is None:
                return
            for line in process.stdout:
                with marker_lock:
                    markers["window_ready"] |= _WINDOW_READY_MARKER in line
                    markers["passed"] |= _SMOKE_PASSED_MARKER in line

        reader = threading.Thread(target=read_markers, daemon=True)
        reader.start()
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="failed",
                reason=f"O app não concluiu o smoke test em {timeout_seconds:g} segundos.",
                exit_code=process.returncode,
            )
        except KeyboardInterrupt:
            _terminate_process_tree(process)
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="failed",
                reason="Execução cancelada pelo operador; os processos foram encerrados.",
                exit_code=process.returncode,
            )
        finally:
            if process.poll() is None:
                _terminate_process_tree(process)
            if reader is not None:
                reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()

        with marker_lock:
            startup_validated = markers["window_ready"] and markers["passed"]
        if exit_code != 0:
            status: RunStatus = "failed"
            reason = f"O app encerrou com código {exit_code}."
        elif not startup_validated:
            status = "failed"
            reason = "O app encerrou sem confirmar backend e renderer prontos."
        else:
            status = "passed"
            reason = None
        return _report(
            run_id=run_id,
            app_path=app,
            started_at=started_at,
            started_ns=started_ns,
            status=status,
            reason=reason,
            exit_code=exit_code,
        )
