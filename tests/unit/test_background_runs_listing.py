"""Listagem de runs pela conexão que a produção usa (``aiosqlite.Row``)."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

import backend
from backend.api.handlers.background import RunOut
from backend.scheduling import background_tasks as bg

_SCHEMA = (
    Path(backend.__file__).parent / "storage" / "migrations" / "sqlite" / "schema.sql"
)


@pytest.fixture
async def run_do_usuario(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = tmp_path / "runs.db"

    async def connect() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(path)
        conn.row_factory = aiosqlite.Row
        return conn

    conn = await connect()
    await conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    await conn.commit()
    await conn.close()
    monkeypatch.setattr(bg, "_get_db", connect)
    task = await bg.create_task(
        session_id="s1",
        user_id="u1",
        kind="routine",
        name="rotina",
        instruction="faça",
        trigger_type="manual",
    )
    await bg._insert_run("run-1", task, "thread-run", "manual")
    return "run-1"


async def test_runs_do_usuario_viram_resposta_da_api(run_do_usuario: str) -> None:
    rows = await bg.list_runs_for_user("s1", "u1")

    assert [RunOut.model_validate(row).id for row in rows] == [run_do_usuario]


async def test_runs_de_outro_usuario_nao_aparecem(run_do_usuario: str) -> None:
    assert await bg.list_runs_for_user("s1", "intruso") == []
