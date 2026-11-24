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


class BenchmarkRun(BaseModel):
    """Execução individual que preserva proveniência e resultado terminal."""

    run_id: str
    scenario_id: str
    status: RunStatus
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
    command: list[str]
    runs: list[BenchmarkRun]
    status_counts: dict[str, int]
    distributions: list[MetricDistribution]
    limitations: list[str] = Field(default_factory=list)


class MetricComparison(BaseModel):
    name: str
    dimension: str
    unit: str
    baseline_mean: float
    candidate_mean: float
    delta: float


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
        ("scenario_id", lambda run: run.scenario_id),
        ("fixture_digest", lambda run: run.fixture_digest),
        ("host", lambda run: (run.provenance.host_system, run.provenance.host_machine)),
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
            metrics.append(
                MetricComparison(
                    name=name,
                    dimension=dimension,
                    unit=unit,
                    baseline_mean=left[key],
                    candidate_mean=right[key],
                    delta=right[key] - left[key],
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
            "revisões diferentes são os alvos da comparação e permanecem identificadas"
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
            "profile",
            "command",
            "source_revision",
            "runtime_version",
            "host_system",
            "host_machine",
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
                "profile": run.profile,
                "command": json.dumps(run.command, ensure_ascii=False),
                "source_revision": run.provenance.source_revision,
                "runtime_version": run.provenance.runtime_version,
                "host_system": run.provenance.host_system,
                "host_machine": run.provenance.host_machine,
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
