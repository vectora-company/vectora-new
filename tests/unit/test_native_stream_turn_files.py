"""A varredura dos arquivos do turno não segura o evento final do stream."""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.api import native_stream
from backend.api.turn_files import TurnFileChange

_CHANGE = TurnFileChange(path="a.py", status="M", additions=1, deletions=0, hunks=[])


@pytest.mark.asyncio
async def test_slow_scan_is_abandoned_at_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(native_stream, "_TURN_FILES_TIMEOUT_SECONDS", 0.05)

    def slow_scan(_snapshot) -> list[TurnFileChange]:
        time.sleep(0.3)
        return [_CHANGE]

    started_at = time.monotonic()
    result = await native_stream._finalize_turn_files(slow_scan, None)

    assert result == []
    assert time.monotonic() - started_at < 0.25


@pytest.mark.asyncio
async def test_fast_scan_returns_changes() -> None:
    result = await native_stream._finalize_turn_files(lambda _s: [_CHANGE], None)
    assert result == [_CHANGE]


@pytest.mark.asyncio
async def test_file_persistence_runs_in_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = asyncio.Event()

    class _Store:
        async def attach_edited_files(
            self, thread_id: str, run_id: str, files: list[dict[str, str]]
        ) -> None:
            called.set()

    async def _get_store():
        return _Store()

    monkeypatch.setattr("backend.services.agent_factory.get_session_store", _get_store)

    native_stream._schedule_attach_edited_files("t", "r", [{"path": "a.py"}])

    await asyncio.wait_for(called.wait(), timeout=1)


@pytest.mark.asyncio
async def test_finalize_and_attach_returns_list_and_schedules_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        native_stream,
        "_schedule_attach_edited_files",
        lambda *args: scheduled.append(args),
    )

    payload = await native_stream._finalize_and_attach(
        lambda _s: [_CHANGE],
        lambda changes: [{"path": c.path} for c in changes],
        None,
        "t1",
        "r1",
    )

    assert payload == [{"path": "a.py"}]
    assert scheduled == [("t1", "r1", [{"path": "a.py"}])]


@pytest.mark.asyncio
async def test_finalize_and_attach_without_changes_does_not_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scheduled: list[tuple] = []
    monkeypatch.setattr(
        native_stream,
        "_schedule_attach_edited_files",
        lambda *args: scheduled.append(args),
    )

    payload = await native_stream._finalize_and_attach(
        lambda _s: [], lambda changes: [], None, "t1", "r1"
    )

    assert payload == []
    assert scheduled == []
