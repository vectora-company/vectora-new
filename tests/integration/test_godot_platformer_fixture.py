"""Executa o fixture real de gameplay com uma instalação Godot 4.6."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest

_PROJECT = Path(__file__).parents[1] / "fixtures" / "godot-platformer"
_MAX_RUNTIME_SECONDS = 45


def _godot_executable() -> str | None:
    configured = os.environ.get("VECTORA_GODOT_EXECUTABLE", "").strip()
    if configured:
        return shutil.which(configured) or (
            configured if Path(configured).is_file() else None
        )
    return shutil.which("godot") or shutil.which("godot4")


def _terminate_tree(process: subprocess.Popen[str]) -> None:
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        process.kill()


@pytest.mark.godot
def test_platformer_imports_and_gameplay_regressions_pass() -> None:
    executable = _godot_executable()
    if executable is None:
        pytest.skip("Instale Godot 4.6 ou defina VECTORA_GODOT_EXECUTABLE")

    version = subprocess.run(
        [executable, "--headless", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout
    match = re.search(r"\b(\d+)\.(\d+)", version)
    assert match is not None and match.groups() == ("4", "6"), (
        f"O fixture exige Godot 4.6; encontrado: {version.strip()}"
    )

    with tempfile.TemporaryDirectory(prefix="vectora-godot-gameplay-") as temp:
        project = Path(temp) / "project"
        shutil.copytree(
            _PROJECT,
            project,
            ignore=shutil.ignore_patterns(".godot", "*.import"),
        )
        project_file = project / "project.godot"
        settings = project_file.read_text(encoding="utf-8")
        settings = settings.replace(
            'run/main_scene="res://scenes/main.tscn"',
            'run/main_scene="res://tests/gameplay_regression.tscn"',
        )
        assert 'run/main_scene="res://tests/gameplay_regression.tscn"' in settings
        project_file.write_text(settings, encoding="utf-8")

        process = subprocess.Popen(
            [executable, "--headless", "--path", str(project), "--quit-after", "600"],
            cwd=project,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=os.name != "nt",
        )
        try:
            output, _ = process.communicate(timeout=_MAX_RUNTIME_SECONDS)
        except subprocess.TimeoutExpired:
            _terminate_tree(process)
            output, _ = process.communicate(timeout=5)
            pytest.fail(
                f"Godot excedeu {_MAX_RUNTIME_SECONDS}s; processos encerrados.\n{output[-8000:]}"
            )

    assert process.returncode == 0, output[-8000:]
    assert "GODOT_GAMEPLAY_REGRESSION_PASSED" in output, output[-8000:]
    assert "ERROR:" not in output, output[-8000:]
