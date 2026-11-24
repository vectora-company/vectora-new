"""Execução isolada de um runner de cenário controlado por fixture."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess  # nosec B404
import tempfile
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from backend.evaluation.scenarios import _CATALOG, BenchmarkScenario, load_scenarios

AssertionStatus = Literal["passed", "failed", "blocked", "skipped", "unsupported"]
ScenarioStatus = Literal["passed", "failed", "blocked", "skipped", "unsupported"]
_SENSITIVE_ENV = re.compile(
    r"(?:API[_-]?KEY|ACCESS[_-]?KEY|(?:^|_)KEY(?:_|$)|AUTH|"
    r"CLIENT[_-]?SECRET|CREDENTIAL|COOKIE|PASS(?:WORD|PHRASE)|"
    r"PRIVATE[_-]?KEY|SECRET|TOKEN)",
    re.IGNORECASE,
)


class ScenarioAssertion(BaseModel):
    """Resultado verificável que o subprocesso devolveu para um critério."""

    name: str = Field(min_length=1)
    status: AssertionStatus
    reason: str | None = None


class ScenarioRunnerMetric(BaseModel):
    """Métrica numérica fornecida pelo runner, sem valor inventado."""

    name: str = Field(min_length=1)
    value: float
    unit: str = Field(min_length=1)
    dimension: Literal[
        "quality", "performance", "cost", "security", "resources", "reliability"
    ]
    source: str = Field(min_length=1)


class ScenarioRunnerPayload(BaseModel):
    """Contrato JSON produzido pelo processo executor do cenário."""

    schema_version: Literal[1]
    scenario_id: str
    assertions: list[ScenarioAssertion]
    metrics: list[ScenarioRunnerMetric] = Field(default_factory=list)


class ScenarioRunnerReport(BaseModel):
    """Resultado do subprocesso separado da instalação e do provider real."""

    schema_version: Literal[1] = 1
    evidence_level: Literal["fixture_runner"] = "fixture_runner"
    run_id: str
    scenario_id: str
    status: ScenarioStatus
    reason: str | None = None
    started_at: str
    finished_at: str
    duration_ms: int = Field(ge=0)
    fixture_sha256: str
    runner_executable: str
    runner_command_sha256: str
    exit_code: int | None = None
    assertions: list[ScenarioAssertion] = Field(default_factory=list)
    metrics: list[ScenarioRunnerMetric] = Field(default_factory=list)
    temporary_workspace_removed: bool = True
    provider_evidence: Literal["not_captured"] = "not_captured"
    limitations: list[str] = Field(
        default_factory=lambda: [
            "O resultado representa somente o runner de fixture; não prova o app instalado.",
            "A rede do subprocesso não é isolada pelo sistema operacional.",
        ]
    )


def _scenario_and_fixture(
    scenario_id: str, catalog_path: Path | str | None
) -> tuple[BenchmarkScenario, Path]:
    catalog = Path(catalog_path or _CATALOG).resolve()
    scenarios = load_scenarios(catalog)
    scenario = next(
        (item for item in scenarios if item.scenario_id == scenario_id), None
    )
    if scenario is None:
        raise ValueError(f"cenário não encontrado: {scenario_id}")
    return scenario, (catalog.parent / scenario.fixture).resolve()


def _isolated_environment(profile: Path, workspace: Path) -> dict[str, str]:
    """Preserva variáveis de execução, removendo credenciais do operador."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not _SENSITIVE_ENV.search(key)
    }
    home = profile / "home"
    vectora_home = profile / "vectora-home"
    config_home = profile / "xdg-config"
    data_home = profile / "xdg-data"
    cache_home = profile / "xdg-cache"
    for directory in (home, vectora_home, config_home, data_home, cache_home):
        directory.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "VECTORA_HOME": str(vectora_home),
            "VECTORA_EVALUATION_WORKSPACE": str(workspace),
            "XDG_CONFIG_HOME": str(config_home),
            "XDG_DATA_HOME": str(data_home),
            "XDG_CACHE_HOME": str(cache_home),
            "TMPDIR": str(profile),
            "TMP": str(profile),
            "TEMP": str(profile),
        }
    )
    return env


def _stop_process_tree(process: subprocess.Popen[bytes]) -> None:
    """Encerra o executor e subprocessos quando houver cancelamento ou timeout."""
    if os.name == "nt":
        if process.poll() is None:
            taskkill = shutil.which("taskkill")
            if taskkill:
                with suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(  # noqa: S603  # nosec B603
                        [taskkill, "/PID", str(process.pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
            if process.poll() is None:
                process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        graceful_deadline = time.monotonic() + 1.0
        while time.monotonic() < graceful_deadline:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)


def _status(assertions: list[ScenarioAssertion]) -> ScenarioStatus:
    if not assertions:
        return "failed"
    statuses = {item.status for item in assertions}
    if "failed" in statuses:
        return "failed"
    if "blocked" in statuses:
        return "blocked"
    if "unsupported" in statuses:
        return "unsupported"
    if "skipped" in statuses:
        return "skipped"
    return "passed"


def run_fixture_scenario(
    scenario_id: str,
    command: list[str],
    *,
    timeout_seconds: float = 120,
    catalog_path: Path | str | None = None,
) -> ScenarioRunnerReport:
    """Execute um runner filho com fixture/workspace temporários e contrato JSON.

    O processo recebe `VECTORA_EVAL_SCENARIO_PATH`,
    `VECTORA_EVAL_FIXTURE_PATH`, `VECTORA_EVAL_RESULT_PATH`,
    `VECTORA_EVAL_RUN_ID` e `VECTORA_EVALUATION_WORKSPACE`. Ele deve gravar no
    caminho de resultado um JSON `ScenarioRunnerPayload`.
    """
    if not command or not command[0].strip():
        raise ValueError("comando do runner obrigatório")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds precisa ser positivo")
    scenario, fixture = _scenario_and_fixture(scenario_id, catalog_path)
    run_id = uuid4().hex
    started = datetime.now(UTC)
    started_ns = time.monotonic_ns()
    command_digest = hashlib.sha256(
        json.dumps(command, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    fixture_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    report_status: ScenarioStatus = "failed"
    reason: str | None = None
    exit_code: int | None = None
    assertions: list[ScenarioAssertion] = []
    metrics: list[ScenarioRunnerMetric] = []

    try:
        temporary = tempfile.TemporaryDirectory(prefix="vectora-eval-scenario-")
    except OSError as exc:
        return _make_report(
            run_id=run_id,
            scenario=scenario,
            command=command,
            command_digest=command_digest,
            fixture_digest=fixture_digest,
            started=started,
            started_ns=started_ns,
            status="blocked",
            reason=f"não foi possível criar área temporária: {exc.strerror or exc}",
        )

    with temporary as temporary_path:
        root = Path(temporary_path)
        profile = root / "profile"
        workspace = root / "workspace"
        scenario_path = root / "scenario.json"
        fixture_path = root / "fixture.json"
        result_path = root / "result.json"
        try:
            profile.mkdir()
            shutil.copyfile(fixture, fixture_path)
            scenario_path.write_text(
                scenario.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            env = _isolated_environment(profile, workspace)
            env.update(
                {
                    "VECTORA_EVAL_RUN_ID": run_id,
                    "VECTORA_EVAL_SCENARIO_PATH": str(scenario_path),
                    "VECTORA_EVAL_FIXTURE_PATH": str(fixture_path),
                    "VECTORA_EVAL_RESULT_PATH": str(result_path),
                }
            )
            with Path(os.devnull).open("wb") as sink:
                process = subprocess.Popen(  # noqa: S603  # nosec B603
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=sink,
                    stderr=sink,
                    cwd=workspace,
                    env=env,
                    start_new_session=os.name != "nt",
                    creationflags=(
                        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
                    ),
                )
                try:
                    exit_code = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    _stop_process_tree(process)
                    reason = f"runner excedeu o limite de {timeout_seconds:g} segundos"
                except KeyboardInterrupt:
                    _stop_process_tree(process)
                    reason = "runner cancelado pelo operador"
                if reason is None:
                    if not result_path.is_file():
                        reason = "runner terminou sem gravar o resultado JSON"
                    else:
                        try:
                            payload = ScenarioRunnerPayload.model_validate_json(
                                result_path.read_text(encoding="utf-8")
                            )
                        except (OSError, ValueError) as exc:
                            reason = f"resultado do runner inválido: {exc}"
                        else:
                            if payload.scenario_id != scenario.scenario_id:
                                reason = "resultado do runner pertence a outro cenário"
                            else:
                                assertions = payload.assertions
                                metrics = payload.metrics
                                report_status = _status(assertions)
                                if exit_code != 0:
                                    report_status = "failed"
                                    reason = f"runner encerrou com código {exit_code}"
                elif reason == "runner cancelado pelo operador":
                    raise KeyboardInterrupt
        except KeyboardInterrupt:
            report_status = "failed"
        except OSError as exc:
            reason = f"não foi possível iniciar o runner: {exc.strerror or exc}"
            report_status = "blocked"

    return _make_report(
        run_id=run_id,
        scenario=scenario,
        command=command,
        command_digest=command_digest,
        fixture_digest=fixture_digest,
        started=started,
        started_ns=started_ns,
        status=report_status,
        reason=reason,
        exit_code=exit_code,
        assertions=assertions,
        metrics=metrics,
    )


def _make_report(
    *,
    run_id: str,
    scenario: BenchmarkScenario,
    command: list[str],
    command_digest: str,
    fixture_digest: str,
    started: datetime,
    started_ns: int,
    status: ScenarioStatus,
    reason: str | None,
    exit_code: int | None = None,
    assertions: list[ScenarioAssertion] | None = None,
    metrics: list[ScenarioRunnerMetric] | None = None,
) -> ScenarioRunnerReport:
    finished = datetime.now(UTC)
    return ScenarioRunnerReport(
        run_id=run_id,
        scenario_id=scenario.scenario_id,
        status=status,
        reason=reason,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_ms=max(0, (time.monotonic_ns() - started_ns) // 1_000_000),
        fixture_sha256=fixture_digest,
        runner_executable=Path(command[0]).name,
        runner_command_sha256=command_digest,
        exit_code=exit_code,
        assertions=assertions or [],
        metrics=metrics or [],
    )


def write_scenario_report(
    report: ScenarioRunnerReport, report_dir: Path | str
) -> tuple[Path, Path]:
    """Grava JSON e Markdown num diretório novo e remove saída parcial ao falhar."""
    target = Path(report_dir).expanduser()
    target.mkdir(parents=True, exist_ok=False)
    json_path = target / "report.json"
    markdown_path = target / "report.md"
    try:
        json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        lines = [
            f"# Cenário {report.scenario_id}",
            "",
            f"- Status: `{report.status}`",
            f"- Nível de evidência: `{report.evidence_level}`",
            f"- Run ID: `{report.run_id}`",
            f"- Fixture SHA-256: `{report.fixture_sha256}`",
            f"- Duração do runner: {report.duration_ms} ms",
            f"- Executável: `{report.runner_executable}`",
            f"- Digest do comando: `{report.runner_command_sha256}`",
        ]
        if report.reason:
            lines.extend(["", f"Motivo: {report.reason}"])
        lines.extend(["", "## Critérios", ""])
        if report.assertions:
            lines.extend(
                f"- `{item.status}` — {item.name}"
                + (f": {item.reason}" if item.reason else "")
                for item in report.assertions
            )
        else:
            lines.append("Nenhum critério foi reportado pelo runner.")
        lines.extend(
            [
                "",
                "## Limites",
                "",
                *[f"- {limitation}" for limitation in report.limitations],
            ]
        )
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    return json_path, markdown_path
