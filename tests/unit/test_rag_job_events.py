"""Testes de `_maybe_emit_job_event` — emissão de progresso do RAG via SSE."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any, NoReturn

import pytest

from backend.api.handlers import workspaces
from backend.vtypes.contracts import JsonObject


class _FakeQueue:
    def __init__(self, stats: dict[str, int]) -> None:
        self._stats = stats

    async def get_job_stats(self, _job_id: str) -> dict[str, int]:
        return self._stats


@pytest.fixture(autouse=True)
def _isolado() -> Generator[None]:
    workspaces._RAG_JOBS.clear()
    workspaces._RAG_LAST_EMITTED_STATUS.clear()
    yield
    workspaces._RAG_JOBS.clear()
    workspaces._RAG_LAST_EMITTED_STATUS.clear()


def _patch_queue(monkeypatch: pytest.MonkeyPatch, stats: dict[str, int]) -> None:
    async def _get_queue(_dsn: str | None) -> _FakeQueue:
        return _FakeQueue(stats)

    monkeypatch.setattr(
        "backend.embedding.queue.get_configured_embedding_queue", _get_queue
    )


def _capture_emitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, dict]]:
    calls: list[tuple[str, str, dict]] = []

    def _fake_emit(provider: str, event_type: str, data: JsonObject) -> None:
        calls.append((provider, event_type, data))

    monkeypatch.setattr("backend.api.handlers.webhooks._emit_sse_event", _fake_emit)
    return calls


@pytest.mark.asyncio
async def test_job_desconhecido_nao_emite(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_emitted(monkeypatch)
    await workspaces._maybe_emit_job_event("job-inexistente")
    assert calls == []


@pytest.mark.asyncio
async def test_emite_na_primeira_chamada(monkeypatch: pytest.MonkeyPatch) -> None:
    workspaces._RAG_JOBS["j1"] = {
        "path": "/tmp",
        "total_chunks": 10,
        "workspace_id": "ws-1",
        "enqueue_done": False,
    }
    _patch_queue(monkeypatch, {"success": 2, "failed": 0, "dlq": 0, "total": 10})
    calls = _capture_emitted(monkeypatch)

    await workspaces._maybe_emit_job_event("j1")

    assert len(calls) == 1
    provider, event_type, data = calls[0]
    assert provider == "rag"
    assert event_type == "rag_job.indexing"
    assert data["job_id"] == "j1"
    assert data["workspace_id"] == "ws-1"
    assert data["processed"] == 2
    assert data["total"] == 10


@pytest.mark.asyncio
async def test_nao_reemite_mesma_faixa_de_progresso(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces._RAG_JOBS["j2"] = {
        "path": "/tmp",
        "total_chunks": 100,
        "workspace_id": "ws-1",
        "enqueue_done": False,
    }
    _patch_queue(monkeypatch, {"success": 1, "failed": 0, "dlq": 0, "total": 100})
    calls = _capture_emitted(monkeypatch)

    await workspaces._maybe_emit_job_event("j2")
    # Mesma faixa (~5%) — não deve reemitir.
    await workspaces._maybe_emit_job_event("j2")

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_reemite_ao_saltar_faixa_de_progresso(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces._RAG_JOBS["j3"] = {
        "path": "/tmp",
        "total_chunks": 100,
        "workspace_id": "ws-1",
        "enqueue_done": False,
    }
    calls = _capture_emitted(monkeypatch)

    _patch_queue(monkeypatch, {"success": 1, "failed": 0, "dlq": 0, "total": 100})
    await workspaces._maybe_emit_job_event("j3")

    _patch_queue(monkeypatch, {"success": 30, "failed": 0, "dlq": 0, "total": 100})
    await workspaces._maybe_emit_job_event("j3")

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_reemite_ao_mudar_status_mesmo_sem_mudar_faixa(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces._RAG_JOBS["j4"] = {
        "path": "/tmp",
        "total_chunks": 5,
        "workspace_id": "ws-1",
        "enqueue_done": False,
    }
    calls = _capture_emitted(monkeypatch)

    _patch_queue(monkeypatch, {"success": 5, "failed": 0, "dlq": 0, "total": 5})
    await workspaces._maybe_emit_job_event("j4")
    assert calls[-1][1] == "rag_job.indexing"

    workspaces._RAG_JOBS["j4"]["enqueue_done"] = True
    await workspaces._maybe_emit_job_event("j4")

    assert len(calls) == 2
    assert calls[-1][1] == "rag_job.done"


@pytest.mark.asyncio
async def test_erro_ao_buscar_stats_nao_propaga(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces._RAG_JOBS["j5"] = {
        "path": "/tmp",
        "total_chunks": 10,
        "workspace_id": "ws-1",
        "enqueue_done": False,
    }

    async def _boom(_dsn: str | None) -> NoReturn:
        raise RuntimeError("fila indisponível")

    monkeypatch.setattr("backend.embedding.queue.get_configured_embedding_queue", _boom)
    calls = _capture_emitted(monkeypatch)

    await workspaces._maybe_emit_job_event("j5")

    assert calls == []


@pytest.mark.asyncio
async def test_job_endpoints_report_queue_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from backend.api.handlers.workspaces import rag_job_status, rag_jobs

    async def unavailable(_dsn: str | None) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "backend.embedding.queue.get_configured_embedding_queue", unavailable
    )
    workspaces._RAG_JOBS["job"] = {"workspace_id": "workspace"}
    with pytest.raises(HTTPException) as status_error:
        await rag_job_status("workspace", "job")
    assert status_error.value.status_code == 503
    with pytest.raises(HTTPException) as list_error:
        await rag_jobs("workspace")
    assert list_error.value.status_code == 503


@pytest.mark.asyncio
async def test_enqueue_failure_is_visible_even_when_queue_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspaces._RAG_JOBS["job"] = {
        "workspace_id": "ws-1",
        "enqueue_done": True,
        "enqueue_error": "Falha ao preparar a fila de indexação.",
    }

    async def unavailable(_dsn: str | None) -> None:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        "backend.embedding.queue.get_configured_embedding_queue", unavailable
    )
    calls = _capture_emitted(monkeypatch)
    await workspaces._maybe_emit_job_event("job")
    assert calls[0][1] == "rag_job.failed"
    status = await workspaces.rag_job_status("ws-1", "job")
    assert status.status == "failed"
    assert status.error_reason == "Falha ao preparar a fila de indexação."

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        await workspaces.rag_job_status("other", "job")
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_retry_only_allows_own_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    workspaces._RAG_JOBS["job"] = {"workspace_id": "ws-1"}
    retry_job = AsyncMock(return_value=2)
    worker = SimpleNamespace(retry_job=retry_job)
    monkeypatch.setattr(
        "backend.embedding.background.get_background_worker",
        AsyncMock(return_value=worker),
    )
    monkeypatch.setattr(workspaces, "_maybe_emit_job_event", AsyncMock())
    with pytest.raises(HTTPException) as error:
        await workspaces.retry_rag_job("other", "job")
    assert error.value.status_code == 404
    retry_job.assert_not_awaited()
    assert await workspaces.retry_rag_job("ws-1", "job") == {"retried": 2}
    retry_job.assert_awaited_once_with("job")


@pytest.mark.asyncio
async def test_job_status_recovers_metadata_after_restart_with_workspace_isolation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi import HTTPException

    from backend.embedding.queue import EmbeddingQueue

    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()

    async def get_queue(_dsn: str | None) -> EmbeddingQueue:
        return queue

    monkeypatch.setattr("backend.embedding.queue.get_embedding_queue", get_queue)
    monkeypatch.setattr(
        "backend.embedding.queue.get_configured_embedding_queue", get_queue
    )
    try:
        await queue.save_ingestion_job(
            "job", "ws-1", "/project", total_chunks=1, enqueue_done=True
        )
        item = await queue.enqueue("data", job_id="job")
        await queue.mark_processing(item)
        await queue.mark_success(item)
        workspaces._RAG_JOBS.clear()
        status = await workspaces.rag_job_status("ws-1", "job")
        assert status.status == "done"
        assert status.processed == 1
        with pytest.raises(HTTPException) as error:
            await workspaces.rag_job_status("other", "job")
        assert error.value.status_code == 404
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_unconfirmed_preparation_is_unknown_until_persisted_completion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from backend.embedding.queue import EmbeddingQueue

    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()

    async def get_queue(_dsn: str | None) -> EmbeddingQueue:
        return queue

    monkeypatch.setattr("backend.embedding.queue.get_embedding_queue", get_queue)
    monkeypatch.setattr(
        "backend.embedding.queue.get_configured_embedding_queue", get_queue
    )
    try:
        await queue.save_ingestion_job("job", "ws-1", "/project")
        first = await workspaces.rag_job_status("ws-1", "job")
        assert first.status == "unknown"
        assert first.error_reason is not None
        await queue.save_ingestion_job(
            "job", "ws-1", "/project", total_chunks=0, enqueue_done=True
        )
        second = await workspaces.rag_job_status("ws-1", "job")
        assert second.status == "no_files"
    finally:
        await queue.close()


def test_partially_failed_job_is_not_reported_as_done() -> None:
    workspaces._RAG_JOBS["job"] = {
        "workspace_id": "ws-1",
        "path": "/project",
        "total_chunks": 3,
        "enqueue_done": True,
    }
    result = workspaces._rag_job_status(
        "job", {"total": 3, "success": 2, "failed": 0, "dlq": 1}
    )
    assert result.status == "failed"
    assert result.processed == 2
    assert result.failed == 1
