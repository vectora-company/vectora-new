from __future__ import annotations

import json
from typing import Literal

import pytest

from backend.evaluation.metrics import BenchmarkMetric, RunProvenance
from backend.evaluation.reports import (
    BenchmarkRun,
    aggregate_runs,
    compare_runs,
    export_csv,
    export_json,
)


def _run(
    run_id: str,
    value: float,
    *,
    status: Literal[
        "passed", "failed", "blocked", "skipped", "not_run", "cancelled"
    ] = "passed",
    profile: Literal["cold", "warm"] = "cold",
    fixture_digest: str = "fixture-v1",
    revision: str = "sha-a",
    scenario_id: str = "scenario-1",
) -> BenchmarkRun:
    return BenchmarkRun(
        run_id=run_id,
        scenario_id=scenario_id,
        status=status,
        evidence_level="live_provider",
        profile=profile,
        provenance=RunProvenance(
            run_id=run_id,
            scenario_id=scenario_id,
            source_revision=revision,
            host_system="Linux",
            host_machine="x86_64",
            provider="local",
            model="fixture-model",
            configuration_digest="config-v1",
        ),
        command=["vectora", "eval", "run"],
        fixture_digest=fixture_digest,
        policy_digest="policy-v1",
        evaluator_version="eval-v1",
        metrics=[
            BenchmarkMetric(
                run_id=run_id,
                scenario_id=scenario_id,
                stage="startup",
                name="latency",
                value=value,
                unit="ms",
                dimension="performance",
                source="python.monotonic_ns",
            )
        ],
    )


def test_aggregation_keeps_cold_warm_profiles_and_uncertainty_separate() -> None:
    report = aggregate_runs(
        [_run("a", 10), _run("b", 20), _run("c", 30, profile="warm")],
        command=["vectora", "eval", "scenario", "--local"],
    )

    assert report.status_counts == {"passed": 3}
    assert len(report.distributions) == 2
    cold = next(item for item in report.distributions if item.profile == "cold")
    warm = next(item for item in report.distributions if item.profile == "warm")
    assert cold.mean == 15
    assert cold.p50 == 15
    assert cold.p90 == 19
    assert cold.standard_deviation is not None
    assert cold.confidence_95_low is not None
    assert warm.sample_count == 1
    assert warm.confidence_95_low is None


def test_aggregation_rejects_mixing_replay_and_live_evidence() -> None:
    replay = _run("replay", 10)
    replay.evidence_level = "fixture_replay"

    with pytest.raises(ValueError, match="não pode misturar níveis de evidência"):
        aggregate_runs([_run("live", 10), replay], command=["vectora", "eval"])


def test_aggregation_rejects_mixing_fixture_runner_and_cassette_replay() -> None:
    fixture_runner = _run("runner", 10)
    fixture_runner.evidence_level = "fixture_runner"
    cassette_replay = _run("replay", 10)
    cassette_replay.evidence_level = "fixture_replay"

    with pytest.raises(ValueError, match="não pode misturar níveis de evidência"):
        aggregate_runs([fixture_runner, cassette_replay], command=["vectora", "eval"])


def test_comparison_records_target_revisions_and_ignores_expected_sha_change() -> None:
    report = compare_runs(
        [_run("base-1", 20, revision="sha-base")],
        [_run("candidate-1", 15, revision="sha-candidate")],
    )

    assert report.compatible
    assert report.baseline_revisions == ["sha-base"]
    assert report.candidate_revisions == ["sha-candidate"]
    assert report.metrics[0].delta == -5


def test_comparison_refuses_different_fixtures_and_failed_runs() -> None:
    fixture_mismatch = compare_runs(
        [_run("base", 20)],
        [_run("candidate", 10, fixture_digest="fixture-v2")],
    )
    failed_run = compare_runs(
        [_run("base", 20)],
        [_run("candidate", 10, status="blocked")],
    )

    assert not fixture_mismatch.compatible
    assert fixture_mismatch.metrics == []
    assert any("fixture_digest" in item for item in fixture_mismatch.differences)
    assert not failed_run.compatible
    assert any("status" in item for item in failed_run.differences)


def test_comparison_refuses_mixing_fixture_replay_with_live_provider() -> None:
    live = _run("live", 10)
    replay = _run("replay", 10)
    replay.evidence_level = "fixture_replay"

    comparison = compare_runs([live], [replay])

    assert not comparison.compatible
    assert comparison.metrics == []
    assert "evidence_level: baseline e candidato diferem" in comparison.differences


def test_json_csv_exports_preserve_schema_status_and_provenance() -> None:
    report = aggregate_runs([_run("run-1", 9)], command=["vectora", "eval"])
    json_data = json.loads(export_json(report))
    csv_data = export_csv(report)

    assert json_data["schema_version"] == 1
    assert json_data["runs"][0]["provenance"]["source_revision"] == "sha-a"
    assert json_data["runs"][0]["metrics"][0]["unit"] == "ms"
    assert (
        "report_id,report_command,run_id,scenario_id,status,evidence_level,profile"
        in csv_data
    )
    assert "run-1" in csv_data
    assert "config-v1" in csv_data
    assert "fixture-v1" in csv_data
    assert "policy-v1" in csv_data
    assert "python.monotonic_ns" in csv_data


def test_public_export_requires_review_and_redacts_sensitive_values() -> None:
    run = _run("run-secret", 1)
    run.command = ["vectora", "--api-key=private-secret-value"]
    report = aggregate_runs([run], command=run.command)

    with pytest.raises(ValueError, match="revisão de privacidade"):
        export_json(report, public=True)
    safe_json = export_json(report, public=True, reviewed=True)
    safe_csv = export_csv(report, public=True, reviewed=True)

    assert "private-secret-value" not in safe_json
    assert "private-secret-value" not in safe_csv
    assert "run-secret" in safe_json


def test_report_requires_one_scenario_and_correlated_run_id() -> None:
    with pytest.raises(ValueError, match="um único cenário"):
        aggregate_runs(
            [_run("a", 1, scenario_id="scenario-a"), _run("b", 2)], command=[]
        )
