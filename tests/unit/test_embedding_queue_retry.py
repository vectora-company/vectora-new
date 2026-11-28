from pathlib import Path
from typing import NoReturn

import pytest

from backend.embedding.queue import EmbeddingQueue


@pytest.mark.asyncio
async def test_retry_failed_makes_exhausted_item_available_again(
    tmp_path: Path,
) -> None:
    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()
    try:
        item = await queue.enqueue("document")
        await queue.mark_processing(item)
        for _ in range(2):
            await queue.mark_retry(item)
        await queue.mark_dlq(item, "provider unavailable")
        assert await queue.get_pending() == []
        assert await queue.retry_failed() == 1
        pending = await queue.get_pending()
        assert [record.queue_id for record in pending] == [item]
        assert pending[0].dlq_reason == "provider unavailable"
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_retry_failed_respects_limit_and_leaves_success_untouched(
    tmp_path: Path,
) -> None:
    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()
    try:
        first = await queue.enqueue("first")
        second = await queue.enqueue("second")
        complete = await queue.enqueue("complete")
        await queue.mark_failed(first, "first failure")
        await queue.mark_failed(second, "second failure")
        await queue.mark_processing(complete)
        await queue.mark_success(complete)
        assert await queue.retry_failed(limit=1) == 1
        assert len(await queue.get_pending()) == 1
        assert len(await queue.get_failed()) == 1
        assert await queue.retry_failed() == 1
        assert {record.queue_id for record in await queue.get_pending()} == {
            first,
            second,
        }
        assert await queue.retry_failed() == 0
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_worker_does_not_invoke_provider_when_queue_rejects_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from sqlalchemy import text

    from backend.embedding.background import BackgroundEmbeddingWorker

    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()
    try:
        await queue.enqueue("documento")
        record = (await queue.get_pending())[0]
        assert queue.engine is not None
        async with queue.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TRIGGER refuse_claim BEFORE UPDATE ON embedding_queue "
                    "BEGIN SELECT RAISE(ABORT, 'fila selada'); END"
                )
            )
        worker = BackgroundEmbeddingWorker()
        generate = AsyncMock()
        monkeypatch.setattr(worker, "_generate_embedding", generate)
        await worker._process_record(record, queue)
        generate.assert_not_awaited()
        assert (await queue.get_job_stats("unused"))["total"] == 0
        assert (await queue.get_pending())[0].queue_id == record.queue_id
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_worker_failure_remains_retryable_when_dlq_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from backend.embedding import background
    from backend.embedding.background import BackgroundEmbeddingWorker

    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()
    try:
        await queue.enqueue("documento")
        record = (await queue.get_pending())[0]
        worker = BackgroundEmbeddingWorker()
        monkeypatch.setattr(
            worker, "_generate_embedding", AsyncMock(side_effect=RuntimeError("falhou"))
        )
        monkeypatch.setattr(background, "RETRY_BACKOFF", (0, 0, 0))

        async def fail_dlq(_queue_id: str, _reason: str) -> NoReturn:
            raise RuntimeError("DLQ indisponível")

        monkeypatch.setattr(queue, "mark_dlq", fail_dlq)
        await worker._process_record(record, queue)

        failures = await queue.list_failures(10)
        assert len(failures) == 1
        assert failures[0]["status"] == "failed"
        assert failures[0]["error"].startswith("falhou")
        assert await queue.retry_failed() == 1
        assert [item.queue_id for item in await queue.get_pending()] == [
            record.queue_id
        ]
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_retry_job_only_requeues_the_requested_job(tmp_path: Path) -> None:
    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()
    try:
        first = await queue.enqueue("first", job_id="one")
        second = await queue.enqueue("second", job_id="two")
        await queue.mark_dlq(first, "quota")
        await queue.mark_dlq(second, "quota")
        assert await queue.retry_job("one") == 1
        assert [record.queue_id for record in await queue.get_pending()] == [first]
        assert (await queue.get_job_stats("two"))["dlq"] == 1
        assert await queue.retry_job("one") == 0
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_ingestion_jobs_survive_reopening_and_keep_workspace_ownership(
    tmp_path: Path,
) -> None:
    dsn = f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}"
    first = EmbeddingQueue(dsn)
    await first.init()
    try:
        await first.save_ingestion_job("job", "workspace-a", "/project")
        await first.save_ingestion_job(
            "job", "workspace-a", "/project", total_chunks=3, enqueue_done=True
        )
        with pytest.raises(ValueError, match="outro workspace"):
            await first.save_ingestion_job("job", "workspace-b", "/other")
    finally:
        await first.close()

    second = EmbeddingQueue(dsn)
    await second.init()
    try:
        assert await second.list_ingestion_jobs("workspace-b") == []
        assert await second.list_ingestion_jobs("workspace-a") == [
            {
                "job_id": "job",
                "workspace_id": "workspace-a",
                "path": "/project",
                "total_chunks": 3,
                "enqueue_done": True,
                "enqueue_error": None,
            }
        ]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_success_cannot_overwrite_archived_or_missing_item(
    tmp_path: Path,
) -> None:
    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()
    try:
        item = await queue.enqueue("documento", job_id="job")
        await queue.mark_processing(item)
        await queue.mark_dlq(item, "quota")
        with pytest.raises(RuntimeError, match="não está em processamento"):
            await queue.mark_success(item)
        with pytest.raises(RuntimeError, match="não está em processamento"):
            await queue.mark_success("missing")
        assert (await queue.get_job_stats("job"))["dlq"] == 1
    finally:
        await queue.close()


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_same_sqlite_item(tmp_path: Path) -> None:
    import asyncio

    dsn = f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}"
    first, second = EmbeddingQueue(dsn), EmbeddingQueue(dsn)
    await first.init()
    await second.init()
    try:
        item = await first.enqueue("documento")
        outcomes = await asyncio.gather(
            first.mark_processing(item),
            second.mark_processing(item),
            return_exceptions=True,
        )
        assert sum(outcome is None for outcome in outcomes) == 1
        assert sum(isinstance(outcome, RuntimeError) for outcome in outcomes) == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_list_failures_summarizes_failed_and_dlq_items(tmp_path: Path) -> None:
    queue = EmbeddingQueue(f"sqlite+aiosqlite:///{tmp_path / 'queue.db'}")
    await queue.init()
    try:
        failed = await queue.enqueue("a", collection="docs")
        dead = await queue.enqueue("b", collection="docs")
        ok = await queue.enqueue("c", collection="docs")
        await queue.mark_failed(failed, "falhou")
        await queue.mark_dlq(dead, "sem provider")
        await queue.mark_processing(ok)
        await queue.mark_success(ok)

        failures = await queue.list_failures(10)

        assert {item["queue_id"] for item in failures} == {failed, dead}
        by_id = {item["queue_id"]: item for item in failures}
        assert by_id[failed]["error"] == "falhou"
        assert by_id[dead]["error"] == "sem provider"
        assert by_id[dead]["collection"] == "docs"
        assert await queue.cleanup_old_records(days=1) == 0
    finally:
        await queue.close()
