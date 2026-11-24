from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pytest

from backend.cli import eval as eval_cli
from backend.evaluation.scenario_runner import (
    ScenarioRunnerReport,
    run_fixture_scenario,
    run_fixture_suite,
    write_scenario_report,
    write_suite_report,
)
from backend.evaluation.scenarios import _CATALOG


def _write_runner(
    path: Path, assertion_status: str = "passed", *, omit_criterion: bool = False
) -> None:
    path.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "if 'VECTORA_EVAL_CAPTURE_PATH' in os.environ: "
        "Path(os.environ['VECTORA_EVAL_CAPTURE_PATH']).write_text("
        "os.environ['VECTORA_EVALUATION_WORKSPACE'])\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "scenario = json.loads(Path(os.environ['VECTORA_EVAL_SCENARIO_PATH']).read_text())\n"
        "fixture = json.loads(Path(os.environ['VECTORA_EVAL_FIXTURE_PATH']).read_text())\n"
        "criteria = scenario['success_criteria']\n"
        f"criteria = criteria[:-1] if {omit_criterion!r} else criteria\n"
        "assert scenario['scenario_id'] == 'workspace-project-open-v1'\n"
        "assert fixture['expected_paths'] == ['README.md']\n"
        "Path(os.environ['VECTORA_EVAL_RESULT_PATH']).write_text(json.dumps({"
        "'schema_version': 1, 'scenario_id': scenario['scenario_id'], "
        "'assertions': [{'name': name, 'status': "
        f"{assertion_status!r}, 'reason': 'approval trace missing'"
        "} for name in criteria]}))\n",
        encoding="utf-8",
    )


def test_fixture_runner_reports_json_markdown_and_cleans_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "workspace-path.txt"
    script = tmp_path / "runner.py"
    _write_runner(script)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-child")
    monkeypatch.setenv("VECTORA_EVAL_CAPTURE_PATH", str(capture))

    report = run_fixture_scenario(
        "workspace-project-open-v1",
        [sys.executable, str(script)],
        catalog_path=_CATALOG,
    )

    assert report.status == "passed"
    assert report.evidence_level == "fixture_runner"
    assert report.capability_maturity == "unassessed"
    assert report.provider_evidence == "not_captured"
    assert len(report.assertions) == 2
    assert report.assertions[0].status == "passed"
    workspace = Path(capture.read_text(encoding="utf-8"))
    assert not workspace.exists()

    json_path, markdown_path = write_scenario_report(report, tmp_path / "report")
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["status"] == "passed"
    assert saved["evidence_level"] == "fixture_runner"
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Maturidade da capability: `unassessed`" in markdown
    assert "não prova o app instalado" in markdown


@pytest.mark.parametrize(
    ("assertion_status", "expected_status"),
    [("unsupported", "unsupported"), ("failed", "failed")],
)
def test_fixture_runner_preserves_negative_status_and_reason(
    tmp_path: Path, assertion_status: str, expected_status: str
) -> None:
    script = tmp_path / "runner.py"
    _write_runner(script, assertion_status)

    report = run_fixture_scenario(
        "workspace-project-open-v1",
        [sys.executable, str(script)],
        catalog_path=_CATALOG,
    )

    assert report.status == expected_status
    assert report.assertions[0].reason == "approval trace missing"


def test_eval_scenario_cli_keeps_unsupported_separate_from_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "runner.py"
    _write_runner(script, "unsupported")
    report_dir = tmp_path / "cli-report"

    result = eval_cli.handle_eval(
        argparse.Namespace(
            action="scenario",
            scenario_id="workspace-project-open-v1",
            runner=sys.executable,
            runner_arg=[str(script)],
            timeout_seconds=5,
            catalog=str(_CATALOG),
            report_dir=str(report_dir),
            output="json",
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["status"] == "unsupported"
    assert payload["evidence_level"] == "fixture_runner"
    assert Path(payload["report_paths"]["json"]).is_file()
    assert Path(payload["report_paths"]["markdown"]).is_file()


def test_fixture_runner_rejects_success_without_all_versioned_criteria(
    tmp_path: Path,
) -> None:
    script = tmp_path / "runner.py"
    _write_runner(script, omit_criterion=True)

    report = run_fixture_scenario(
        "workspace-project-open-v1",
        [sys.executable, str(script)],
        catalog_path=_CATALOG,
    )

    assert report.status == "failed"
    assert report.reason == (
        "resultado não corresponde exatamente aos critérios versionados do cenário"
    )


def test_fixture_runner_times_out_and_cleans_temporary_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capture = tmp_path / "workspace-path.txt"
    marker = tmp_path / "child-survived.txt"
    script = tmp_path / "runner.py"
    child = (
        "import signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(5); "
        f"Path({str(marker)!r}).write_text('alive')"
    )
    script.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['VECTORA_EVAL_CAPTURE_PATH']).write_text("
        "os.environ['VECTORA_EVALUATION_WORKSPACE'])\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("VECTORA_EVAL_CAPTURE_PATH", str(capture))

    report = run_fixture_scenario(
        "workspace-project-open-v1",
        [sys.executable, str(script)],
        timeout_seconds=0.1,
        catalog_path=_CATALOG,
    )
    time.sleep(1.0)

    assert report.status == "failed"
    assert "excedeu o limite" in (report.reason or "")
    assert not Path(capture.read_text(encoding="utf-8")).exists()
    assert not marker.exists()


def test_scenario_report_directory_is_removed_when_markdown_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = ScenarioRunnerReport(
        run_id="run-1",
        scenario_id="fixture-case",
        status="passed",
        started_at="2026-11-24T00:00:00+00:00",
        finished_at="2026-11-24T00:00:01+00:00",
        duration_ms=1,
        fixture_sha256="a" * 64,
        runner_executable="runner",
        runner_command_sha256="b" * 64,
    )
    report_dir = tmp_path / "partial-report"
    original_write_text = Path.write_text

    def fail_markdown_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        if path.name == "report.md":
            raise OSError("simulated markdown write failure")
        return original_write_text(
            path, data, encoding=encoding, errors=errors, newline=newline
        )

    monkeypatch.setattr(Path, "write_text", fail_markdown_write)

    with pytest.raises(OSError, match="simulated markdown write failure"):
        write_scenario_report(report, report_dir)

    assert not report_dir.exists()


def test_fixture_suite_runs_each_catalog_scenario_and_keeps_unsupported_distinct(
    tmp_path: Path,
) -> None:
    script = tmp_path / "suite-runner.py"
    script.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "scenario = json.loads(Path(os.environ['VECTORA_EVAL_SCENARIO_PATH']).read_text())\n"
        "fixture = json.loads(Path(os.environ['VECTORA_EVAL_FIXTURE_PATH']).read_text())\n"
        "assert isinstance(fixture, dict)\n"
        "status = 'unsupported' if scenario['scenario_id'] == 'memory-recall-v1' else 'passed'\n"
        "Path(os.environ['VECTORA_EVAL_RESULT_PATH']).write_text(json.dumps({"
        "'schema_version': 1, 'scenario_id': scenario['scenario_id'], "
        "'assertions': [{'name': 'fixture contract loaded', 'status': status, "
        "'reason': 'provider approval evidence unavailable' if status == 'unsupported' else None}]}))\n",
        encoding="utf-8",
    )

    report = run_fixture_suite(
        [sys.executable, str(script)], catalog_path=_CATALOG, timeout_seconds=5
    )

    assert report.status == "unsupported"
    assert report.evidence_level == "fixture_runner"
    assert report.status_counts == {"passed": 6, "unsupported": 1}
    assert len(report.scenarios) == 7
    assert report.scenarios[-1].scenario_id == "memory-recall-v1"
    assert report.scenarios[-1].assertions[0].status == "unsupported"
    assert all(item.temporary_workspace_removed for item in report.scenarios)

    json_path, markdown_path = write_suite_report(report, tmp_path / "suite-report")
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["status_counts"] == {"passed": 6, "unsupported": 1}
    assert (
        len(list((tmp_path / "suite-report" / "scenarios").glob("*/report.json"))) == 7
    )
    assert "não tem timeout global" in markdown_path.read_text(encoding="utf-8")


def test_fixture_suite_rejects_unknown_selection_before_starting_runner(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "runner-started"
    script = tmp_path / "runner.py"
    script.write_text(
        f"from pathlib import Path; Path({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cenários não encontrados"):
        run_fixture_suite(
            [sys.executable, str(script)],
            catalog_path=_CATALOG,
            scenario_ids=["unknown-scenario"],
        )

    assert not marker.exists()
