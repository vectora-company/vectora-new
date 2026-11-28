"""API de aprovação DAG: vínculo ao thread, hash e dispatch autenticado."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import aiosqlite
import pytest
from aiosqlite import Connection
from fastapi import HTTPException
from starlette.requests import Request

from backend.api.handlers import dag
from backend.scheduling.dag import DAGStage, DAGStore


@pytest.mark.asyncio
async def test_approve_dag_checks_thread_and_dispatches_after_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dag-api.db"
    db = await aiosqlite.connect(db_path)
    store = DAGStore(db)
    await store.setup()
    plan_id, plan_hash = await store.create_plan(
        run_id="run-owned", stages=[DAGStage("first", "Faça a primeira etapa")]
    )
    await db.close()

    class Sessions:
        async def list_agent_runs(
            self, thread_id: str, *, limit: int
        ) -> list[SimpleNamespace]:
            return [SimpleNamespace(run_id="run-owned")]

    async def get_sessions():
        return Sessions()

    async def connect_db() -> Connection:
        return await aiosqlite.connect(db_path)

    dispatched: list[str] = []

    async def dispatch(execution_id: str) -> int:
        dispatched.append(execution_id)
        return 1

    monkeypatch.setattr(dag, "_assert_owns_thread", _async_noop)
    monkeypatch.setattr(dag, "_get_session_store", get_sessions)
    monkeypatch.setattr(dag, "_user_id", lambda _request: "human-owner")
    monkeypatch.setattr("backend.scheduling.background_tasks._get_db", connect_db)
    monkeypatch.setattr("backend.scheduling.dag_runner.dispatch_execution", dispatch)

    result = await dag.approve_dag(
        "thread-owned",
        plan_id,
        dag.ApproveDAGRequest(plan_hash=plan_hash),
        request=_request(),
    )
    assert result.plan is not None
    assert result.plan.status == "approved"
    assert result.plan.execution is not None
    assert dispatched == [result.plan.execution.execution_id]
    assert result.plan.approved_by == "human-owner"


@pytest.mark.asyncio
async def test_plan_from_another_thread_is_not_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "dag-api-owner.db"
    db = await aiosqlite.connect(db_path)
    store = DAGStore(db)
    await store.setup()
    plan_id, _ = await store.create_plan(
        run_id="run-other", stages=[DAGStage("first", "Etapa")]
    )
    await db.close()

    class Sessions:
        async def list_agent_runs(
            self, thread_id: str, *, limit: int
        ) -> list[SimpleNamespace]:
            return [SimpleNamespace(run_id="run-owned")]

    async def get_sessions():
        return Sessions()

    async def connect_db() -> Connection:
        return await aiosqlite.connect(db_path)

    monkeypatch.setattr(dag, "_assert_owns_thread", _async_noop)
    monkeypatch.setattr(dag, "_get_session_store", get_sessions)
    monkeypatch.setattr("backend.scheduling.background_tasks._get_db", connect_db)

    with pytest.raises(HTTPException) as exc:
        await dag._plan_for_thread("thread-owned", plan_id, _request())
    assert exc.value.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["read", "approve", "cancel"])
async def test_dag_operations_reject_a_thread_the_user_cannot_access(
    operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def denied(*_args: Request | str, **_kwargs: bool) -> NoReturn:
        raise HTTPException(status_code=404, detail="Thread not found")

    async def unexpected(*_args, **_kwargs) -> NoReturn:
        raise AssertionError("a operação não deve consultar nem alterar a DAG")

    monkeypatch.setattr(dag, "_assert_owns_thread", denied)
    monkeypatch.setattr(dag, "_get_session_store", unexpected)
    request = _request()

    async def perform_operation() -> None:
        if operation == "read":
            await dag.get_latest_dag("thread-private", request)
        elif operation == "approve":
            await dag.approve_dag(
                "thread-private",
                "plan-private",
                dag.ApproveDAGRequest(plan_hash="a" * 64),
                request,
            )
        else:
            await dag.cancel_dag("thread-private", "plan-private", request)

    with pytest.raises(HTTPException) as exc:
        await perform_operation()

    assert exc.value.status_code == 404


async def _async_noop(*_args: Request | str, **_kwargs: bool) -> None:
    return None


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})
