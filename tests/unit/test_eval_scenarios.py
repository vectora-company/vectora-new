from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pytest

from backend.cli import eval as eval_cli
from backend.evaluation.scenarios import assess_scenario_readiness, load_scenarios

_CATALOG = Path(__file__).parents[2] / "backend/evaluation/scenarios/v1/catalog.json"


def test_versioned_catalog_covers_requested_core_flow_groups() -> None:
    scenarios = load_scenarios(_CATALOG)

    assert {scenario.category for scenario in scenarios} == {
        "workspace",
        "filesystem",
        "terminal-git",
        "browser-search-fetch",
        "mcp",
        "rag-context-graph",
        "memory",
    }
    assert all(scenario.fixture_sha256 for scenario in scenarios)
    assert all(scenario.setup and scenario.teardown for scenario in scenarios)
    assert all(scenario.success_criteria for scenario in scenarios)


def test_catalog_rejects_modified_fixture(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    shutil.copy(
        _CATALOG.parent / "fixtures" / "file-edit.json",
        fixtures / "file-edit.json",
    )
    entries = json.loads(_CATALOG.read_text(encoding="utf-8"))
    entries["scenarios"] = [
        entry for entry in entries["scenarios"] if entry["category"] == "filesystem"
    ]
    catalog.write_text(json.dumps(entries), encoding="utf-8")
    (fixtures / "file-edit.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="hash de fixture inválido"):
        load_scenarios(catalog)


def test_readiness_blocks_missing_provider_without_fake_pass() -> None:
    scenario = load_scenarios(_CATALOG)[0]

    result = assess_scenario_readiness(
        scenario,
        available_capabilities=set(scenario.required_capabilities),
    )

    assert result.status == "blocked"
    assert "provider" in (result.reason or "")


def test_remote_provider_needs_explicit_opt_in() -> None:
    scenario = load_scenarios(_CATALOG)[0]
    available = set(scenario.required_capabilities)

    result = assess_scenario_readiness(
        scenario,
        available_capabilities=available,
        provider_configured=True,
        provider_is_remote=True,
    )

    assert result.status == "skipped"
    assert "opt-in" in (result.reason or "")


def test_ready_catalog_item_is_not_reported_as_executed() -> None:
    scenario = load_scenarios(_CATALOG)[0]

    result = assess_scenario_readiness(
        scenario,
        available_capabilities=set(scenario.required_capabilities),
        provider_configured=True,
    )

    assert result.status == "not_run"


def test_eval_scenarios_cli_lists_without_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = eval_cli.handle_eval(
        argparse.Namespace(action="scenarios", output="human")
    )

    assert result == 0
    output = capsys.readouterr().out
    assert "Plano: 7 cenários versionados" in output
    assert "nenhum relatório foi gravado" in output
    assert "blocked: workspace-project-open-v1" in output


def test_scenario_plan_json_is_not_execution_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = eval_cli.handle_eval(argparse.Namespace(action="scenarios", output="json"))

    assert result == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "plan"
    assert plan["status"] == "not_run"
    assert plan["executable_started"] is False
    assert plan["report_written"] is False
    assert len(plan["scenarios"]) == 7
