"""Rotas do administrador para a fila de embedding."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, NoReturn
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api.handlers import admin
from backend.vtypes.contracts import JsonObject


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | tuple[str, str]]] = []

    async def get_stats(self) -> dict[str, int]:
        return {"pending": 2, "processing": 1, "success": 5, "failed": 1, "dlq": 1}

    async def count_pending(self) -> int:
        return 3

    async def list_failures(self, limit: int) -> list[JsonObject]:
        self.calls.append(("list_failures", limit))
        return [
            {
                "queue_id": "q1",
                "collection": "docs",
                "status": "failed",
                "attempt_count": 3,
                "error": "x" * 500,
            }
        ]

    async def retry_failed(self, limit: int) -> int:
        self.calls.append(("retry", limit))
        return 2

    async def cleanup_old_records(self, days: int) -> int:
        self.calls.append(("cleanup", days))
        return 4

    async def mark_dlq(self, queue_id: str, reason: str) -> None:
        self.calls.append(("dlq", (queue_id, reason)))


def _request(role: str = "admin") -> MagicMock:
    user = SimpleNamespace(id="u1", role=role)
    request = MagicMock(spec=Request)
    request.state = SimpleNamespace(user=user)
    return request


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> FakeQueue:
    fake = FakeQueue()

    async def _get(_dsn: str | None) -> FakeQueue:
        return fake

    monkeypatch.setattr("backend.embedding.queue.get_configured_embedding_queue", _get)
    return fake


async def test_state_reports_stats_and_truncates_errors(queue: FakeQueue) -> None:
    state = await admin.get_embedding_queue_state(_request(), 5)

    assert state.stats["failed"] == 1
    assert state.pending == 3
    assert len(state.failures[0].error) == 300
    assert ("list_failures", 5) in queue.calls


async def test_retry_cleanup_and_discard_call_the_queue(queue: FakeQueue) -> None:
    moved = await admin.retry_embedding_queue(
        _request(), admin.EmbeddingQueueRetryBody(limit=10)
    )
    removed = await admin.cleanup_embedding_queue(
        _request(), admin.EmbeddingQueueCleanupBody(days=7)
    )
    discarded = await admin.discard_embedding_queue_item(_request(), "q1")

    assert moved == {"moved": 2}
    assert removed == {"deleted": 4}
    assert discarded == {"status": "dlq", "queue_id": "q1"}
    assert ("retry", 10) in queue.calls
    assert ("cleanup", 7) in queue.calls
    assert ("dlq", ("q1", "descartado por u1")) in queue.calls


@pytest.mark.parametrize("role", ["viewer", "user"])
async def test_non_admin_is_denied(queue: FakeQueue, role: str) -> None:
    with pytest.raises(HTTPException) as excinfo:
        await admin.retry_embedding_queue(
            _request(role), admin.EmbeddingQueueRetryBody()
        )

    assert excinfo.value.status_code == 403
    assert queue.calls == []


async def test_unavailable_queue_returns_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(_dsn: str | None) -> NoReturn:
        raise RuntimeError("sem postgres")

    monkeypatch.setattr("backend.embedding.queue.get_configured_embedding_queue", _boom)

    with pytest.raises(HTTPException) as excinfo:
        await admin.get_embedding_queue_state(_request(), 5)

    assert excinfo.value.status_code == 503


def test_limits_are_validated() -> None:
    with pytest.raises(ValueError):
        admin.EmbeddingQueueRetryBody(limit=0)
    with pytest.raises(ValueError):
        admin.EmbeddingQueueCleanupBody(days=400)
