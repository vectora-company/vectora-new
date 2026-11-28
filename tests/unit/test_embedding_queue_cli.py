from __future__ import annotations

from argparse import Namespace
from unittest.mock import AsyncMock

import pytest

from backend.cli.storage import _run_storage_async
from backend.main import _build_parser


def _args(*extra: str) -> Namespace:
    return _build_parser().parse_args(["storage", "migrate-embedding-queue", *extra])


@pytest.mark.asyncio
async def test_cutover_requires_offline_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    migrate = AsyncMock()
    monkeypatch.setattr("backend.embedding.queue.resolve_embedding_queue", migrate)
    with pytest.raises(SystemExit) as exc:
        await _run_storage_async(_args())
    assert exc.value.code == 1
    migrate.assert_not_awaited()


@pytest.mark.asyncio
async def test_cutover_invokes_resolver_and_closes_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrate = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr("backend.embedding.queue.resolve_embedding_queue", migrate)
    monkeypatch.setattr("backend.storage.factory.close_pg_pool", close)
    await _run_storage_async(_args("--offline"))
    assert migrate.await_args is not None
    kwargs = migrate.await_args.kwargs
    assert kwargs["offline_cutover"] is True
    assert kwargs["storage_mode"] == "complete"
    close.assert_awaited_once()
