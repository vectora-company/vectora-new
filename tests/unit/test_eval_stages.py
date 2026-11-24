from __future__ import annotations

import logging

import pytest

from backend.evaluation_stages import emit_backend_stage


def test_backend_stage_is_logged_only_with_correlated_evaluation_run_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.evaluation_stages")
    monkeypatch.delenv("VECTORA_EVAL_RUN_ID", raising=False)

    emit_backend_stage("embedding_worker", "ready")

    assert "VECTORA_EVAL_BACKEND_STAGE" not in caplog.text

    run_id = "a" * 32
    monkeypatch.setenv("VECTORA_EVAL_RUN_ID", run_id)
    emit_backend_stage("embedding_worker", "ready")

    assert f"VECTORA_EVAL_BACKEND_STAGE:{run_id}:embedding_worker:" in caplog.text
    assert caplog.text.rstrip().endswith(":ready")


def test_backend_stage_rejects_invalid_correlation_and_stage_names(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="backend.evaluation_stages")
    monkeypatch.setenv("VECTORA_EVAL_RUN_ID", "not-a-run-id")
    emit_backend_stage("embedding_worker", "ready")

    monkeypatch.setenv("VECTORA_EVAL_RUN_ID", "b" * 32)
    emit_backend_stage("unsafe:stage", "failed")

    assert "VECTORA_EVAL_BACKEND_STAGE" not in caplog.text
