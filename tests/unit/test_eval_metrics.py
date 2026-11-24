from __future__ import annotations

import pytest

from backend.evaluation.metrics import (
    BenchmarkMetric,
    EvaluationRecorder,
    RunProvenance,
)


def test_recorder_correlates_monotonic_stage_measurement() -> None:
    recorder = EvaluationRecorder("filesystem-edit", run_id="run-1")
    recorder.start("first_response")
    metric = recorder.finish("first_response", capability="agent")

    assert metric.run_id == "run-1"
    assert metric.scenario_id == "filesystem-edit"
    assert metric.value is not None and metric.value >= 0
    assert metric.unit == "ms"
    assert metric.source == "python.monotonic_ns"
    assert metric.dimension == "performance"


def test_recorder_rejects_invalid_stage_lifecycle() -> None:
    recorder = EvaluationRecorder("scenario")
    with pytest.raises(ValueError, match="etapa não iniciada"):
        recorder.finish("missing")
    recorder.start("stage")
    with pytest.raises(ValueError, match="etapa já iniciada"):
        recorder.start("stage")


def test_unavailable_metric_does_not_report_zero() -> None:
    recorder = EvaluationRecorder("scenario", run_id="run-2")
    metric = recorder.unavailable(
        stage="provider_call",
        name="cost",
        unit="USD",
        dimension="cost",
        reason="provider não disponibilizou dados de custo",
    )

    assert metric.status == "unavailable"
    assert metric.value is None
    assert metric.reason


def test_metric_rejects_missing_measured_value_and_fake_zero() -> None:
    common = {
        "run_id": "run-3",
        "scenario_id": "scenario",
        "stage": "generation",
        "name": "cost",
        "unit": "USD",
        "dimension": "cost",
        "source": "provider.usage",
    }
    with pytest.raises(ValueError, match="exige um valor"):
        BenchmarkMetric.model_validate(common)
    with pytest.raises(ValueError, match="não pode carregar um valor"):
        BenchmarkMetric.model_validate(
            {**common, "value": 0, "status": "unavailable", "reason": "unknown"}
        )


def test_metric_keeps_usage_in_resource_dimension_and_provider_labels() -> None:
    metric = BenchmarkMetric(
        run_id="run-4",
        scenario_id="scenario",
        stage="provider_request",
        capability="chat.generate",
        name="input_tokens",
        value=42,
        unit="tokens",
        dimension="resources",
        source="provider.usage",
        provider="local",
        model="test-model",
    )

    assert metric.dimension == "resources"
    assert metric.provider == "local"
    assert metric.model == "test-model"


def test_provenance_hashes_safe_configuration_without_storing_values() -> None:
    provenance = RunProvenance.from_configuration(
        "scenario",
        {"temperature": 0, "max_tokens": 300},
        provider="local",
        model="test-model",
    )

    assert len(provenance.configuration_digest or "") == 64
    assert provenance.provider == "local"
    assert provenance.model == "test-model"
    assert "temperature" not in provenance.model_dump_json()
    assert "300" not in provenance.model_dump_json()
