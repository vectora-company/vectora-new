"""Evaluation Lab com suporte a Replay e Benchmark de Decisões."""

from backend.evaluation.cassette import Cassette, CassetteInteraction
from backend.evaluation.installed import (
    EvaluationDiagnostic,
    InstalledSmokeReport,
    run_installed_smoke,
)
from backend.evaluation.isolation import (
    EffectJournal,
    ScenarioIsolationGuard,
    ScenarioPolicy,
    ScenarioTerminalResult,
    cancel_guarded_run,
    run_guarded_effect,
    sidecar_failure_result,
)
from backend.evaluation.metrics import (
    BenchmarkMetric,
    EvaluationRecorder,
    RunProvenance,
)
from backend.evaluation.replay_client import ReplayChatClient
from backend.evaluation.reports import (
    BenchmarkComparison,
    BenchmarkReport,
    BenchmarkRun,
    MetricDistribution,
    aggregate_runs,
    compare_runs,
    export_csv,
    export_json,
)
from backend.evaluation.runner import EvalMetric, EvalReport, compare_cassettes
from backend.evaluation.scenarios import (
    BenchmarkScenario,
    ScenarioReadiness,
    assess_scenario_readiness,
    load_scenarios,
)

__all__ = [
    "BenchmarkComparison",
    "BenchmarkMetric",
    "BenchmarkReport",
    "BenchmarkRun",
    "BenchmarkScenario",
    "Cassette",
    "CassetteInteraction",
    "EffectJournal",
    "EvalMetric",
    "EvalReport",
    "EvaluationDiagnostic",
    "EvaluationRecorder",
    "InstalledSmokeReport",
    "MetricDistribution",
    "ReplayChatClient",
    "RunProvenance",
    "ScenarioIsolationGuard",
    "ScenarioPolicy",
    "ScenarioReadiness",
    "ScenarioTerminalResult",
    "aggregate_runs",
    "assess_scenario_readiness",
    "cancel_guarded_run",
    "compare_cassettes",
    "compare_runs",
    "export_csv",
    "export_json",
    "load_scenarios",
    "run_guarded_effect",
    "run_installed_smoke",
    "sidecar_failure_result",
]
