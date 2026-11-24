"""Métricas e proveniência explícitas para execuções de avaliação."""

from __future__ import annotations

import hashlib
import json
import platform
import time
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

MetricStatus = Literal["measured", "unavailable", "blocked", "skipped", "not_run"]


class BenchmarkMetric(BaseModel):
    """Uma medição com origem, unidade e estado declarados."""

    run_id: str
    scenario_id: str
    stage: str
    capability: str | None = None
    name: str
    value: float | None = None
    unit: str
    dimension: Literal[
        "quality", "performance", "cost", "security", "resources", "reliability"
    ]
    source: str
    provider: str | None = None
    model: str | None = None
    status: MetricStatus = "measured"
    reason: str | None = None

    @model_validator(mode="after")
    def validate_measurement(self) -> BenchmarkMetric:
        """A ausência de uma medição nunca deve ser confundida com zero."""
        if self.status == "measured" and self.value is None:
            raise ValueError("métrica measured exige um valor")
        if self.status != "measured" and self.value is not None:
            raise ValueError("métrica indisponível não pode carregar um valor")
        if self.status != "measured" and not self.reason:
            raise ValueError("métrica indisponível exige um motivo")
        return self


class RunProvenance(BaseModel):
    """Metadados seguros para explicar onde e como uma execução ocorreu."""

    schema_version: Literal[1] = 1
    run_id: str = Field(default_factory=lambda: uuid4().hex)
    scenario_id: str
    started_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    runtime_version: str | None = None
    source_revision: str | None = None
    host_system: str = Field(default_factory=platform.system)
    host_machine: str = Field(default_factory=lambda: platform.machine() or "unknown")
    python_version: str = Field(default_factory=platform.python_version)
    provider: str | None = None
    model: str | None = None
    configuration_digest: str | None = None
    opt_ins: list[str] = Field(default_factory=list)

    @classmethod
    def from_configuration(
        cls,
        scenario_id: str,
        configuration: dict[str, object],
        *,
        runtime_version: str | None = None,
        source_revision: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> RunProvenance:
        """Hash uma configuração já sanitizada sem persistir seus valores."""
        canonical = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return cls(
            scenario_id=scenario_id,
            configuration_digest=digest,
            runtime_version=runtime_version,
            source_revision=source_revision,
            provider=provider,
            model=model,
        )


class EvaluationRecorder:
    """Mede etapas com relógio monotônico e identidade comum da execução."""

    def __init__(self, scenario_id: str, *, run_id: str | None = None) -> None:
        self.run_id = run_id or uuid4().hex
        self.scenario_id = scenario_id
        self._started: dict[str, int] = {}
        self.metrics: list[BenchmarkMetric] = []

    def start(self, stage: str) -> None:
        """Inicia uma etapa e rejeita identidades ainda abertas."""
        if stage in self._started:
            raise ValueError(f"etapa já iniciada: {stage}")
        self._started[stage] = time.monotonic_ns()

    def finish(
        self,
        stage: str,
        *,
        capability: str | None = None,
        name: str = "duration",
        dimension: Literal[
            "quality",
            "performance",
            "cost",
            "security",
            "resources",
            "reliability",
        ] = "performance",
    ) -> BenchmarkMetric:
        """Fecha uma etapa como duração medida em milissegundos."""
        try:
            started_ns = self._started.pop(stage)
        except KeyError as exc:
            raise ValueError(f"etapa não iniciada: {stage}") from exc
        duration_ms = max(0, time.monotonic_ns() - started_ns) / 1_000_000
        metric = BenchmarkMetric(
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            stage=stage,
            capability=capability,
            name=name,
            value=duration_ms,
            unit="ms",
            dimension=dimension,
            source="python.monotonic_ns",
        )
        self.metrics.append(metric)
        return metric

    def unavailable(
        self,
        *,
        stage: str,
        name: str,
        unit: str,
        dimension: Literal[
            "quality", "performance", "cost", "security", "resources", "reliability"
        ],
        reason: str,
        capability: str | None = None,
        status: Literal["unavailable", "blocked", "skipped", "not_run"] = "unavailable",
    ) -> BenchmarkMetric:
        """Registra a falta de evidência sem inventar um valor zero."""
        metric = BenchmarkMetric(
            run_id=self.run_id,
            scenario_id=self.scenario_id,
            stage=stage,
            capability=capability,
            name=name,
            value=None,
            unit=unit,
            dimension=dimension,
            source="evaluation.recorder",
            status=status,
            reason=reason,
        )
        self.metrics.append(metric)
        return metric
