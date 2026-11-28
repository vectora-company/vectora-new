import json
from types import TracebackType
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.embedding.queue import PostgresQueueDB


class _ConnectionContext:
    def __init__(self, connection: AsyncMock) -> None:
        self.connection = connection

    async def __aenter__(self) -> AsyncMock:
        return self.connection

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        pass


class _Pool:
    def __init__(self, connection: AsyncMock) -> None:
        self.connection = connection

    def acquire(self) -> _ConnectionContext:
        return _ConnectionContext(self.connection)


@pytest.mark.asyncio
async def test_postgres_enqueue_preserves_job_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = AsyncMock()
    monkeypatch.setattr(
        "backend.storage.factory.get_pg_pool", AsyncMock(return_value=_Pool(connection))
    )
    queue = PostgresQueueDB()

    queue_id = await queue.enqueue(
        "conteúdo", "docs", {"workspace_id": "w"}, job_id="job"
    )

    args = connection.execute.await_args.args
    assert args[1] == queue_id
    task = json.loads(args[2])
    assert task["text"] == "conteúdo"
    assert task["collection"] == "docs"
    assert json.loads(task["doc_metadata"]) == {"workspace_id": "w"}
    assert task["job_id"] == "job"


@pytest.mark.asyncio
async def test_postgres_job_stats_count_only_requested_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = AsyncMock()
    connection.fetch.return_value = [
        {"status": "success", "count": 2},
        {"status": "dlq", "count": 1},
    ]
    monkeypatch.setattr(
        "backend.storage.factory.get_pg_pool", AsyncMock(return_value=_Pool(connection))
    )

    stats = await PostgresQueueDB().get_job_stats("job-1")

    assert stats == {
        "pending": 0,
        "processing": 0,
        "success": 2,
        "failed": 0,
        "dlq": 1,
        "total": 3,
    }
    assert connection.fetch.await_args.args[1] == "job-1"


@pytest.mark.asyncio
async def test_postgres_pending_exposes_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = PostgresQueueDB()
    monkeypatch.setattr(
        queue, "dequeue", AsyncMock(return_value=[{"queue_id": "q", "job_id": "job"}])
    )

    records = await queue.get_pending()

    assert records[0].job_id == "job"
