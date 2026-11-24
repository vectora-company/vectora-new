"""Marcadores estruturados de etapas do backend durante avaliação instalada."""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Literal

logger = logging.getLogger(__name__)
_RUN_ID = re.compile(r"^[0-9a-f]{32}$")
_STAGE_NAME = re.compile(r"^[a-z_]+$")
_PROCESS_STARTED_NS = time.monotonic_ns()

BackendStageStatus = Literal["ready", "failed", "skipped"]


def emit_backend_stage(stage: str, status: BackendStageStatus) -> None:
    """Registra uma etapa correlacionável sem copiar detalhes ou configuração."""
    run_id = os.environ.get("VECTORA_EVAL_RUN_ID", "")
    if not _RUN_ID.fullmatch(run_id) or not _STAGE_NAME.fullmatch(stage):
        return
    elapsed_ms = max(0, time.monotonic_ns() - _PROCESS_STARTED_NS) / 1_000_000
    logger.info(
        "VECTORA_EVAL_BACKEND_STAGE:%s:%s:%.3f:%s",
        run_id,
        stage,
        elapsed_ms,
        status,
    )
