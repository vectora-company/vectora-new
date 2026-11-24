"""Evaluation Lab com suporte a Replay e Benchmark de Decisões."""

from backend.evaluation.cassette import Cassette, CassetteInteraction
from backend.evaluation.installed import (
    EvaluationDiagnostic,
    InstalledSmokeReport,
    run_installed_smoke,
)
from backend.evaluation.replay_client import ReplayChatClient
from backend.evaluation.runner import EvalMetric, EvalReport, compare_cassettes

__all__ = [
    "Cassette",
    "CassetteInteraction",
    "EvalMetric",
    "EvalReport",
    "EvaluationDiagnostic",
    "InstalledSmokeReport",
    "ReplayChatClient",
    "compare_cassettes",
    "run_installed_smoke",
]
