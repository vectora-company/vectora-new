"""Leitura de planos de DAG e de suas execuções."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from backend.vtypes.contracts import JsonValue


class DagStageSpec(TypedDict):
    """Etapa como foi declarada no plano."""

    stage_id: str
    name: str
    depends_on: list[str]


class DagStageRun(DagStageSpec):
    """Etapa de uma execução, com o estado atual."""

    status: str
    attempt: int
    error: str | None
    output: JsonValue


class DagExecution(TypedDict):
    """Execução mais recente de um plano."""

    execution_id: str
    status: str
    stages: NotRequired[list[DagStageRun]]


class DagPlanRecord(TypedDict):
    """Plano persistido com a execução mais recente."""

    plan_id: str
    run_id: str
    workspace_id: str | None
    plan_hash: str
    status: str
    approved_by: str | None
    approved_at: str | None
    created_at: str
    stages: list[JsonValue]
    execution: DagExecution | None
