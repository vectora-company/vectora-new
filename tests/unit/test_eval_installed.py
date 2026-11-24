from __future__ import annotations

import argparse
import os
import plistlib
import sys
import time
from pathlib import Path

import pytest

from backend.cli import eval as eval_cli
from backend.evaluation import installed


def _python_app(monkeypatch: pytest.MonkeyPatch, script: Path) -> None:
    monkeypatch.setattr(
        installed,
        "_build_launch_command",
        lambda _app: [sys.executable, str(script), "--smoke-test"],
    )


def test_installed_smoke_reports_pass_and_removes_isolated_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "vectora-app"
    app.touch()
    capture = tmp_path / "profile-path.txt"
    script = tmp_path / "success.py"
    script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['EVAL_CAPTURE']).write_text("
        "os.environ['VECTORA_HOME'] + '\\n' + "
        "os.environ['VECTORA_EVALUATION_WORKSPACE'] + '\\n' + os.getcwd())\n"
        "assert 'OPENAI_API_KEY' not in os.environ\n"
        "assert 'GPG_PASSPHRASE' not in os.environ\n"
        "assert 'VECTORA_LICENSE_BYPASS' not in os.environ\n"
        "run_id = os.environ['VECTORA_EVAL_RUN_ID']\n"
        "print('VECTORA_EVAL_APP:0.1.26:45.3.3:144.0.7559.60')\n"
        "print(f'VECTORA_EVAL_BACKEND_STAGE:{run_id}:embedding_worker:35.250:ready')\n"
        "print(f'VECTORA_EVAL_BACKEND_STAGE:{run_id}:nats_sidecar:45.500:skipped')\n"
        "print('VECTORA_EVAL_BACKEND_STAGE:' + '0' * 32 + ':fake:1.000:failed')\n"
        "print('VECTORA_EVAL_STAGE:electron_ready:1.000')\n"
        "print('VECTORA_EVAL_STAGE:backend_started:5.000')\n"
        "print('VECTORA_EVAL_STAGE:backend_ready:12.500')\n"
        "print('VECTORA_EVAL_STAGE:renderer_dom_ready:30.250')\n"
        "print('VECTORA_EVAL_STAGE:renderer_loaded:40.500')\n"
        "print('VECTORA_EVAL_STAGE:window_ready:41.000')\n"
        "print('VECTORA_EVAL_STAGE:shutdown_complete:50.000')\n"
        "print('VECTORA_ELECTRON_WINDOW_READY')\n"
        "print('Vectora startup smoke test passed; closing the app.')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EVAL_CAPTURE", str(capture))
    monkeypatch.setenv("OPENAI_API_KEY", "private-test-key")
    monkeypatch.setenv("GPG_PASSPHRASE", "private-passphrase")
    monkeypatch.setenv("VECTORA_LICENSE_BYPASS", "1")
    _python_app(monkeypatch, script)

    report = installed.run_installed_smoke(app, timeout_seconds=5)

    assert report.status == "passed"
    assert report.evidence_level == "installed_app"
    assert report.real_provider_tested is False
    assert report.scenario_task_executed is False
    assert report.run_id
    assert report.app_version == "0.1.26"
    assert report.electron_version == "45.3.3"
    assert report.chromium_version == "144.0.7559.60"
    assert report.provenance.runtime_version == "0.1.26"
    assert "release" in report.host
    assert report.diagnostics[0].status == "passed"
    assert report.stage_timings_ms["backend_ready"] == 12.5
    assert report.stage_timings_ms["shutdown_complete"] == 50
    assert report.backend_stages["embedding_worker"].status == "ready"
    assert report.backend_stages["embedding_worker"].elapsed_ms == 35.25
    assert report.backend_stages["nats_sidecar"].status == "skipped"
    assert "fake" not in report.backend_stages
    assert {
        metric.name for metric in report.metrics if metric.stage == "electron_startup"
    } == {
        "electron_ready_elapsed",
        "backend_started_elapsed",
        "backend_ready_elapsed",
        "renderer_dom_ready_elapsed",
        "renderer_loaded_elapsed",
        "window_ready_elapsed",
        "shutdown_complete_elapsed",
    }
    profile_raw, workspace_raw, current_directory = capture.read_text(
        encoding="utf-8"
    ).splitlines()
    profile = Path(profile_raw)
    workspace = Path(workspace_raw)
    assert profile.parent.parent.name.startswith("vectora-eval-")
    assert not profile.exists()
    assert not workspace.exists()
    assert current_directory == workspace_raw


def test_missing_installed_app_is_blocked(tmp_path: Path) -> None:
    report = installed.run_installed_smoke(tmp_path / "missing-app")

    assert report.status == "blocked"
    assert "não existe" in (report.reason or "")


def test_macos_app_bundle_resolves_its_installed_executable(tmp_path: Path) -> None:
    bundle = tmp_path / "Vectora.app"
    executable = bundle / "Contents" / "MacOS" / "Vectora"
    executable.parent.mkdir(parents=True)
    executable.touch()
    info = bundle / "Contents" / "Info.plist"
    info.write_bytes(plistlib.dumps({"CFBundleExecutable": "Vectora"}))

    resolved, reason = installed._resolve_app_executable(bundle)

    assert resolved == executable.resolve()
    assert reason is None


def test_eval_smoke_cli_writes_structured_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "evaluation.json"
    expected = installed.InstalledSmokeReport(
        run_id="run-test",
        status="passed",
        provenance=installed.RunProvenance(
            run_id="run-test", scenario_id="installed-app-startup"
        ),
        app_path="/opt/vectora",
        started_at="2026-11-24T00:00:00+00:00",
        finished_at="2026-11-24T00:00:01+00:00",
        duration_ms=1,
    )
    monkeypatch.setattr(
        eval_cli, "run_installed_smoke", lambda *_args, **_kwargs: expected
    )

    result = eval_cli.handle_eval(
        argparse.Namespace(
            action="smoke",
            app="/opt/vectora",
            timeout_seconds=5,
            report=str(target),
            output="json",
        )
    )

    assert result == 0
    assert '"run_id": "run-test"' in target.read_text(encoding="utf-8")
    assert '"status": "passed"' in capsys.readouterr().out


def test_nonzero_exit_without_smoke_evidence_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "vectora-app"
    app.touch()
    script = tmp_path / "failure.py"
    script.write_text("raise SystemExit(7)\n", encoding="utf-8")
    _python_app(monkeypatch, script)

    report = installed.run_installed_smoke(app, timeout_seconds=5)

    assert report.status == "failed"
    assert report.exit_code == 7


def test_zero_exit_without_startup_markers_is_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "vectora-app"
    app.touch()
    script = tmp_path / "missing-markers.py"
    script.write_text("print('startup output only')\n", encoding="utf-8")
    _python_app(monkeypatch, script)

    report = installed.run_installed_smoke(app, timeout_seconds=5)

    assert report.status == "failed"
    assert "sem confirmar backend e renderer" in (report.reason or "")
    assert all(
        item.status == "skipped"
        for item in report.diagnostics
        if item.stage.startswith("electron_")
    )


def test_timeout_terminates_child_processes_and_cleans_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "vectora-app"
    app.touch()
    capture = tmp_path / "profile-path.txt"
    marker = tmp_path / "child-finished.txt"
    script = tmp_path / "timeout.py"
    child_code = (
        "import time; from pathlib import Path; time.sleep(0.7); "
        f"Path({str(marker)!r}).write_text('alive')"
    )
    script.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "Path(os.environ['EVAL_CAPTURE']).write_text(os.environ['VECTORA_HOME'])\n"
        f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EVAL_CAPTURE", str(capture))
    _python_app(monkeypatch, script)

    report = installed.run_installed_smoke(app, timeout_seconds=0.1)
    time.sleep(0.9)

    assert report.status == "failed"
    assert "não concluiu" in (report.reason or "")
    profile = Path(capture.read_text(encoding="utf-8"))
    assert not profile.exists()
    assert not marker.exists()
