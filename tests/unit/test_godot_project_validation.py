"""Validação de versão, importação e inicialização de projetos Godot."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from backend.tools import godot
from backend.tools.context import ToolContext


def _project(path: Path, feature: str = "4.6") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "project.godot").write_text(
        f'[application]\nconfig/features=PackedStringArray("{feature}")\n',
        encoding="utf-8",
    )
    return path


class _Stream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def read(self, _size: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        await asyncio.sleep(60)
        return b""


class _Process:
    pid = 987654
    returncode: int | None = None

    def __init__(
        self, *, chunks: list[bytes] | None = None, hang: bool = False
    ) -> None:
        self.stdout = _Stream(chunks or [])
        self.killed = False
        self.hang = hang

    async def wait(self) -> int:
        if self.killed:
            self.returncode = -9
            return self.returncode
        await asyncio.sleep(60 if self.hang else 0)
        self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def test_project_version_reads_godot_features_array(tmp_path: Path) -> None:
    project_file = _project(tmp_path) / "project.godot"

    assert godot._project_version(project_file) == "4.6"


async def test_run_engine_caps_output_and_returns_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process(chunks=[b"Godot 4.6.3\n", b""])
    monkeypatch.setattr(
        godot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )

    exit_code, output, timed_out = await godot._run_engine(
        "godot", ["--version"], tmp_path, timeout_seconds=1
    )

    assert exit_code == 0
    assert "Godot 4.6.3" in output
    assert timed_out is False


async def test_run_engine_kills_process_group_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _Process(hang=True)
    kill_group = Mock(side_effect=lambda *_args: process.kill())
    monkeypatch.setattr(
        godot.asyncio, "create_subprocess_exec", AsyncMock(return_value=process)
    )
    monkeypatch.setattr(godot.os, "killpg", kill_group, raising=False)

    exit_code, output, timed_out = await godot._run_engine(
        "godot", ["--headless"], tmp_path, timeout_seconds=0.01
    )

    assert timed_out is True
    assert exit_code == -9
    assert "limite de 0.01s" in output
    if godot.os.name != "nt":
        kill_group.assert_called_once_with(process.pid, godot.signal.SIGKILL)
    else:
        assert process.killed


async def test_windows_process_cleanup_uses_taskkill_for_descendants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(hang=True)
    taskkill = _Process()
    taskkill.wait = AsyncMock(return_value=0)  # type: ignore[method-assign]
    create_process = AsyncMock(return_value=taskkill)
    monkeypatch.setattr(godot.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(godot.shutil, "which", lambda _name: "C:/Windows/taskkill.exe")

    await godot._terminate_process_tree(process, platform_name="nt")

    create_process.assert_awaited_once_with(
        "C:/Windows/taskkill.exe",
        "/F",
        "/T",
        "/PID",
        str(process.pid),
        stdout=godot.asyncio.subprocess.DEVNULL,
        stderr=godot.asyncio.subprocess.DEVNULL,
    )
    assert process.killed


async def test_windows_process_cleanup_falls_back_when_taskkill_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _Process(hang=True)
    create_process = AsyncMock()
    monkeypatch.setattr(godot.asyncio, "create_subprocess_exec", create_process)
    monkeypatch.setattr(godot.shutil, "which", lambda _name: None)

    await godot._terminate_process_tree(process, platform_name="nt")

    create_process.assert_not_awaited()
    assert process.killed


async def test_validation_rejects_project_version_different_from_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _project(tmp_path / "game")
    monkeypatch.setattr(godot, "_require_trust", lambda _ctx: "")
    monkeypatch.setattr(godot, "_require_local", lambda _ctx: None)
    monkeypatch.setattr(godot, "_confine", lambda _path, _ctx: (project_dir, ""))
    runner = AsyncMock()
    monkeypatch.setattr(godot, "_run_engine", runner)

    result = json.loads(
        await godot.validate_godot_project(
            ToolContext(), expected_version="4.7", timeout_seconds=30
        )
    )

    assert result["status"] == "error"
    assert result["stage"] == "project_version"
    runner.assert_not_awaited()


async def test_validation_requires_matching_installed_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _project(tmp_path / "game")
    monkeypatch.setattr(godot, "_require_trust", lambda _ctx: "")
    monkeypatch.setattr(godot, "_require_local", lambda _ctx: None)
    monkeypatch.setattr(godot, "_confine", lambda _path, _ctx: (project_dir, ""))
    monkeypatch.setattr(godot.shutil, "which", lambda _name: "/usr/bin/godot")
    runner = AsyncMock(return_value=(0, "Godot Engine v4.7.2.stable.official", False))
    monkeypatch.setattr(godot, "_run_engine", runner)

    result = json.loads(
        await godot.validate_godot_project(ToolContext(), timeout_seconds=30)
    )

    assert result["status"] == "error"
    assert result["stage"] == "engine_version"
    assert result["actual_version"] == "4.7"
    runner.assert_awaited_once()


async def test_validation_reports_import_errors_even_when_engine_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _project(tmp_path / "game")
    monkeypatch.setattr(godot, "_require_trust", lambda _ctx: "")
    monkeypatch.setattr(godot, "_require_local", lambda _ctx: None)
    monkeypatch.setattr(godot, "_confine", lambda _path, _ctx: (project_dir, ""))
    monkeypatch.setattr(godot.shutil, "which", lambda _name: "/usr/bin/godot")
    runner = AsyncMock(
        side_effect=[
            (0, "Godot Engine v4.6.3.stable.official", False),
            (0, "ERROR: Failed parsing main scene", False),
        ]
    )
    monkeypatch.setattr(godot, "_run_engine", runner)

    result = json.loads(
        await godot.validate_godot_project(ToolContext(), timeout_seconds=30)
    )

    assert result["status"] == "error"
    assert result["stage"] == "import"
    assert len(result["stages"]) == 1
    assert runner.await_count == 2


async def test_validation_reports_completed_import_and_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_dir = _project(tmp_path / "game")
    monkeypatch.setattr(godot, "_require_trust", lambda _ctx: "")
    monkeypatch.setattr(godot, "_require_local", lambda _ctx: None)
    monkeypatch.setattr(godot, "_confine", lambda _path, _ctx: (project_dir, ""))
    monkeypatch.setattr(godot.shutil, "which", lambda _name: "/usr/bin/godot")
    runner = AsyncMock(
        side_effect=[
            (0, "Godot Engine v4.6.3.stable.official", False),
            (0, "", False),
            (0, "", False),
        ]
    )
    monkeypatch.setattr(godot, "_run_engine", runner)

    result = json.loads(
        await godot.validate_godot_project(
            ToolContext(), expected_version="4.6", timeout_seconds=30
        )
    )

    assert result["status"] == "passed"
    assert [stage["stage"] for stage in result["stages"]] == ["import", "startup"]
    assert runner.await_count == 3
