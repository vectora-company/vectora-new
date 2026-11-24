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

__all__ = [
    "BenchmarkMetric",
    "Cassette",
    "CassetteInteraction",
    "EvalMetric",
    "EvalReport",
    "EvaluationDiagnostic",
    "EvaluationRecorder",
    "InstalledSmokeReport",
    "ReplayChatClient",
    "RunProvenance",
    "compare_cassettes",
    "run_installed_smoke",
]
