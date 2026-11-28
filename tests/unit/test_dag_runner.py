"""Integration do scheduler DAG com as tasks de background existentes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from aiosqlite import Connection

from backend.persistence.native.agent_run import AgentRun
from backend.scheduling import dag_runner
from backend.scheduling.dag import DAGStage, DAGStore
from backend.vtypes.contracts import JsonObject


@pytest.mark.asyncio
async def test_dag_dispatches_only_dependency_ready_stage_then_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dag-runner.db"
    db = await aiosqlite.connect(db_path)
    store = DAGStore(db)
    await store.setup()
    plan_id, plan_hash = await store.create_plan(
        run_id="origin-run",
        stages=[
            DAGStage("first", "Primeira etapa"),
            DAGStage("second", "Segunda etapa", ("first",)),
        ],
    )
    await store.approve_plan(plan_id, plan_hash=plan_hash, approver="user")
    execution = await store.start_execution(
        plan_id, run_id="origin-run", idempotency_key="dag-runner"
    )
    await db.close()

    async def connect_db() -> Connection:
        return await aiosqlite.connect(db_path)

    async def create_task(**kwargs: dict[str, str] | str) -> SimpleNamespace:
        scheduled.append(kwargs)
        return SimpleNamespace(id="task")

    class Sessions:
        async def get_agent_run(self, run_id: str) -> AgentRun:
            assert run_id == "origin-run"
            return AgentRun(
                run_id=run_id,
                thread_id="parent-thread",
                user_id="owner",
                workspace_id="workspace",
            )

    async def get_sessions():
        return Sessions()

    scheduled: list[dict] = []
    monkeypatch.setattr("backend.scheduling.background_tasks._get_db", connect_db)
    monkeypatch.setattr("backend.scheduling.background_tasks.create_task", create_task)
    monkeypatch.setattr(
        "backend.services.agent_factory.get_session_store", get_sessions
    )

    assert await dag_runner.dispatch_execution(execution.execution_id) == 1
    assert scheduled[0]["session_id"] == "parent-thread"
    assert scheduled[0]["trigger_config"]["dag_stage_id"] == "first"
    assert "Primeira etapa" in scheduled[0]["instruction"]

    await dag_runner.finish_stage_from_task(
        scheduled[0]["trigger_config"], passed=True, result="resultado primeiro"
    )
    assert len(scheduled) == 2
    assert scheduled[1]["trigger_config"]["dag_stage_id"] == "second"
    assert "resultado primeiro" in scheduled[1]["instruction"]


@pytest.mark.asyncio
async def test_cancel_stops_a_claimed_stage_before_its_worker_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dag-runner-cancel.db"
    db = await aiosqlite.connect(db_path)
    store = DAGStore(db)
    await store.setup()
    plan_id, plan_hash = await store.create_plan(
        run_id="origin-cancel", stages=[DAGStage("only", "Etapa")]
    )
    await store.approve_plan(plan_id, plan_hash=plan_hash, approver="owner")
    execution = await store.start_execution(
        plan_id, run_id="origin-cancel", idempotency_key="cancel-runner"
    )
    claim = await store.claim_ready_stage(execution.execution_id)
    assert claim is not None
    await store.cancel_execution(execution.execution_id, requested_by="owner")
    await db.close()

    async def connect_db() -> Connection:
        return await aiosqlite.connect(db_path)

    monkeypatch.setattr("backend.scheduling.background_tasks._get_db", connect_db)
    config: JsonObject = {
        "dag_execution_id": execution.execution_id,
        "dag_stage_id": claim[0],
        "dag_claim_token": claim[1],
    }
    assert await dag_runner.execution_cancelling_for_task(config)
    await dag_runner.cancel_stage_from_task(config, reason="cancelled before start")
    db = await aiosqlite.connect(db_path)
    store = DAGStore(db)
    await store.setup()
    plan = await store.get_plan(plan_id)
    assert plan is not None
    execution_view = plan["execution"]
    assert execution_view is not None
    assert execution_view["status"] == "cancelled"
    assert execution_view.get("stages", [])[0]["status"] == "cancelled"
    await db.close()
