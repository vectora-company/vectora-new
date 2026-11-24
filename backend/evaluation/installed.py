"""Execução isolada do smoke test de uma instalação Electron do Vectora."""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import signal
import socket
import statistics
import subprocess  # nosec B404
import tempfile
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast
from uuid import uuid4

import psutil
from pydantic import BaseModel, Field

from backend.evaluation.metrics import BenchmarkMetric, RunProvenance
from backend.evaluation.resources import ProcessTreeResourceSampler

RunStatus = Literal["passed", "failed", "blocked", "skipped", "not_run"]
_SENSITIVE_ENV = re.compile(
    r"(?:API[_-]?KEY|ACCESS[_-]?KEY|(?:^|_)KEY(?:_|$)|AUTH|"
    r"CLIENT[_-]?SECRET|CREDENTIAL|COOKIE|LICENSE[_-]?BYPASS|"
    r"PASS(?:WORD|PHRASE)|PRIVATE[_-]?KEY|SECRET|TOKEN)",
    re.IGNORECASE,
)
_WINDOW_READY_MARKER = "VECTORA_ELECTRON_WINDOW_READY"
_SMOKE_PASSED_MARKER = "Vectora startup smoke test passed; closing the app."
_STAGE_MARKER = re.compile(r"^VECTORA_EVAL_STAGE:([a-z_]+):([0-9]+(?:\.[0-9]+)?)$")
_APP_METADATA_MARKER = re.compile(
    r"^VECTORA_EVAL_APP:([^:\s]+):([^:\s]+):([^:\s]+)"
    r"(?::([0-9a-f]{7,64}|unknown):(clean|dirty|unknown))?$"
)
_BACKEND_STAGE_MARKER = re.compile(
    r"VECTORA_EVAL_BACKEND_STAGE:([0-9a-f]{32}):([a-z_]+):"
    r"([0-9]+(?:\.[0-9]+)?):(ready|failed|skipped)"
)
_EXPECTED_STAGES = (
    "electron_ready",
    "backend_started",
    "backend_ready",
    "renderer_dom_ready",
    "renderer_loaded",
    "window_ready",
    "shutdown_complete",
)


class EvaluationDiagnostic(BaseModel):
    """Resultado de uma etapa verificada durante o smoke test."""

    stage: str
    status: RunStatus
    duration_ms: int = Field(ge=0)
    detail: str | None = None


class BackendStageRecord(BaseModel):
    """Estado e tempo monotônico reportados por uma etapa do backend."""

    status: Literal["ready", "failed", "skipped"]
    elapsed_ms: float = Field(ge=0)


class InstalledSmokeReport(BaseModel):
    """Relatório de inicialização live, sem alegar teste de provider ou tarefa."""

    schema_version: Literal[1] = 1
    evidence_level: Literal["installed_app"] = "installed_app"
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
    real_provider_tested: Literal[False] = False
    scenario_task_executed: Literal[False] = False
    renderer_tested: bool = False
    renderer_status: RunStatus = "not_run"
    renderer_ready_state: str | None = None
    renderer_root_children: int | None = Field(default=None, ge=0)
    renderer_route_mounted: bool | None = None
    renderer_titlebar_mounted: bool | None = None
    renderer_reload_status: RunStatus = "not_run"
    renderer_reload_duration_ms: float | None = Field(default=None, ge=0)
    host: dict[str, str] = Field(default_factory=dict)
    app_version: str | None = None
    electron_version: str | None = None
    chromium_version: str | None = None
    source_revision: str | None = None
    source_dirty: bool | None = None
    stage_timings_ms: dict[str, float] = Field(default_factory=dict)
    backend_stages: dict[str, BackendStageRecord] = Field(default_factory=dict)
    provenance: RunProvenance
    metrics: list[BenchmarkMetric] = Field(default_factory=list)
    diagnostics: list[EvaluationDiagnostic] = Field(default_factory=list)


class InstalledSmokeDistribution(BaseModel):
    """Distribuição de medições válidas em uma série do app instalado."""

    stage: str
    name: str
    dimension: str
    unit: str
    values: list[float]
    sample_count: int = Field(ge=0)
    attempted_count: int = Field(ge=1)
    mean: float | None = None
    standard_deviation: float | None = None
    p50: float | None = None
    p95: float | None = None
    confidence_95_low: float | None = None
    confidence_95_high: float | None = None


class InstalledSmokeSeriesReport(BaseModel):
    """Repetições mantêm cada resultado e resumem somente medições válidas."""

    schema_version: Literal[1] = 1
    evidence_level: Literal["installed_app"] = "installed_app"
    run_id: str
    scenario_id: str
    status: RunStatus
    started_at: str
    finished_at: str
    repeat_count: int = Field(ge=2, le=100)
    profile: Literal["fresh_profile"] = "fresh_profile"
    samples: list[InstalledSmokeReport]
    distributions: list[InstalledSmokeDistribution]
    limitations: list[str] = Field(
        default_factory=lambda: [
            "Cada repetição usa perfil e workspace novos; o cache do sistema operacional não é controlado.",
            "Distribuições incluem apenas medições válidas de amostras com status passed.",
        ]
    )


def _build_launch_command(app_path: Path, *, ui_test: bool = False) -> list[str]:
    """Build the argument vector for the installed app's startup scenario."""
    return [str(app_path), "--ui-smoke-test" if ui_test else "--smoke-test"]


def _available_loopback_port() -> int:
    """Choose an ephemeral loopback port for the disposable Electron CDP endpoint."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _inspect_installed_renderer(
    port: int, timeout_seconds: float
) -> tuple[str, int, bool, bool, float]:
    """Exercise titlebar reload and verify the installed React route remounts."""
    from playwright.sync_api import sync_playwright

    endpoint = f"http://127.0.0.1:{port}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            endpoint,
            timeout=max(1, round(timeout_seconds * 1000)),
        )
        try:
            pages = [
                page
                for context in browser.contexts
                for page in context.pages
                if not page.url.startswith("devtools://")
            ]
            if not pages:
                raise RuntimeError("Electron não expôs uma página do app via CDP.")
            page = pages[0]
            page.wait_for_function(
                """() => {
                  const root = document.querySelector('#root');
                  return document.readyState === 'complete' &&
                    root instanceof HTMLElement && root.childElementCount > 0 &&
                    root.querySelector('[data-route]') &&
                    root.querySelector('[data-titlebar]');
                }""",
                timeout=max(1, round(timeout_seconds * 1000)),
            )
            state = page.evaluate(
                """() => ({
                  readyState: document.readyState,
                  rootChildren: document.querySelector('#root')?.childElementCount ?? 0,
                  routeMounted: Boolean(document.querySelector('#root [data-route]')),
                  titlebarMounted: Boolean(document.querySelector('#root [data-titlebar]')),
                })"""
            )
            ready_state = state.get("readyState")
            root_children = state.get("rootChildren")
            route_mounted = state.get("routeMounted")
            titlebar_mounted = state.get("titlebarMounted")
            if (
                ready_state != "complete"
                or not isinstance(root_children, int)
                or route_mounted is not True
                or titlebar_mounted is not True
            ):
                raise RuntimeError("O renderer não retornou estado verificável.")
            reload_started_ns = time.monotonic_ns()
            reload_button = page.locator("#root [data-titlebar] button").nth(1)
            if reload_button.count() == 0:
                raise RuntimeError("A ação de recarregar da titlebar não existe.")
            reload_button.click(timeout=max(1, round(timeout_seconds * 1000)))
            page.wait_for_function(
                """() => {
                  const root = document.querySelector('#root');
                  const navigation = performance.getEntriesByType('navigation')[0];
                  return document.readyState === 'complete' &&
                    navigation?.type === 'reload' &&
                    root?.querySelector('[data-route]') &&
                    root.querySelector('[data-titlebar]');
                }""",
                timeout=max(1, round(timeout_seconds * 1000)),
            )
            reload_duration_ms = (time.monotonic_ns() - reload_started_ns) / 1_000_000
            return (
                ready_state,
                root_children,
                route_mounted,
                titlebar_mounted,
                reload_duration_ms,
            )
        finally:
            with suppress(Exception):
                browser.close()


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
    stage_timings_ms: dict[str, float] | None = None,
    backend_stages: dict[str, BackendStageRecord] | None = None,
    app_metadata: tuple[str, str, str, str | None, bool | None] | None = None,
    resource_metrics: list[BenchmarkMetric] | None = None,
    ui_test: bool = False,
    renderer_status: RunStatus = "not_run",
    renderer_ready_state: str | None = None,
    renderer_root_children: int | None = None,
    renderer_route_mounted: bool | None = None,
    renderer_titlebar_mounted: bool | None = None,
    renderer_reload_status: RunStatus = "not_run",
    renderer_reload_duration_ms: float | None = None,
) -> InstalledSmokeReport:
    """Create a terminal report without copying application logs or user data."""
    finished_ns = time.monotonic_ns()
    duration_ms = max(0, (finished_ns - started_ns) // 1_000_000)
    finished_at = datetime.now(UTC)
    timings = stage_timings_ms or {}
    scenario_id = "installed-app-ui" if ui_test else "installed-app-startup"
    stage_metrics = [
        BenchmarkMetric(
            run_id=run_id,
            scenario_id=scenario_id,
            stage="electron_startup",
            name=f"{stage}_elapsed",
            value=elapsed_ms,
            unit="ms",
            dimension="performance",
            source="electron.process.hrtime.bigint",
        )
        for stage, elapsed_ms in sorted(timings.items())
    ]
    stage_diagnostics = [
        EvaluationDiagnostic(
            stage=f"electron_{stage}",
            status="passed" if stage in timings else "skipped",
            duration_ms=round(timings.get(stage, 0)),
            detail=(
                "tempo desde o início do processo Electron"
                if stage in timings
                else "marcador indisponível nesta versão instalada"
            ),
        )
        for stage in _EXPECTED_STAGES
    ]
    backend_stage_records = backend_stages or {}
    backend_stage_metrics = [
        BenchmarkMetric(
            run_id=run_id,
            scenario_id=scenario_id,
            stage="backend_startup",
            name=f"{stage}_elapsed",
            value=record.elapsed_ms,
            unit="ms",
            dimension="performance",
            source="backend.process.monotonic_ns",
        )
        for stage, record in sorted(backend_stage_records.items())
    ]
    backend_stage_diagnostics = [
        EvaluationDiagnostic(
            stage=f"backend_{stage}",
            status={"ready": "passed", "failed": "failed", "skipped": "skipped"}[
                record.status
            ],
            duration_ms=round(record.elapsed_ms),
            detail="marcador correlacionado pelo run_id da avaliação",
        )
        for stage, record in sorted(backend_stage_records.items())
    ]
    (
        app_version,
        electron_version,
        chromium_version,
        source_revision,
        source_dirty,
    ) = app_metadata or (None, None, None, None, None)
    host = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine() or "unknown",
        "processor": platform.processor() or "unknown",
        "logical_cpus": str(psutil.cpu_count(logical=True) or "unknown"),
    }
    try:
        host["memory_total_mib"] = str(
            round(psutil.virtual_memory().total / (1024 * 1024))
        )
    except psutil.Error:
        host["memory_total_mib"] = "unknown"
    return InstalledSmokeReport(
        run_id=run_id,
        status=status,
        reason=reason,
        app_path=str(app_path),
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_ms=duration_ms,
        exit_code=exit_code,
        host=host,
        scenario_id=scenario_id,
        app_version=app_version,
        electron_version=electron_version,
        chromium_version=chromium_version,
        source_revision=source_revision,
        source_dirty=source_dirty,
        renderer_tested=ui_test,
        renderer_status=renderer_status,
        renderer_ready_state=renderer_ready_state,
        renderer_root_children=renderer_root_children,
        renderer_route_mounted=renderer_route_mounted,
        renderer_titlebar_mounted=renderer_titlebar_mounted,
        renderer_reload_status=renderer_reload_status,
        renderer_reload_duration_ms=renderer_reload_duration_ms,
        stage_timings_ms=timings,
        backend_stages=backend_stage_records,
        provenance=RunProvenance(
            run_id=run_id,
            scenario_id=scenario_id,
            started_at=started_at.isoformat(),
            runtime_version=app_version,
            source_revision=source_revision,
            source_dirty=source_dirty,
            host_system=platform.system(),
            host_machine=platform.machine() or "unknown",
        ),
        metrics=[
            BenchmarkMetric(
                run_id=run_id,
                scenario_id=scenario_id,
                stage="installed_app_ui" if ui_test else "installed_app_startup",
                name="duration",
                value=float(duration_ms),
                unit="ms",
                dimension="performance",
                source="python.monotonic_ns",
            ),
            *stage_metrics,
            *backend_stage_metrics,
            *(
                [
                    BenchmarkMetric(
                        run_id=run_id,
                        scenario_id=scenario_id,
                        stage="installed_app_ui",
                        name="titlebar_reload_duration",
                        value=renderer_reload_duration_ms,
                        unit="ms",
                        dimension="performance",
                        source="playwright.cdp.monotonic_ns",
                    )
                ]
                if renderer_reload_duration_ms is not None
                else []
            ),
            *(resource_metrics or []),
        ],
        diagnostics=[
            EvaluationDiagnostic(
                stage="installed_app_ui" if ui_test else "installed_app_startup",
                status=status,
                duration_ms=duration_ms,
                detail=reason,
            ),
            *stage_diagnostics,
            *backend_stage_diagnostics,
            *(
                [
                    EvaluationDiagnostic(
                        stage="renderer_react_root",
                        status=renderer_status,
                        duration_ms=0,
                        detail=(
                            f"document.readyState={renderer_ready_state}; "
                            f"#root possui {renderer_root_children} elementos filhos; "
                            f"rota={renderer_route_mounted}; "
                            f"titlebar={renderer_titlebar_mounted}"
                            if renderer_status == "passed"
                            else "o renderer instalado não passou pela verificação CDP"
                        ),
                    )
                ]
                if ui_test
                else []
            ),
            *(
                [
                    EvaluationDiagnostic(
                        stage="renderer_titlebar_reload",
                        status=renderer_reload_status,
                        duration_ms=round(renderer_reload_duration_ms or 0),
                        detail=(
                            "recarregamento via titlebar remontou a rota e a titlebar"
                            if renderer_reload_status == "passed"
                            else "ação de recarga da titlebar não foi verificada"
                        ),
                    )
                ]
                if ui_test
                else []
            ),
        ],
    )


def run_installed_smoke(  # noqa: PLR0911
    app_path: str | Path,
    *,
    timeout_seconds: float = 120,
    ui_test: bool = False,
) -> InstalledSmokeReport:
    """Open the installed app in a disposable profile and validate its startup.

    The app's own smoke path waits for backend and renderer readiness. In UI
    mode, a temporary loopback CDP endpoint also verifies that React mounted
    content before the app is closed. No provider is invoked and the user's
    checkout and application profile are never used.
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
            ui_test=ui_test,
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
            ui_test=ui_test,
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
            ui_test=ui_test,
        )

    with temporary_directory as temporary:
        profile = Path(temporary) / "profile"
        workspace = Path(temporary) / "workspace"
        try:
            profile.mkdir()
            env = _isolated_environment(profile, workspace)
            env["VECTORA_EVAL_RUN_ID"] = run_id
            cdp_port = _available_loopback_port() if ui_test else None
            if cdp_port is not None:
                env["VECTORA_ELECTRON_CDP_PORT"] = str(cdp_port)
        except OSError as exc:
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="failed",
                reason=f"Não foi possível preparar o ambiente isolado: {exc.strerror or exc}",
                exit_code=None,
                ui_test=ui_test,
            )

        resource_metrics: list[BenchmarkMetric] = []
        reader: threading.Thread | None = None
        markers = {"window_ready": False, "passed": False}
        window_ready_event = threading.Event()
        stage_timings_ms: dict[str, float] = {}
        renderer_status: RunStatus = "not_run"
        renderer_ready_state: str | None = None
        renderer_root_children: int | None = None
        renderer_route_mounted: bool | None = None
        renderer_titlebar_mounted: bool | None = None
        renderer_reload_duration_ms: float | None = None
        renderer_reload_status: RunStatus = "not_run"
        backend_stages: dict[str, BackendStageRecord] = {}
        app_metadata: tuple[str, str, str, str | None, bool | None] | None = None
        marker_lock = threading.Lock()
        try:
            process = subprocess.Popen(  # noqa: S603  # nosec B603
                (
                    _build_launch_command(app, ui_test=True)
                    if ui_test
                    else _build_launch_command(app)
                ),
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
                ui_test=ui_test,
            )

        resource_sampler = ProcessTreeResourceSampler(
            process.pid,
            run_id=run_id,
            scenario_id="installed-app-ui" if ui_test else "installed-app-startup",
        )
        resource_sampler.start()

        def read_markers() -> None:
            nonlocal app_metadata
            if process is None or process.stdout is None:
                return
            for line in process.stdout:
                with marker_lock:
                    markers["window_ready"] |= _WINDOW_READY_MARKER in line
                    if _WINDOW_READY_MARKER in line:
                        window_ready_event.set()
                    markers["passed"] |= _SMOKE_PASSED_MARKER in line
                    match = _STAGE_MARKER.fullmatch(line.strip())
                    if match:
                        stage_timings_ms[match.group(1)] = float(match.group(2))
                    metadata_match = _APP_METADATA_MARKER.fullmatch(line.strip())
                    if metadata_match:
                        app_metadata = (
                            metadata_match.group(1),
                            metadata_match.group(2),
                            metadata_match.group(3),
                            None
                            if metadata_match.group(4) in (None, "unknown")
                            else metadata_match.group(4),
                            {
                                "clean": False,
                                "dirty": True,
                                "unknown": None,
                            }.get(metadata_match.group(5)),
                        )
                    backend_match = _BACKEND_STAGE_MARKER.search(line)
                    if backend_match and backend_match.group(1) == run_id:
                        backend_stages[backend_match.group(2)] = BackendStageRecord(
                            status=cast(
                                "Literal['ready', 'failed', 'skipped']",
                                backend_match.group(4),
                            ),
                            elapsed_ms=float(backend_match.group(3)),
                        )

        reader = threading.Thread(target=read_markers, daemon=True)
        reader.start()
        try:
            if ui_test:
                deadline = time.monotonic() + timeout_seconds
                while not window_ready_event.wait(timeout=0.1):
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"O app encerrou antes da janela ficar pronta ({process.returncode})."
                        )
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(process.args, timeout_seconds)
                if cdp_port is None:
                    raise RuntimeError("A porta CDP da avaliação não foi alocada.")
                (
                    renderer_ready_state,
                    renderer_root_children,
                    renderer_route_mounted,
                    renderer_titlebar_mounted,
                    renderer_reload_duration_ms,
                ) = _inspect_installed_renderer(
                    cdp_port,
                    max(0.1, deadline - time.monotonic()),
                )
                renderer_status = "passed"
                renderer_reload_status = "passed"
                exit_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            else:
                exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            resource_metrics = resource_sampler.stop()
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="failed",
                reason=f"O app não concluiu o smoke test em {timeout_seconds:g} segundos.",
                exit_code=process.returncode,
                resource_metrics=resource_metrics,
                ui_test=ui_test,
                renderer_status=renderer_status,
                renderer_ready_state=renderer_ready_state,
                renderer_root_children=renderer_root_children,
                renderer_route_mounted=renderer_route_mounted,
                renderer_titlebar_mounted=renderer_titlebar_mounted,
                renderer_reload_status=renderer_reload_status,
                renderer_reload_duration_ms=renderer_reload_duration_ms,
            )
        except Exception as exc:
            _terminate_process_tree(process)
            resource_metrics = resource_sampler.stop()
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="failed",
                reason=(
                    f"A verificação do renderer instalado falhou: "
                    f"{type(exc).__name__}: {str(exc)[:300]}"
                ),
                exit_code=process.returncode,
                resource_metrics=resource_metrics,
                ui_test=ui_test,
                renderer_status="failed" if ui_test else "not_run",
                renderer_route_mounted=renderer_route_mounted,
                renderer_titlebar_mounted=renderer_titlebar_mounted,
                renderer_reload_status="failed" if ui_test else "not_run",
                renderer_reload_duration_ms=renderer_reload_duration_ms,
            )
        except KeyboardInterrupt:
            _terminate_process_tree(process)
            resource_metrics = resource_sampler.stop()
            return _report(
                run_id=run_id,
                app_path=app,
                started_at=started_at,
                started_ns=started_ns,
                status="failed",
                reason="Execução cancelada pelo operador; os processos foram encerrados.",
                exit_code=process.returncode,
                resource_metrics=resource_metrics,
                ui_test=ui_test,
                renderer_status=renderer_status,
                renderer_ready_state=renderer_ready_state,
                renderer_root_children=renderer_root_children,
                renderer_route_mounted=renderer_route_mounted,
                renderer_titlebar_mounted=renderer_titlebar_mounted,
                renderer_reload_status=renderer_reload_status,
                renderer_reload_duration_ms=renderer_reload_duration_ms,
            )
        finally:
            if process.poll() is None:
                _terminate_process_tree(process)
            if reader is not None:
                reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
            resource_metrics = resource_sampler.stop()

        with marker_lock:
            startup_validated = markers["window_ready"] and (
                renderer_status == "passed" if ui_test else markers["passed"]
            )
        if exit_code != 0:
            status: RunStatus = "failed"
            reason = f"O app encerrou com código {exit_code}."
        elif not startup_validated:
            status = "failed"
            reason = (
                "O app encerrou sem confirmar backend e renderer prontos."
                if not ui_test
                else "O app encerrou sem concluir a verificação do renderer."
            )
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
            stage_timings_ms=stage_timings_ms,
            backend_stages=backend_stages,
            app_metadata=app_metadata,
            resource_metrics=resource_metrics,
            ui_test=ui_test,
            renderer_status=renderer_status,
            renderer_ready_state=renderer_ready_state,
            renderer_root_children=renderer_root_children,
            renderer_route_mounted=renderer_route_mounted,
            renderer_titlebar_mounted=renderer_titlebar_mounted,
            renderer_reload_status=renderer_reload_status,
            renderer_reload_duration_ms=renderer_reload_duration_ms,
        )


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def run_installed_smoke_series(
    app_path: str | Path,
    *,
    repeat_count: int,
    timeout_seconds: float = 120,
    ui_test: bool = False,
) -> InstalledSmokeSeriesReport:
    """Run a finite number of isolated app samples with a timeout per sample."""
    if not 2 <= repeat_count <= 100:
        raise ValueError("repeat_count precisa estar entre 2 e 100")
    started_at = datetime.now(UTC)
    samples = [
        run_installed_smoke(
            app_path,
            timeout_seconds=timeout_seconds,
            ui_test=ui_test,
        )
        for _ in range(repeat_count)
    ]
    finished_at = datetime.now(UTC)
    scenario_id = "installed-app-ui" if ui_test else "installed-app-startup"
    statuses = [sample.status for sample in samples]
    if all(status == "passed" for status in statuses):
        status: RunStatus = "passed"
    elif "failed" in statuses:
        status = "failed"
    elif "blocked" in statuses:
        status = "blocked"
    elif "skipped" in statuses:
        status = "skipped"
    else:
        status = "not_run"

    grouped: dict[tuple[str, str, str, str], list[float]] = {}
    for sample in samples:
        if sample.status != "passed":
            continue
        for metric in sample.metrics:
            if metric.status != "measured" or metric.value is None:
                continue
            key = (metric.stage, metric.name, metric.dimension, metric.unit)
            grouped.setdefault(key, []).append(metric.value)

    distributions: list[InstalledSmokeDistribution] = []
    for (stage, name, dimension, unit), values in sorted(grouped.items()):
        count = len(values)
        standard_deviation = statistics.stdev(values) if count > 1 else None
        margin = (
            1.96 * standard_deviation / (count**0.5)
            if standard_deviation is not None
            else None
        )
        mean = statistics.fmean(values)
        distributions.append(
            InstalledSmokeDistribution(
                stage=stage,
                name=name,
                dimension=dimension,
                unit=unit,
                values=values,
                sample_count=count,
                attempted_count=repeat_count,
                mean=mean,
                standard_deviation=standard_deviation,
                p50=_percentile(values, 0.5),
                p95=_percentile(values, 0.95),
                confidence_95_low=mean - margin if margin is not None else None,
                confidence_95_high=mean + margin if margin is not None else None,
            )
        )

    return InstalledSmokeSeriesReport(
        run_id=uuid4().hex,
        scenario_id=scenario_id,
        status=status,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        repeat_count=repeat_count,
        samples=samples,
        distributions=distributions,
    )
