"""Human-readable and authorized API for persistent plan executions."""

from __future__ import annotations

import contextlib
import hmac
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.handlers.threads import (
    _assert_owns_thread,
    _get_session_store,
    _user_id,
)
from backend.scheduling.dag import DAGStore, DAGValidationError
from backend.vtypes.contracts import JsonObject
from backend.vtypes.dag import DagPlanRecord

router = APIRouter()


if TYPE_CHECKING:
    import aiosqlite


class ApproveDAGRequest(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=64)


class DAGStageView(BaseModel):
    stage_id: str
    name: str
    depends_on: list[str]
    status: str = "pending"
    attempt: int = 0
    error: str | None = None
    output: JsonObject | None = None


class DAGExecutionView(BaseModel):
    execution_id: str
    status: str
    stages: list[DAGStageView] = Field(default_factory=list)


class DAGPlanView(BaseModel):
    plan_id: str
    run_id: str
    workspace_id: str | None = None
    stages: list[DAGStageView]
    plan_hash: str
    status: str
    approved_by: str | None = None
    approved_at: str | None = None
    created_at: str
    execution: DAGExecutionView | None = None


class DAGPlanResponse(BaseModel):
    plan: DAGPlanView | None


async def _db_store() -> tuple[aiosqlite.Connection, DAGStore]:
    from backend.scheduling.background_tasks import _get_db

    db = await _get_db()
    store = DAGStore(db)
    await store.setup()
    return db, store


async def _plan_for_thread(
    thread_id: str, plan_id: str, request: Request
) -> DagPlanRecord:
    await _assert_owns_thread(thread_id, request, require_existing=True)
    sessions = await _get_session_store()
    runs = await sessions.list_agent_runs(thread_id, limit=100)
    run_ids = {str(run.run_id) for run in runs}
    db, store = await _db_store()
    try:
        plan = await store.get_plan(plan_id)
    finally:
        with contextlib.suppress(Exception):
            await db.close()
    if plan is None or plan["run_id"] not in run_ids:
        raise HTTPException(status_code=404, detail="Plano não encontrado.")
    return plan


@router.get("/threads/{thread_id}/dag", response_model=DAGPlanResponse)
async def get_latest_dag(thread_id: str, request: Request) -> DAGPlanResponse:
    """Return the most recent persisted plan belonging to this thread."""
    await _assert_owns_thread(thread_id, request, require_existing=True)
    sessions = await _get_session_store()
    runs = await sessions.list_agent_runs(thread_id, limit=100)
    db, store = await _db_store()
    try:
        for run in runs:
            plan = await store.get_plan_for_run(str(run.run_id))
            if plan is not None:
                return DAGPlanResponse(plan=DAGPlanView.model_validate(plan))
    finally:
        with contextlib.suppress(Exception):
            await db.close()
    return DAGPlanResponse(plan=None)


@router.post(
    "/threads/{thread_id}/dag/{plan_id}/approve", response_model=DAGPlanResponse
)
async def approve_dag(
    thread_id: str,
    plan_id: str,
    body: ApproveDAGRequest,
    request: Request,
) -> DAGPlanResponse:
    """Approve the immutable plan and schedule its ready stages."""
    plan = await _plan_for_thread(thread_id, plan_id, request)
    if not hmac.compare_digest(body.plan_hash, str(plan["plan_hash"])):
        raise HTTPException(
            status_code=409, detail="O plano mudou; carregue a versão atual."
        )
    db, store = await _db_store()
    try:
        if plan["status"] == "draft":
            await store.approve_plan(
                plan_id, plan_hash=body.plan_hash, approver=_user_id(request)
            )
        elif plan["status"] != "approved":
            raise HTTPException(
                status_code=409, detail="O plano não pode ser aprovado neste estado."
            )
        execution = await store.start_execution(
            plan_id,
            run_id=str(plan["run_id"]),
            idempotency_key=f"plan:{plan_id}",
        )
    except DAGValidationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        with contextlib.suppress(Exception):
            await db.close()
    from backend.scheduling.dag_runner import dispatch_execution

    await dispatch_execution(execution.execution_id)
    fresh = await _plan_for_thread(thread_id, plan_id, request)
    return DAGPlanResponse(plan=DAGPlanView.model_validate(fresh))


@router.post(
    "/threads/{thread_id}/dag/{plan_id}/cancel", response_model=DAGPlanResponse
)
async def cancel_dag(thread_id: str, plan_id: str, request: Request) -> DAGPlanResponse:
    """Cancel an active execution and invalidate its outstanding stage claims."""
    plan = await _plan_for_thread(thread_id, plan_id, request)
    execution = plan.get("execution")
    if not execution:
        raise HTTPException(status_code=409, detail="O plano ainda não foi aprovado.")
    db, store = await _db_store()
    try:
        await store.cancel_execution(
            str(execution["execution_id"]), requested_by=_user_id(request)
        )
        fresh = await store.get_plan(plan_id)
    finally:
        with contextlib.suppress(Exception):
            await db.close()
    return DAGPlanResponse(
        plan=DAGPlanView.model_validate(fresh) if fresh is not None else None
    )
