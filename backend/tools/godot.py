"""Validação headless de projetos Godot dentro do workspace confiável."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
from pathlib import Path

from pydantic import Field

from backend.tools.context import ToolContext
from backend.tools.fs import _confine, _require_local, _require_trust
from backend.tools.registry import ToolExtras, vtool

_VERSION_RE = re.compile(r"\b(\d+)\.(\d+)(?:\.(\d+))?")
_FEATURES_RE = re.compile(r"config/features\s*=\s*PackedStringArray\(([^)]*)\)")
_ERROR_LINE_RE = re.compile(r"(?:^|\s)(?:SCRIPT )?ERROR:", re.IGNORECASE)
_MAX_OUTPUT_CHARS = 8_000


def _version_family(value: str) -> str | None:
    match = _VERSION_RE.search(value)
    return f"{match.group(1)}.{match.group(2)}" if match else None


def _project_version(project_file: Path) -> str | None:
    try:
        text = project_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = _FEATURES_RE.search(text)
    if not match:
        return None
    for feature in re.findall(r'"([^\"]+)"', match.group(1)):
        family = _version_family(feature)
        if family:
            return family
    return None


async def _run_engine(
    executable: str, arguments: list[str], project_dir: Path, timeout: float
) -> tuple[int | None, str, bool]:
    try:
        if os.name != "nt":
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                cwd=project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                cwd=project_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
    except OSError as exc:
        return None, str(exc), False

    output_tail = bytearray()

    async def drain_output() -> None:
        if process.stdout is None:
            return
        while chunk := await process.stdout.read(4096):
            output_tail.extend(chunk)
            if len(output_tail) > _MAX_OUTPUT_CHARS:
                del output_tail[:-_MAX_OUTPUT_CHARS]

    reader = asyncio.create_task(drain_output())
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            pass
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        output = output_tail.decode("utf-8", errors="replace")
        return (
            process.returncode,
            (f"Godot excedeu o limite de {timeout}s.\n{output}")[-_MAX_OUTPUT_CHARS:],
            True,
        )
    except asyncio.CancelledError:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            process.kill()
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
        raise
    await reader
    text = output_tail.decode("utf-8", errors="replace")
    return process.returncode, text, False


@vtool(
    extras=ToolExtras(
        render_hint="terminal_output",
        category="filesystem",
        destructive=True,
        icon="gamepad-2",
        invalidates=["files", "git"],
    )
)
async def validate_godot_project(
    ctx: ToolContext,
    project_path: str = ".",
    expected_version: str | None = None,
    timeout_seconds: int = Field(default=120, ge=10, le=600),
) -> str:
    """Importa e inicia o projeto Godot por poucos frames, sem interface gráfica.

    Args:
        project_path: Diretório do projeto, relativo ou absoluto dentro do workspace.
        expected_version: Família esperada do Godot, por exemplo ``4.6``.
        timeout_seconds: Limite independente para cada etapa de validação.
    """
    if trust_error := _require_trust(ctx):
        return json.dumps({"status": "error", "message": trust_error})
    if local_error := _require_local(ctx):
        return json.dumps({"status": "remote_unsupported", "message": local_error})
    project_dir, path_error = _confine(project_path, ctx)
    if project_dir is None:
        return json.dumps({"status": "error", "message": path_error})
    project_file = project_dir / "project.godot"
    if not project_dir.is_dir() or not project_file.is_file():
        return json.dumps(
            {
                "status": "error",
                "message": f"project.godot não encontrado em {project_dir}.",
            }
        )

    configured_version = _project_version(project_file)
    requested_version = _version_family(expected_version or "")
    if expected_version and requested_version is None:
        return json.dumps(
            {"status": "error", "message": "expected_version deve ser major.minor."}
        )
    if (
        requested_version
        and configured_version
        and requested_version != configured_version
    ):
        return json.dumps(
            {
                "status": "error",
                "stage": "project_version",
                "configured_version": configured_version,
                "expected_version": requested_version,
                "message": "A versão declarada pelo projeto difere da solicitada.",
            }
        )

    configured_binary = os.environ.get("VECTORA_GODOT_EXECUTABLE", "").strip()
    executable = (
        shutil.which(configured_binary)
        if configured_binary
        else shutil.which("godot") or shutil.which("godot4")
    )
    if not executable:
        return json.dumps(
            {
                "status": "error",
                "stage": "engine_discovery",
                "code": "godot_unavailable",
                "message": "Godot não encontrado no PATH nem em VECTORA_GODOT_EXECUTABLE.",
            }
        )

    version_code, version_output, version_timeout = await _run_engine(
        executable, ["--headless", "--version"], project_dir, timeout_seconds
    )
    actual_version = _version_family(version_output)
    target_version = requested_version or configured_version
    if version_timeout or version_code != 0 or actual_version is None:
        return json.dumps(
            {
                "status": "error",
                "stage": "engine_version",
                "output": version_output,
                "message": "Não foi possível identificar a versão do Godot instalado.",
            },
            ensure_ascii=False,
        )
    if target_version and actual_version != target_version:
        return json.dumps(
            {
                "status": "error",
                "stage": "engine_version",
                "actual_version": actual_version,
                "expected_version": target_version,
                "message": "O executável não corresponde à versão esperada pelo projeto.",
            },
            ensure_ascii=False,
        )

    stages: list[dict[str, object]] = []
    for stage_name, arguments in (
        ("import", ["--headless", "--editor", "--path", str(project_dir), "--quit"]),
        (
            "startup",
            ["--headless", "--path", str(project_dir), "--quit-after", "2"],
        ),
    ):
        code, output, timed_out = await _run_engine(
            executable, arguments, project_dir, timeout_seconds
        )
        stage: dict[str, object] = {
            "stage": stage_name,
            "exit_code": code,
            "timed_out": timed_out,
            "output": output,
        }
        stages.append(stage)
        if timed_out or code != 0 or _ERROR_LINE_RE.search(output):
            return json.dumps(
                {
                    "status": "error",
                    "stage": stage_name,
                    "engine_version": actual_version,
                    "configured_version": configured_version,
                    "stages": stages,
                    "message": f"A etapa {stage_name} encontrou erros ou não terminou no prazo.",
                },
                ensure_ascii=False,
            )

    return json.dumps(
        {
            "status": "passed",
            "engine_version": actual_version,
            "configured_version": configured_version,
            "stages": stages,
            "message": (
                "Importação e inicialização headless concluídas sem erros. "
                "Isso não valida as interações de gameplay."
            ),
        },
        ensure_ascii=False,
    )
