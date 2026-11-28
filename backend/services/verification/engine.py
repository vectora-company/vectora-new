"""Motor de verificação pós-execução e enforcement de quality gates."""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import functools
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from backend.contracts.models import (
    QualityGatePolicy,
    QualityGateReason,
    QualityGateRunResult,
    QualityGateSpec,
    VerificationOutcome,
    WorkspaceVerificationProfile,
)
from backend.services.verification.profile_store import get_verification_profile
from backend.services.verification.snapshot import compute_files_snapshot

if TYPE_CHECKING:
    from backend.vtypes.contracts import JsonObject

logger = logging.getLogger(__name__)


# Tamanho máximo do feedback de falha guardado na evidência e reenviado ao modelo.
FEEDBACK_MAX_CHARS = 4000


@dataclass
class VerificationResult:
    """Resultado consolidado do ciclo de verificação pós-execução."""

    passed: bool
    status: Literal["passed", "degraded", "failed"]
    snapshot_sha256: str
    file_hashes: dict[str, str] = field(default_factory=dict)
    gate_results: list[QualityGateRunResult] = field(default_factory=list)
    failure_feedback: str | None = None
    outcome: VerificationOutcome = "unverified"
    run_id: str | None = None
    policy_snapshot_id: str | None = None
    commit_sha: str | None = None
    artifact_refs: list[str] = field(default_factory=list)
    snapshot_after_sha256: str | None = None
    changed_during_verification: list[str] = field(default_factory=list)

    @property
    def detail(self) -> str:
        """Texto curto do desfecho, para o estado do AgentRun."""
        if self.outcome == "not_run":
            return "Nenhum quality gate foi executado: a execução não foi verificada."
        if self.passed:
            return "Verificação concluída com sucesso."
        return f"Falha nos quality gates: {self.failure_feedback or ''}"

    def to_evidence_entry(self) -> JsonObject:
        """Formata o resultado da verificação como evidência persistível no AgentRun."""
        return {
            "kind": "quality_gate_verification",
            "run_id": self.run_id,
            "policy_snapshot_id": self.policy_snapshot_id,
            "commit_sha": self.commit_sha,
            "artifact_refs": list(self.artifact_refs),
            "passed": self.passed,
            "status": self.status,
            "outcome": self.outcome,
            "snapshot_sha256": self.snapshot_sha256,
            "snapshot_after_sha256": self.snapshot_after_sha256,
            "changed_during_verification": list(self.changed_during_verification),
            # Keep the per-file material used to derive the snapshot alongside
            # the aggregate digest.  A digest alone cannot prove which files
            # were observed after a replay or a later workspace mutation.
            "file_hashes": dict(sorted(self.file_hashes.items())),
            # Texto que o agente recebe no turno seguinte para corrigir a falha.
            "failure_feedback": (self.failure_feedback or "")[:FEEDBACK_MAX_CHARS],
            "gates_evaluated": len(self.gate_results),
            "gate_results": [g.model_dump() for g in self.gate_results],
        }


def _matches_any_pattern(file_path: str, patterns: list[str]) -> bool:
    """Executa a operação  matches any pattern."""
    if not patterns:
        return True
    norm_path = file_path.replace("\\", "/")
    pure_p = PurePosixPath(norm_path)
    basename = pure_p.name
    for pat in patterns:
        if fnmatch.fnmatch(norm_path, pat) or fnmatch.fnmatch(basename, pat):
            return True
        if pure_p.match(pat):
            return True
        if pat.startswith("**/"):
            stripped = pat[3:]
            if (
                fnmatch.fnmatch(norm_path, stripped)
                or fnmatch.fnmatch(basename, stripped)
                or pure_p.match(stripped)
            ):
                return True
        if "/**/" in pat:
            collapsed = pat.replace("/**/", "/")
            if fnmatch.fnmatch(norm_path, collapsed) or pure_p.match(collapsed):
                return True
    return False


async def _execute_single_gate(
    gate: QualityGateSpec, cwd: Path
) -> QualityGateRunResult:
    """Executa um comando de quality gate de forma isolada e mede sua saída."""
    t0 = time.perf_counter()
    cmd = [c for c in gate.command if c]
    if not cmd:
        return QualityGateRunResult(
            gate_id=gate.id,
            passed=False,
            exit_code=1,
            output="No command specified for quality gate",
            duration_ms=0,
            reason="dispatch_error",
        )

    max_output_bytes = 10000
    output_buffer = bytearray()
    failure_reason: QualityGateReason | None = None

    async def _read_stream(stream: asyncio.StreamReader) -> None:
        """Executa a operação  read stream."""
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            if len(output_buffer) < max_output_bytes:
                remaining = max_output_bytes - len(output_buffer)
                output_buffer.extend(chunk[:remaining])

    try:
        proc = await asyncio.create_subprocess_exec(
            cmd[0],
            *cmd[1:],
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
        try:
            if proc.stdout is not None:
                await asyncio.wait_for(
                    asyncio.gather(_read_stream(proc.stdout), proc.wait()),
                    timeout=float(gate.timeout_seconds),
                )
            else:
                await asyncio.wait_for(proc.wait(), timeout=float(gate.timeout_seconds))
            exit_code = proc.returncode if proc.returncode is not None else 1
            output = output_buffer.decode("utf-8", errors="replace")
        except TimeoutError:
            if os.name != "nt" and hasattr(os, "killpg"):
                with contextlib.suppress(ProcessLookupError, Exception):
                    os.killpg(proc.pid, 9)
            else:
                with contextlib.suppress(ProcessLookupError, Exception):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            exit_code = -1
            output = f"Tempo limite de execução ({gate.timeout_seconds}s) excedido para o gate '{gate.id}'"
            failure_reason = "timeout"
        except asyncio.CancelledError:
            if os.name != "nt" and hasattr(os, "killpg"):
                with contextlib.suppress(ProcessLookupError, Exception):
                    os.killpg(proc.pid, 9)
            else:
                with contextlib.suppress(ProcessLookupError, Exception):
                    proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            raise
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        exit_code = 1
        output = f"Falha ao despachar comando do gate '{gate.id}': {exc}"
        failure_reason = "dispatch_error"

    duration_ms = int((time.perf_counter() - t0) * 1000)
    passed = exit_code == 0
    reason: QualityGateReason = (
        "passed" if passed else (failure_reason or "assertion_failed")
    )
    return QualityGateRunResult(
        gate_id=gate.id,
        passed=passed,
        exit_code=exit_code,
        output=output[:max_output_bytes],
        duration_ms=duration_ms,
        reason=reason,
    )


async def _is_git_working_tree_clean(root_dir: Path) -> tuple[bool, str]:
    """Verifica se o working tree git está limpo (sem modificações unstaged ou staged)."""
    git_dir = root_dir / ".git"
    if not git_dir.exists():
        return True, ""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "status",
            "--porcelain",
            cwd=str(root_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_data, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        status_output = (stdout_data or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode == 0 and not status_output:
            return True, ""
        return False, status_output
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        if proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
        raise
    except TimeoutError as exc:
        if proc is not None and proc.returncode is None:
            proc.kill()
        if proc is not None:
            with contextlib.suppress(Exception):
                await proc.wait()
        return False, f"Falha ao checar status do Git: {exc}"
    except Exception as exc:
        return False, f"Falha ao checar status do Git: {exc}"


async def _git_commit_sha(root_dir: Path) -> str | None:
    """Retorna o commit avaliado, sem transformar falta de Git em falha do gate."""
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=str(root_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        if proc.returncode != 0:
            return None
        return stdout.decode("ascii", errors="ignore").strip() or None
    except asyncio.CancelledError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        if proc is not None:
            with contextlib.suppress(Exception):
                await asyncio.shield(proc.wait())
        raise
    except TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
        if proc is not None:
            with contextlib.suppress(Exception):
                await proc.wait()
        return None
    except OSError:
        return None


async def run_verification(
    workspace_id: str | None,
    cwd: Path | str | None,
    *,
    files_touched: list[str] | None = None,
    profile: WorkspaceVerificationProfile | None = None,
    target_gate_ids: list[str] | set[str] | None = None,
    run_id: str | None = None,
    policy_snapshot_id: str | None = None,
) -> VerificationResult:
    """Executa o ciclo completo de verificação pós-execução.

    Calcula o snapshot SHA-256 dos arquivos, seleciona os quality gates aplicáveis,
    executa os comandos e determina se o status final é passed, degraded ou failed.
    """
    root_dir = Path(cwd) if cwd else Path.cwd()
    commit_sha = await _git_commit_sha(root_dir)
    prof = profile or get_verification_profile(workspace_id)

    # 1. Checagem de require_clean_git quando habilitado na verificação final
    git_unclean_reason: str | None = None
    if prof.enabled and prof.require_clean_git and target_gate_ids is None:
        is_clean, unclean_details = await _is_git_working_tree_clean(root_dir)
        if not is_clean:
            git_unclean_reason = f"Git working tree não está limpo:\n{unclean_details}"

    # 2. Filtragem dos gates relevantes aos arquivos alterados ou especificados
    target_set = set(target_gate_ids) if target_gate_ids is not None else None
    if prof.enabled and target_set is not None:
        existing_gate_ids = {g.id for g in prof.gates} if prof.gates else set()
        missing_gate_ids = target_set - existing_gate_ids
        if missing_gate_ids:
            snapshot_sha256, file_hashes = await asyncio.to_thread(
                compute_files_snapshot, root_dir, files=files_touched
            )
            return VerificationResult(
                passed=False,
                status="failed",
                outcome="failed",
                snapshot_sha256=snapshot_sha256,
                file_hashes=file_hashes,
                gate_results=[],
                failure_feedback=f"Quality gates solicitados não existem no perfil: {sorted(missing_gate_ids)}",
                run_id=run_id,
                policy_snapshot_id=policy_snapshot_id,
                commit_sha=commit_sha,
            )

    applicable_gates: list[QualityGateSpec] = (
        [
            g
            for g in prof.gates
            if (target_set is None or g.id in target_set)
            and (
                not g.file_patterns
                or not files_touched
                or any(_matches_any_pattern(f, g.file_patterns) for f in files_touched)
            )
        ]
        if prof.enabled and prof.gates
        else []
    )

    # 3. Snapshot do conteúdo que os gates vão validar (antes de rodá-los).
    # Sem arquivo tocado e sem gate aplicável não há o que validar: varrer e
    # hashear o workspace inteiro só atrasaria o fim do turno (numa conversa
    # sem edição isso levava minutos num repositório grande).
    nothing_to_verify = (
        not files_touched and not applicable_gates and git_unclean_reason is None
    )
    snapshot_sha256, file_hashes = await asyncio.to_thread(
        functools.partial(
            compute_files_snapshot,
            root_dir,
            files=files_touched,
            scan_workspace_when_empty=not nothing_to_verify,
        )
    )

    # 4. Execução dos quality gates aplicáveis
    gate_results: list[QualityGateRunResult] = []
    has_blocking_failure = git_unclean_reason is not None
    has_degrading_failure = False
    failure_feedback_parts: list[str] = []
    if git_unclean_reason:
        failure_feedback_parts.append(git_unclean_reason)

    for gate in applicable_gates:
        res = await _execute_single_gate(gate, root_dir)
        gate_results.append(res)
        if not res.passed:
            if gate.policy == "block":
                has_blocking_failure = True
            elif gate.policy == "degrade":
                has_degrading_failure = True

            if prof.inject_stack_trace_on_failure:
                failure_feedback_parts.append(
                    f"[QualityGate '{gate.id}' ({gate.name}) FALHOU com código {res.exit_code}]:\n{res.output}"
                )

    # 5. Snapshot depois dos gates: diverge se um gate reescreveu arquivos ou
    # se o workspace mudou durante a verificação.
    changed_during: list[str] = []
    snapshot_after_sha256 = snapshot_sha256
    if gate_results:
        snapshot_after_sha256, after_hashes = await asyncio.to_thread(
            compute_files_snapshot, root_dir, files=files_touched
        )
        if snapshot_after_sha256 != snapshot_sha256:
            changed_during = sorted(
                path
                for path in file_hashes.keys() | after_hashes.keys()
                if file_hashes.get(path) != after_hashes.get(path)
            )

    if git_unclean_reason is None and not gate_results:
        # Nenhum gate rodou (perfil desabilitado, sem gates ou sem gate
        # aplicável aos arquivos tocados): não há bloqueio, mas também não
        # houve verificação. ``degraded`` evita apresentar como aprovada uma
        # execução que nada validou.
        return VerificationResult(
            passed=True,
            status="degraded",
            outcome="not_run",
            snapshot_sha256=snapshot_sha256,
            file_hashes=file_hashes,
            snapshot_after_sha256=snapshot_after_sha256,
            gate_results=[],
            run_id=run_id,
            policy_snapshot_id=policy_snapshot_id,
            commit_sha=commit_sha,
        )

    has_infra_error = any(
        result.reason in ("dispatch_error", "timeout") for result in gate_results
    )
    status: Literal["passed", "degraded", "failed"]
    if has_infra_error:
        outcome: VerificationOutcome = "infra_error"
    elif has_blocking_failure:
        outcome = "blocked" if git_unclean_reason else "failed"
    elif has_degrading_failure:
        outcome = "degraded"
    else:
        outcome = "passed"

    if has_infra_error:
        # Um gate que não pôde ser despachado não aprovou o workspace. O
        # ``outcome`` preserva a causa de infraestrutura, enquanto o status
        # terminal permanece falho para impedir que a execução prossiga como
        # se tivesse sido verificada.
        status = "failed"
        overall_passed = False
    elif has_blocking_failure:
        status = "failed"
        overall_passed = False
    elif has_degrading_failure:
        status = "degraded"
        overall_passed = True
    else:
        status = "passed"
        overall_passed = True

    if changed_during and status == "passed":
        # Aprovar o que os gates viram não aprova o que ficou no workspace.
        status = "degraded"
        outcome = "degraded"
        failure_feedback_parts.append(
            "Arquivos mudaram durante a verificação: " + ", ".join(changed_during)
        )

    feedback = "\n\n".join(failure_feedback_parts) if failure_feedback_parts else None

    return VerificationResult(
        passed=overall_passed,
        status=status,
        outcome=outcome,
        snapshot_sha256=snapshot_sha256,
        file_hashes=file_hashes,
        snapshot_after_sha256=snapshot_after_sha256,
        changed_during_verification=changed_during,
        gate_results=gate_results,
        failure_feedback=feedback,
        run_id=run_id,
        policy_snapshot_id=policy_snapshot_id,
        commit_sha=commit_sha,
    )
