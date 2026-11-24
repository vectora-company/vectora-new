"""Agregação, comparação e exportação de relatórios reproduzíveis."""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
from collections import defaultdict
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from backend.evaluation.metrics import BenchmarkMetric, RunProvenance
from backend.persistence.native.agent_run import redact_sensitive_data

RunStatus = Literal["passed", "failed", "blocked", "skipped", "not_run", "cancelled"]
Profile = Literal["cold", "warm"]
EvidenceLevel = Literal[
    "contract", "fixture_replay", "fixture_runner", "installed_app", "live_provider"
]


class BenchmarkRun(BaseModel):
    """Execução individual que preserva proveniência e resultado terminal."""

    run_id: str
    scenario_id: str
    status: RunStatus
    evidence_level: EvidenceLevel
    profile: Profile
    provenance: RunProvenance
    command: list[str]
    fixture_digest: str
    policy_digest: str
    evaluator_version: str
    limitations: list[str] = Field(default_factory=list)
    metrics: list[BenchmarkMetric] = Field(default_factory=list)
    completed_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    @model_validator(mode="after")
    def validate_correlated_metrics(self) -> BenchmarkRun:
        """Impede misturar medições de outra execução ou cenário."""
        if self.provenance.run_id != self.run_id:
            raise ValueError("proveniência precisa compartilhar o run_id")
        if not self.provenance.source_revision:
            raise ValueError("proveniência precisa registrar SHA/revisão do build")
        if not all((self.fixture_digest, self.policy_digest, self.evaluator_version)):
            raise ValueError("fixture, policy e avaliador precisam de versão/digest")
        if not self.command or not all(token.strip() for token in self.command):
            raise ValueError("comando de execução obrigatório para reprodução")
        for metric in self.metrics:
            if metric.run_id != self.run_id or metric.scenario_id != self.scenario_id:
                raise ValueError("métrica pertence a outra execução ou cenário")
        return self


class MetricDistribution(BaseModel):
    """Distribuição preserva valores e incerteza em vez de só a média."""

    name: str
    dimension: str
    unit: str
    profile: Profile
    values: list[float]
    sample_count: int
    mean: float | None
    standard_deviation: float | None
    p50: float | None
    p90: float | None
    p95: float | None
    confidence_95_low: float | None
    confidence_95_high: float | None


class BenchmarkReport(BaseModel):
    """Relatório exportável com estados e agregações separados por perfil."""

    schema_version: Literal[1] = 1
    report_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    evidence_level: EvidenceLevel
    command: list[str]
    runs: list[BenchmarkRun]
    status_counts: dict[str, int]
    distributions: list[MetricDistribution]
    limitations: list[str] = Field(default_factory=list)


class MetricComparison(BaseModel):
    """Resumo comparável que preserva amostra, dispersão e incerteza."""

    name: str
    dimension: str
    unit: str
    baseline_sample_count: int
    baseline_mean: float
    baseline_standard_deviation: float | None
    baseline_p50: float
    baseline_p95: float
    candidate_sample_count: int
    candidate_mean: float
    candidate_standard_deviation: float | None
    candidate_p50: float
    candidate_p95: float
    delta: float
    delta_confidence_95_low: float | None
    delta_confidence_95_high: float | None


class BenchmarkComparison(BaseModel):
    schema_version: Literal[1] = 1
    compatible: bool
    differences: list[str]
    baseline_revisions: list[str]
    candidate_revisions: list[str]
    metrics: list[MetricComparison] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def aggregate_runs(runs: list[BenchmarkRun], *, command: list[str]) -> BenchmarkReport:
    """Agrega repetições com percentis, dispersão e intervalo aproximado."""
    if not runs:
        raise ValueError("ao menos uma execução é necessária")
    if len({run.scenario_id for run in runs}) != 1:
        raise ValueError("agregação exige um único cenário")
    evidence_levels = {run.evidence_level for run in runs}
    if len(evidence_levels) != 1:
        raise ValueError("um relatório não pode misturar níveis de evidência")
    condition_getters = (
        ("revisão", lambda run: run.provenance.source_revision),
        ("fixture", lambda run: run.fixture_digest),
        ("policy", lambda run: run.policy_digest),
        ("avaliador", lambda run: run.evaluator_version),
        ("sistema", lambda run: run.provenance.host_system),
        ("release do host", lambda run: run.provenance.host_release),
        ("arquitetura do host", lambda run: run.provenance.host_machine),
        ("processador", lambda run: run.provenance.host_processor),
        ("CPUs lógicas", lambda run: run.provenance.host_logical_cpu_count),
        ("memória do host", lambda run: run.provenance.host_memory_total_bytes),
        ("provider", lambda run: run.provenance.provider),
        ("modelo", lambda run: run.provenance.model),
        ("configuração", lambda run: run.provenance.configuration_digest),
    )
    for name, getter in condition_getters:
        if len({getter(run) for run in runs}) != 1:
            raise ValueError(f"agregação não pode misturar {name}")
    counts: dict[str, int] = defaultdict(int)
    groups: dict[tuple[str, str, str, Profile], list[float]] = defaultdict(list)
    for run in runs:
        counts[run.status] += 1
        for metric in run.metrics:
            if metric.status == "measured" and metric.value is not None:
                groups[
                    (metric.name, metric.dimension, metric.unit, run.profile)
                ].append(metric.value)

    distributions: list[MetricDistribution] = []
    for (name, dimension, unit, profile), values in sorted(groups.items()):
        count = len(values)
        mean = statistics.fmean(values)
        deviation = statistics.stdev(values) if count > 1 else None
        margin = 1.96 * deviation / math.sqrt(count) if deviation is not None else None
        distributions.append(
            MetricDistribution(
                name=name,
                dimension=dimension,
                unit=unit,
                profile=profile,
                values=values,
                sample_count=count,
                mean=mean,
                standard_deviation=deviation,
                p50=_percentile(values, 0.5),
                p90=_percentile(values, 0.9),
                p95=_percentile(values, 0.95),
                confidence_95_low=mean - margin if margin is not None else None,
                confidence_95_high=mean + margin if margin is not None else None,
            )
        )
    return BenchmarkReport(
        evidence_level=next(iter(evidence_levels)),
        command=command,
        runs=runs,
        status_counts=dict(counts),
        distributions=distributions,
        limitations=[
            "intervalo normal aproximado; amostras pequenas não sustentam conclusão forte"
        ],
    )


def compare_runs(
    baseline: list[BenchmarkRun], candidate: list[BenchmarkRun]
) -> BenchmarkComparison:
    """Compara somente condições compatíveis e execuções aprovadas."""
    if not baseline or not candidate:
        raise ValueError("baseline e candidate precisam ter execuções")
    differences: list[str] = []
    checks = (
        ("evidence_level", lambda run: run.evidence_level),
        ("scenario_id", lambda run: run.scenario_id),
        ("fixture_digest", lambda run: run.fixture_digest),
        (
            "host",
            lambda run: (
                run.provenance.host_system,
                run.provenance.host_release,
                run.provenance.host_machine,
                run.provenance.host_processor,
                run.provenance.host_logical_cpu_count,
                run.provenance.host_memory_total_bytes,
            ),
        ),
        ("provider", lambda run: run.provenance.provider),
        ("model", lambda run: run.provenance.model),
        ("configuration_digest", lambda run: run.provenance.configuration_digest),
        ("policy_digest", lambda run: run.policy_digest),
        ("evaluator_version", lambda run: run.evaluator_version),
        ("cold_warm_profile", lambda run: run.profile),
    )
    for name, getter in checks:
        baseline_values = {getter(run) for run in baseline}
        candidate_values = {getter(run) for run in candidate}
        if None in baseline_values or None in candidate_values:
            differences.append(f"{name}: proveniência ausente")
        elif len(baseline_values) != 1 or len(candidate_values) != 1:
            differences.append(f"{name}: valores variam dentro do grupo")
        elif baseline_values != candidate_values:
            differences.append(f"{name}: baseline e candidato diferem")

    for name, runs in (("baseline", baseline), ("candidate", candidate)):
        revisions = {run.provenance.source_revision for run in runs}
        if None in revisions or len(revisions) != 1:
            differences.append(f"source_revision: varia ou está ausente em {name}")
    if any(run.status != "passed" for run in baseline + candidate):
        differences.append("status: há execução não aprovada nos grupos")

    baseline_revisions = sorted(
        {run.provenance.source_revision or "unknown" for run in baseline}
    )
    candidate_revisions = sorted(
        {run.provenance.source_revision or "unknown" for run in candidate}
    )
    compatible = not differences
    metrics: list[MetricComparison] = []
    if compatible:
        left = _mean_metrics(baseline)
        right = _mean_metrics(candidate)
        if not left or not right:
            compatible = False
            differences.append("metrics: não há medições numéricas nos grupos")
        for key in sorted(left.keys() & right.keys()):
            name, dimension, unit = key
            baseline_values = _metric_values(baseline, key)
            candidate_values = _metric_values(candidate, key)
            baseline_deviation = (
                statistics.stdev(baseline_values) if len(baseline_values) > 1 else None
            )
            candidate_deviation = (
                statistics.stdev(candidate_values)
                if len(candidate_values) > 1
                else None
            )
            delta = right[key] - left[key]
            delta_low = None
            delta_high = None
            if baseline_deviation is not None and candidate_deviation is not None:
                standard_error = math.sqrt(
                    baseline_deviation**2 / len(baseline_values)
                    + candidate_deviation**2 / len(candidate_values)
                )
                delta_low = delta - 1.96 * standard_error
                delta_high = delta + 1.96 * standard_error
            metrics.append(
                MetricComparison(
                    name=name,
                    dimension=dimension,
                    unit=unit,
                    baseline_sample_count=len(baseline_values),
                    baseline_mean=left[key],
                    baseline_standard_deviation=baseline_deviation,
                    baseline_p50=_percentile(baseline_values, 0.5) or 0.0,
                    baseline_p95=_percentile(baseline_values, 0.95) or 0.0,
                    candidate_sample_count=len(candidate_values),
                    candidate_mean=right[key],
                    candidate_standard_deviation=candidate_deviation,
                    candidate_p50=_percentile(candidate_values, 0.5) or 0.0,
                    candidate_p95=_percentile(candidate_values, 0.95) or 0.0,
                    delta=delta,
                    delta_confidence_95_low=delta_low,
                    delta_confidence_95_high=delta_high,
                )
            )
        if compatible and left.keys() != right.keys():
            compatible = False
            differences.append("metrics: nomes, dimensões ou unidades diferem")
            metrics = []
    return BenchmarkComparison(
        compatible=compatible,
        differences=differences,
        baseline_revisions=baseline_revisions,
        candidate_revisions=candidate_revisions,
        metrics=metrics,
        limitations=[
            "revisões diferentes são os alvos da comparação e permanecem identificadas",
            "intervalo do delta usa aproximação normal independente; não é teste de significância",
            "intervalos do delta ficam ausentes quando um grupo tem menos de duas amostras",
        ],
    )


def _mean_metrics(runs: list[BenchmarkRun]) -> dict[tuple[str, str, str], float]:
    values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for run in runs:
        for metric in run.metrics:
            if metric.status == "measured" and metric.value is not None:
                values[(metric.name, metric.dimension, metric.unit)].append(
                    metric.value
                )
    return {key: statistics.fmean(items) for key, items in values.items()}


def _metric_values(runs: list[BenchmarkRun], key: tuple[str, str, str]) -> list[float]:
    """Coleta valores medidos para uma dimensão/unidade comparável."""
    name, dimension, unit = key
    return [
        metric.value
        for run in runs
        for metric in run.metrics
        if metric.status == "measured"
        and metric.value is not None
        and (metric.name, metric.dimension, metric.unit) == (name, dimension, unit)
    ]


def export_json(
    report: BaseModel, *, public: bool = False, reviewed: bool = False
) -> str:
    """Exporta JSON; publicação exige revisão explícita e redação recursiva."""
    if public and not reviewed:
        raise ValueError("export público exige revisão de privacidade explícita")
    data = report.model_dump(mode="json")
    if public:
        data = redact_sensitive_data(data)
    return json.dumps(data, indent=2, ensure_ascii=False)


def export_csv(
    report: BenchmarkReport, *, public: bool = False, reviewed: bool = False
) -> str:
    """Exporta uma linha por amostra, incluindo estados sem valor numérico."""
    if public and not reviewed:
        raise ValueError("export público exige revisão de privacidade explícita")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=[
            "report_id",
            "report_command",
            "run_id",
            "scenario_id",
            "status",
            "evidence_level",
            "profile",
            "command",
            "source_revision",
            "runtime_version",
            "host_system",
            "host_release",
            "host_machine",
            "host_processor",
            "host_logical_cpu_count",
            "host_memory_total_bytes",
            "provider",
            "model",
            "configuration_digest",
            "fixture_digest",
            "policy_digest",
            "evaluator_version",
            "completed_at",
            "metric_name",
            "dimension",
            "unit",
            "source",
            "value",
            "metric_status",
            "reason",
            "limitations",
        ],
    )
    writer.writeheader()
    for run in report.runs:
        metrics = run.metrics or [None]
        for metric in metrics:
            row = {
                "report_id": report.report_id,
                "report_command": json.dumps(report.command, ensure_ascii=False),
                "run_id": run.run_id,
                "scenario_id": run.scenario_id,
                "status": run.status,
                "evidence_level": run.evidence_level,
                "profile": run.profile,
                "command": json.dumps(run.command, ensure_ascii=False),
                "source_revision": run.provenance.source_revision,
                "runtime_version": run.provenance.runtime_version,
                "host_system": run.provenance.host_system,
                "host_release": run.provenance.host_release,
                "host_machine": run.provenance.host_machine,
                "host_processor": run.provenance.host_processor,
                "host_logical_cpu_count": run.provenance.host_logical_cpu_count,
                "host_memory_total_bytes": run.provenance.host_memory_total_bytes,
                "provider": run.provenance.provider,
                "model": run.provenance.model,
                "configuration_digest": run.provenance.configuration_digest,
                "fixture_digest": run.fixture_digest,
                "policy_digest": run.policy_digest,
                "evaluator_version": run.evaluator_version,
                "completed_at": run.completed_at,
                "metric_name": metric.name if metric else None,
                "dimension": metric.dimension if metric else None,
                "unit": metric.unit if metric else None,
                "source": metric.source if metric else None,
                "value": metric.value if metric else None,
                "metric_status": metric.status if metric else None,
                "reason": metric.reason if metric else None,
                "limitations": json.dumps(report.limitations, ensure_ascii=False),
            }
            writer.writerow(redact_sensitive_data(row) if public else row)
    return buffer.getvalue()
