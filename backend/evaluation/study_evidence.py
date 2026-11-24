"""Verificação local de referências citadas por estudos técnicos."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess  # nosec B404
from pathlib import Path, PurePosixPath

from backend.evaluation.studies import StudyEvidence, TechnicalStudy

_REPO_SOURCE = re.compile(
    r"^repo://([0-9a-f]{40})/([^#]+)#L([1-9][0-9]*)(?:-L([1-9][0-9]*))?$"
)
_RUN_SOURCE = re.compile(r"^run://([^#]+)#([A-Za-z0-9_-]{8,128})$")
_DATA_SOURCE = re.compile(r"^data://([^#]+)#sha256=([0-9a-f]{64})$")


def _safe_relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or not path.parts
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("referência contém caminho relativo inseguro")
    return path


def _git_source(root: Path, evidence: StudyEvidence) -> str:
    match = _REPO_SOURCE.fullmatch(evidence.source)
    if match is None:
        raise ValueError(
            f"{evidence.evidence_id}: fonte de código deve usar "
            "repo://<sha>/<arquivo>#L<início>-L<fim>"
        )
    revision, raw_path, first_line, last_line = match.groups()
    relative_path = _safe_relative_path(raw_path)
    git_executable = shutil.which("git")
    if git_executable is None:
        raise ValueError("git não está disponível para verificar referências de código")
    start = int(first_line)
    end = int(last_line or first_line)
    if end < start:
        raise ValueError(f"{evidence.evidence_id}: intervalo de linhas invertido")
    try:
        commit_check = subprocess.run(  # noqa: S603  # nosec B603
            [
                git_executable,
                "-C",
                str(root),
                "cat-file",
                "-e",
                f"{revision}^{{commit}}",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"{evidence.evidence_id}: verificação Git excedeu 10 s"
        ) from exc
    if commit_check.returncode != 0:
        raise ValueError(f"{evidence.evidence_id}: SHA Git não existe no clone local")
    try:
        source_check = subprocess.run(  # noqa: S603  # nosec B603
            [
                git_executable,
                "-C",
                str(root),
                "show",
                f"{revision}:{relative_path.as_posix()}",
            ],
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"{evidence.evidence_id}: leitura Git excedeu 10 s") from exc
    if source_check.returncode != 0:
        raise ValueError(f"{evidence.evidence_id}: arquivo não existe nesse SHA")
    line_count = len(source_check.stdout.splitlines())
    if end > line_count:
        raise ValueError(
            f"{evidence.evidence_id}: linha {end} excede o arquivo ({line_count})"
        )
    return f"{evidence.evidence_id}: {revision[:12]}:{relative_path} L{start}-L{end}"


def _resolve_inside(root: Path, raw_path: str) -> Path:
    relative_path = _safe_relative_path(raw_path)
    resolved_root = root.expanduser().resolve()
    resolved_path = (resolved_root / Path(*relative_path.parts)).resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise ValueError("referência escapa do diretório de evidências")
    return resolved_path


def _run_source(root: Path, evidence: StudyEvidence) -> str:
    match = _RUN_SOURCE.fullmatch(evidence.source)
    if match is None:
        raise ValueError(
            f"{evidence.evidence_id}: experimento deve usar "
            "run://<relatório.json>#<run_id>"
        )
    raw_path, expected_run_id = match.groups()
    report_path = _resolve_inside(root, raw_path)
    if report_path.suffix.lower() != ".json" or not report_path.is_file():
        raise ValueError(f"{evidence.evidence_id}: relatório JSON não encontrado")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{evidence.evidence_id}: relatório JSON inválido") from exc
    if not isinstance(report, dict):
        raise ValueError(f"{evidence.evidence_id}: relatório não é um objeto JSON")
    provenance = report.get("provenance")
    provenance_run_id = (
        provenance.get("run_id") if isinstance(provenance, dict) else None
    )
    actual_run_id = report.get("run_id") or provenance_run_id
    if actual_run_id != expected_run_id:
        raise ValueError(f"{evidence.evidence_id}: run_id não corresponde ao relatório")
    status = report.get("status")
    if not isinstance(status, str) or status not in {"passed", "replayed"}:
        raise ValueError(
            f"{evidence.evidence_id}: relatório não contém execução aprovada"
        )
    raw_evidence_level = report.get("evidence_level")
    allowed_evidence_levels = {
        "contract",
        "fixture_replay",
        "fixture_runner",
        "installed_app",
        "live_provider",
    }
    evidence_level = (
        raw_evidence_level
        if isinstance(raw_evidence_level, str)
        and raw_evidence_level in allowed_evidence_levels
        else "unspecified"
    )
    return (
        f"{evidence.evidence_id}: run_id {expected_run_id} confirmado "
        f"(status={status}, evidence_level={evidence_level})"
    )


def _data_source(root: Path, evidence: StudyEvidence) -> str:
    match = _DATA_SOURCE.fullmatch(evidence.source)
    if match is None:
        raise ValueError(
            f"{evidence.evidence_id}: dataset deve usar "
            "data://<arquivo>#sha256=<digest>"
        )
    raw_path, expected_digest = match.groups()
    data_path = _resolve_inside(root, raw_path)
    if not data_path.is_file():
        raise ValueError(f"{evidence.evidence_id}: dataset não encontrado")
    digest = hashlib.sha256(data_path.read_bytes()).hexdigest()
    if digest != expected_digest:
        raise ValueError(f"{evidence.evidence_id}: SHA-256 do dataset não corresponde")
    return f"{evidence.evidence_id}: dataset SHA-256 {digest} confirmado"


def verify_study_evidence(
    study: TechnicalStudy,
    *,
    repo_root: Path | str,
    evidence_root: Path | str,
) -> list[str]:
    """Confere referências locais de código, datasets e execuções citadas."""
    repository = Path(repo_root).expanduser().resolve()
    artifacts = Path(evidence_root).expanduser().resolve()
    verified: list[str] = []
    verifiers = {
        "source_code": lambda item: _git_source(repository, item),
        "reproduced_experiment": lambda item: _run_source(artifacts, item),
        "dataset": lambda item: _data_source(artifacts, item),
    }
    for item in study.evidence:
        verify = verifiers.get(item.kind)
        if verify is not None:
            verified.append(verify(item))
    return verified
