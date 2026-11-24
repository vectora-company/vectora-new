from __future__ import annotations

import pytest

from backend.engine.conversation_loop import _output_tokens_per_second
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


@pytest.mark.parametrize(
    ("usage", "first_text_ms", "provider_duration_ms", "expected", "reason"),
    [
        ({"output_tokens": 30}, 100.0, 1100.0, 30.0, None),
        (None, 100.0, 1100.0, None, "não informou tokens de saída"),
        ({"output_tokens": 30}, None, 1100.0, None, "sem emitir texto"),
        (
            {"output_tokens": 30},
            1100.0,
            1100.0,
            None,
            "janela de geração insuficiente",
        ),
        (
            {"output_tokens": -1},
            100.0,
            1100.0,
            None,
            "contador de tokens inválido",
        ),
    ],
)
def test_output_token_rate_requires_provider_usage_and_elapsed_generation_window(
    usage: dict[str, int | float] | None,
    first_text_ms: float | None,
    provider_duration_ms: float,
    expected: float | None,
    reason: str | None,
) -> None:
    value, actual_reason = _output_tokens_per_second(
        usage,
        first_text_ms=first_text_ms,
        provider_duration_ms=provider_duration_ms,
    )

    assert value == expected
    if reason:
        assert reason in (actual_reason or "")
    else:
        assert actual_reason is None


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
    serialized = provenance.model_dump_json()
    assert "temperature" not in serialized
    assert "max_tokens" not in serialized
