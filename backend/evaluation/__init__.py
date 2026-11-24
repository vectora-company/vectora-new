"""Evaluation Lab com suporte a Replay e Benchmark de Decisões."""

from backend.evaluation.cassette import Cassette, CassetteInteraction
from backend.evaluation.installed import (
    EvaluationDiagnostic,
    InstalledSmokeReport,
    run_installed_smoke,
)
from backend.evaluation.metrics import (
    BenchmarkMetric,
    EvaluationRecorder,
    RunProvenance,
)
from backend.evaluation.replay_client import ReplayChatClient
from backend.evaluation.runner import EvalMetric, EvalReport, compare_cassettes
from backend.evaluation.scenarios import (
    BenchmarkScenario,
    ScenarioReadiness,
    assess_scenario_readiness,
    load_scenarios,
)

__all__ = [
    "BenchmarkMetric",
    "BenchmarkScenario",
    "Cassette",
    "CassetteInteraction",
    "EvalMetric",
    "EvalReport",
    "EvaluationDiagnostic",
    "EvaluationRecorder",
    "InstalledSmokeReport",
    "ReplayChatClient",
    "RunProvenance",
    "ScenarioReadiness",
    "assess_scenario_readiness",
    "compare_cassettes",
    "load_scenarios",
    "run_installed_smoke",
]
