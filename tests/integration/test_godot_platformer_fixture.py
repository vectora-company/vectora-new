"""Executa o fixture real de gameplay com uma instalação Godot 4.6."""

from __future__ import annotations

import contextlib
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
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    else:
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                check=False,
                capture_output=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()


def _run_godot(executable: str, project: Path, arguments: list[str]) -> tuple[int, str]:
    process = subprocess.Popen(
        [executable, *arguments, "--path", str(project)],
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
    return process.returncode or 0, output


def _copy_project(parent: Path) -> Path:
    project = parent / "project"
    shutil.copytree(
        _PROJECT,
        project,
        ignore=shutil.ignore_patterns(".godot", "*.import"),
    )
    return project


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
        project = _copy_project(Path(temp))
        project_file = project / "project.godot"
        settings = project_file.read_text(encoding="utf-8")
        settings = settings.replace(
            'run/main_scene="res://scenes/main.tscn"',
            'run/main_scene="res://tests/gameplay_regression.tscn"',
        )
        assert 'run/main_scene="res://tests/gameplay_regression.tscn"' in settings
        project_file.write_text(settings, encoding="utf-8")

        exit_code, output = _run_godot(
            executable, project, ["--headless", "--quit-after", "600"]
        )

    assert exit_code == 0, output[-8000:]
    assert "GODOT_GAMEPLAY_REGRESSION_PASSED" in output, output[-8000:]
    assert "ERROR:" not in output, output[-8000:]


@pytest.mark.godot
def test_godot_errors_disappear_after_the_causal_files_are_repaired() -> None:
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

    with tempfile.TemporaryDirectory(prefix="vectora-godot-repair-") as temp:
        project = _copy_project(Path(temp))
        player_script = project / "scripts" / "player.gd"
        original_script = player_script.read_text(encoding="utf-8")
        broken_script = original_script.replace(
            "var health: int = 3", 'var health: int = "inválido"'
        )
        assert broken_script != original_script
        player_script.write_text(broken_script, encoding="utf-8")

        bullet_scene = project / "scenes" / "bullet.tscn"
        original_bullet = bullet_scene.read_text(encoding="utf-8")
        bullet_scene.unlink()

        failed_code, failed_output = _run_godot(
            executable, project, ["--headless", "--editor", "--quit"]
        )
        assert failed_code != 0 or "ERROR:" in failed_output, failed_output[-8000:]
        assert "player.gd" in failed_output or "bullet.tscn" in failed_output

        player_script.write_text(original_script, encoding="utf-8")
        bullet_scene.write_text(original_bullet, encoding="utf-8")

        import_code, import_output = _run_godot(
            executable, project, ["--headless", "--editor", "--quit"]
        )
        assert import_code == 0, import_output[-8000:]
        assert "ERROR:" not in import_output, import_output[-8000:]

        startup_code, startup_output = _run_godot(
            executable, project, ["--headless", "--quit-after", "2"]
        )
        assert startup_code == 0, startup_output[-8000:]
        assert "ERROR:" not in startup_output, startup_output[-8000:]
