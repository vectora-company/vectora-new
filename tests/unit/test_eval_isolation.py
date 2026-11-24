from __future__ import annotations

import threading
from pathlib import Path

import pytest

from backend.evaluation.isolation import (
    EffectJournal,
    ScenarioIsolationGuard,
    ScenarioPolicy,
    ScenarioTerminalResult,
    cancel_guarded_run,
    run_guarded_effect,
    sidecar_failure_result,
)


def test_concurrent_sessions_write_only_to_captured_workspaces(tmp_path: Path) -> None:
    workspaces = [tmp_path / "workspace-a", tmp_path / "workspace-b"]
    for workspace in workspaces:
        workspace.mkdir()
    barrier = threading.Barrier(2)
    results: list[Path] = []
    errors: list[BaseException] = []

    def write_session(index: int) -> None:
        guard = ScenarioIsolationGuard(
            f"run-{index}", workspaces[index], ScenarioPolicy({"filesystem.write"})
        )
        barrier.wait(timeout=2)
        target = guard.authorize_effect("filesystem.write", "result.txt")
        target.write_text(guard.run_id, encoding="utf-8")
        results.append(target)

    threads = [threading.Thread(target=write_session, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    assert not errors
    assert {path.parent for path in results} == set(workspaces)
    assert (workspaces[0] / "result.txt").read_text(encoding="utf-8") == "run-0"
    assert (workspaces[1] / "result.txt").read_text(encoding="utf-8") == "run-1"


def test_workspace_switch_does_not_redirect_pending_action(tmp_path: Path) -> None:
    original = tmp_path / "original"
    selected_after_switch = tmp_path / "new"
    original.mkdir()
    selected_after_switch.mkdir()
    guard = ScenarioIsolationGuard(
        "run", original, ScenarioPolicy({"filesystem.write"})
    )
    current_workspace = selected_after_switch

    target = guard.authorize_effect("filesystem.write", "output.txt")

    assert target == original / "output.txt"
    assert current_workspace != target.parent
    with pytest.raises(PermissionError, match="fora do workspace"):
        guard.authorize_effect("filesystem.write", "../new/output.txt")


def test_revoked_policy_blocks_action_at_dispatch_time(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = ScenarioPolicy({"terminal.execute"})
    guard = ScenarioIsolationGuard("run", workspace, policy)
    policy.revoke("terminal.execute")

    with pytest.raises(PermissionError, match="não autoriza"):
        guard.authorize_effect("terminal.execute", "result.txt")


def test_cancelled_pending_effect_becomes_uncertain_and_cannot_repeat() -> None:
    journal = EffectJournal()
    assert journal.prepare("run", "effect-1")
    result = cancel_guarded_run(journal, "run")

    assert result.status == "cancelled"
    assert journal.status("run", "effect-1") == "uncertain"
    assert not journal.prepare("run", "effect-1")
    assert journal.prepare("another-run", "effect-1")


def test_effect_journal_survives_executor_restart_and_blocks_redispatch(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "state" / "effects.sqlite3"
    first_executor = EffectJournal(journal_path)
    assert first_executor.prepare("run", "external-write")
    first_executor.close()

    resumed_executor = EffectJournal(journal_path)
    assert resumed_executor.status("run", "external-write") == "prepared"
    result = cancel_guarded_run(resumed_executor, "run")
    assert result.status == "cancelled"
    resumed_executor.close()

    recovered_executor = EffectJournal(journal_path)
    assert recovered_executor.status("run", "external-write") == "uncertain"
    assert not recovered_executor.prepare("run", "external-write")
    assert recovered_executor.prepare("new-run", "external-write")
    recovered_executor.close()


def test_multiple_executors_reserve_a_persistent_effect_only_once(
    tmp_path: Path,
) -> None:
    journal_path = tmp_path / "effects.sqlite3"
    journals = [EffectJournal(journal_path), EffectJournal(journal_path)]
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def reserve(journal: EffectJournal) -> None:
        barrier.wait(timeout=2)
        results.append(journal.prepare("run", "same-effect"))

    threads = [
        threading.Thread(target=reserve, args=(journal,)) for journal in journals
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    for journal in journals:
        journal.close()

    assert sorted(results) == [False, True]


def test_guarded_effect_rechecks_policy_and_deduplicates_dispatch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    guard = ScenarioIsolationGuard(
        "run", workspace, ScenarioPolicy({"filesystem.write"})
    )
    journal = EffectJournal()
    dispatched: list[Path] = []

    assert run_guarded_effect(
        guard,
        journal,
        effect_id="edit-1",
        capability="filesystem.write",
        relative_path="result.txt",
        dispatch=dispatched.append,
    )
    assert not run_guarded_effect(
        guard,
        journal,
        effect_id="edit-1",
        capability="filesystem.write",
        relative_path="result.txt",
        dispatch=dispatched.append,
    )

    guard.policy.revoke("filesystem.write")
    with pytest.raises(PermissionError, match="não autoriza"):
        run_guarded_effect(
            guard,
            journal,
            effect_id="edit-2",
            capability="filesystem.write",
            relative_path="result.txt",
            dispatch=dispatched.append,
        )
    assert len(dispatched) == 1


def test_policy_revocation_waits_for_authorized_dispatch_and_blocks_next_effect(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = ScenarioPolicy({"filesystem.write"})
    guard = ScenarioIsolationGuard("run", workspace, policy)
    journal = EffectJournal()
    dispatch_started = threading.Event()
    release_dispatch = threading.Event()
    revoke_started = threading.Event()
    revoke_finished = threading.Event()

    def dispatch(_target: Path) -> None:
        dispatch_started.set()
        assert release_dispatch.wait(timeout=2)

    effect_thread = threading.Thread(
        target=run_guarded_effect,
        kwargs={
            "guard": guard,
            "journal": journal,
            "effect_id": "authorized-before-revoke",
            "capability": "filesystem.write",
            "relative_path": "result.txt",
            "dispatch": dispatch,
        },
    )
    effect_thread.start()
    assert dispatch_started.wait(timeout=2)

    def revoke() -> None:
        revoke_started.set()
        policy.revoke("filesystem.write")
        revoke_finished.set()

    revoke_thread = threading.Thread(target=revoke)
    revoke_thread.start()
    assert revoke_started.wait(timeout=2)
    assert not revoke_finished.wait(timeout=0.05)

    release_dispatch.set()
    effect_thread.join(timeout=2)
    revoke_thread.join(timeout=2)

    assert not effect_thread.is_alive()
    assert not revoke_thread.is_alive()
    assert revoke_finished.is_set()
    with pytest.raises(PermissionError, match="não autoriza"):
        run_guarded_effect(
            guard,
            journal,
            effect_id="after-revoke",
            capability="filesystem.write",
            relative_path="result.txt",
            dispatch=dispatch,
        )


def test_sidecar_failure_is_blocked_and_terminal_result_is_persisted(
    tmp_path: Path,
) -> None:
    result = sidecar_failure_result("run", "vectora-nats", "connection refused")
    target = tmp_path / "reports" / "run.json"
    result.save(target)

    assert result.status == "blocked"
    assert "connection refused" in target.read_text(encoding="utf-8")
    assert not list(target.parent.glob("*.tmp"))


def test_terminal_result_cannot_be_falsely_passed_on_sidecar_failure() -> None:
    result = sidecar_failure_result("run", "browser", "startup timeout")

    assert result.status == "blocked"
    assert result.status != "passed"
