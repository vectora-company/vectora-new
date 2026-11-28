"""Sugestão de título e descrição de commit a partir do diff real.

Lê as alterações dos arquivos escolhidos (ou de todos, sem escolha), pede ao
modelo uma mensagem no estilo dos commits recentes do repositório e, quando o
modelo não está disponível ou responde algo inválido, cai numa heurística
determinística baseada no diff — nunca num texto fixo.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from git import Repo

logger = logging.getLogger(__name__)

# Limites para o diff enviado ao modelo: o suficiente para entender a mudança,
# sem estourar o contexto nem a latência.
_MAX_PATHS = 500
_MAX_DIFF_CHARS_PER_FILE = 4_000
_MAX_DIFF_CHARS_TOTAL = 24_000
_UNTRACKED_PREVIEW_LINES = 40
_RECENT_SUBJECTS = 10
_MAX_TITLE_CHARS = 72
_MAX_DESCRIPTION_FILES = 8

_CONVENTIONAL = re.compile(
    r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(\([^)]*\))?!?:\s"
)
_GENERIC_SCOPES = {"src", "lib", "app", "components", "frontend", "backend", "tests"}


class CommitSuggestion(BaseModel):
    """Rascunho editável de título e descrição para um novo commit."""

    title: str
    description: str = ""


@dataclass
class ChangedFile:
    path: str
    status: (
        str  # A (novo), M (modificado), D (removido), R (renomeado), ? (não rastreado)
    )
    additions: int = 0
    deletions: int = 0
    diff: str = ""


@dataclass
class ChangeContext:
    files: list[ChangedFile] = field(default_factory=list)
    recent_subjects: list[str] = field(default_factory=list)

    @property
    def uses_conventional_commits(self) -> bool:
        if not self.recent_subjects:
            return True
        hits = sum(1 for s in self.recent_subjects if _CONVENTIONAL.match(s))
        return hits * 2 >= len(self.recent_subjects)


# ---------------------------------------------------------------------------
# Coleta
# ---------------------------------------------------------------------------


def _clean_paths(paths: Iterable[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in paths or []:
        path = raw.strip().replace("\\", "/")
        # Só caminhos relativos ao repositório: nunca absolutos nem com "..".
        if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
            continue
        cleaned.append(path)
        if len(cleaned) >= _MAX_PATHS:
            break
    return cleaned


def _numstat(
    repo: Repo, base: list[str], pathspec: list[str]
) -> dict[str, tuple[int, int]]:
    stats: dict[str, tuple[int, int]] = {}
    try:
        out = repo.git.diff(*base, "--numstat", "--", *pathspec)
    except Exception:
        return stats
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed, path = parts[0], parts[1], parts[-1]
        stats[path] = (
            int(added) if added.isdigit() else 0,
            int(removed) if removed.isdigit() else 0,
        )
    return stats


def _status_map(repo: Repo, base: list[str], pathspec: list[str]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    try:
        out = repo.git.diff(*base, "--name-status", "--", *pathspec)
    except Exception:
        return statuses
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            statuses[parts[-1]] = parts[0][:1]
    return statuses


def _split_diff_by_file(diff: str) -> dict[str, str]:
    chunks: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                chunks[current] = "\n".join(buffer)
            match = re.search(r" b/(.+)$", line)
            current = match.group(1) if match else None
            buffer = [line]
        else:
            buffer.append(line)
    if current is not None:
        chunks[current] = "\n".join(buffer)
    return chunks


def collect_change_context(
    repo: Repo, paths: Iterable[str] | None = None
) -> ChangeContext:
    """Junta os arquivos alterados (staged, unstaged e não rastreados) e seus diffs."""
    pathspec = _clean_paths(paths)
    wanted = set(pathspec)
    has_head = repo.head.is_valid()
    base = ["HEAD"] if has_head else ["--cached"]

    statuses = _status_map(repo, base, pathspec)
    stats = _numstat(repo, base, pathspec)
    try:
        raw_diff = repo.git.diff(*base, "--unified=2", "--no-color", "--", *pathspec)
    except Exception:
        raw_diff = ""
    diffs = _split_diff_by_file(raw_diff)

    files: list[ChangedFile] = []
    for path, status in sorted(statuses.items()):
        added, removed = stats.get(path, (0, 0))
        files.append(
            ChangedFile(
                path=path,
                status=status,
                additions=added,
                deletions=removed,
                diff=diffs.get(path, "")[:_MAX_DIFF_CHARS_PER_FILE],
            )
        )

    known = {f.path for f in files}
    for path in repo.untracked_files:
        if not path or path in known or (wanted and path not in wanted):
            continue
        preview = ""
        try:
            with (Path(repo.working_tree_dir or "") / path).open(
                encoding="utf-8"
            ) as handle:
                lines = [next(handle, "") for _ in range(_UNTRACKED_PREVIEW_LINES)]
            preview = "".join(lines)
        except (OSError, UnicodeDecodeError):
            preview = ""
        files.append(
            ChangedFile(
                path=path,
                status="?",
                additions=preview.count("\n"),
                diff=preview[:_MAX_DIFF_CHARS_PER_FILE],
            )
        )

    recent: list[str] = []
    if has_head:
        try:
            recent = [
                str(c.message).strip().splitlines()[0]
                for c in repo.iter_commits(max_count=_RECENT_SUBJECTS)
                if c.message.strip()
            ]
        except Exception:
            recent = []
    return ChangeContext(files=files, recent_subjects=recent)


# ---------------------------------------------------------------------------
# Heurística (sem modelo)
# ---------------------------------------------------------------------------


def _is_test(path: str) -> bool:
    p = path.lower()
    return (
        "/tests/" in f"/{p}"
        or "/__tests__/" in f"/{p}"
        or ".test." in p
        or ".spec." in p
        or p.rsplit("/", 1)[-1].startswith("test_")
    )


def _is_docs(path: str) -> bool:
    p = path.lower()
    return p.endswith((".md", ".mdx", ".rst")) or p.startswith("docs/")


def _is_ci(path: str) -> bool:
    return path.startswith(".github/")


def _is_config(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return name in {
        "package.json",
        "pnpm-lock.yaml",
        "uv.lock",
        "pyproject.toml",
        "package-lock.json",
        ".gitignore",
        ".prettierignore",
    } or name.endswith((".lock", ".toml", ".yaml", ".yml", ".json"))


def _only_category(paths: list[str]) -> str | None:
    """Categoria única de todos os caminhos (docs, test, ci, chore) ou None."""
    for name, check in (
        ("docs", _is_docs),
        ("test", _is_test),
        ("ci", _is_ci),
    ):
        if all(check(p) for p in paths):
            return name
    if all(_is_config(p) or _is_docs(p) for p in paths):
        return "chore"
    return None


def infer_commit_type(files: list[ChangedFile]) -> str:
    if not files:
        return "chore"
    category = _only_category([f.path for f in files])
    if category:
        return category
    source = [f for f in files if not (_is_test(f.path) or _is_docs(f.path))]
    added = sum(f.additions for f in source)
    removed = sum(f.deletions for f in source)
    has_new = any(f.status in {"A", "?"} for f in source)
    has_tests = any(_is_test(f.path) for f in files)
    if has_new and added >= removed:
        return "feat"
    if removed > added * 1.2 and removed > 0:
        return "refactor"
    if has_tests and added + removed <= 80:
        return "fix"
    return "feat" if added > removed * 2 else "fix"


def infer_scope(files: list[ChangedFile]) -> str:
    """Último diretório em comum dos arquivos, quando diz alguma coisa."""
    dirs = [PurePosixPath(f.path).parent.parts for f in files]
    if not dirs:
        return ""
    common: list[str] = []
    for parts in zip(*dirs, strict=False):
        if len(set(parts)) != 1:
            break
        common.append(parts[0])
    for name in reversed(common):
        if name and name not in _GENERIC_SCOPES and not name.startswith("."):
            return name
    return ""


def _names_summary(files: list[ChangedFile]) -> str:
    names = [PurePosixPath(f.path).name for f in files]
    if len(names) <= 3:
        return ", ".join(names)
    return f"{', '.join(names[:2])} e mais {len(names) - 2} arquivos"


def heuristic_suggestion(ctx: ChangeContext) -> CommitSuggestion:
    """Mensagem baseada no que mudou (tipo, escopo, arquivos e contagens)."""
    if not ctx.files:
        return CommitSuggestion(title="", description="")
    commit_type = infer_commit_type(ctx.files)
    scope = infer_scope(ctx.files)
    verbs = {
        "feat": "adiciona",
        "fix": "corrige",
        "refactor": "reorganiza",
        "docs": "atualiza a documentação de",
        "test": "cobre com testes",
        "ci": "ajusta a automação de",
        "chore": "atualiza",
    }
    summary = f"{verbs.get(commit_type, 'atualiza')} {_names_summary(ctx.files)}"
    if ctx.uses_conventional_commits:
        prefix = f"{commit_type}({scope}): " if scope else f"{commit_type}: "
        title = f"{prefix}{summary}"
    else:
        title = summary[:1].upper() + summary[1:]
    if len(title) > _MAX_TITLE_CHARS:
        title = title[: _MAX_TITLE_CHARS - 1].rstrip() + "…"

    lines: list[str] = []
    for f in ctx.files[:_MAX_DESCRIPTION_FILES]:
        label = {"A": "novo", "?": "novo", "D": "removido", "R": "renomeado"}.get(
            f.status, "alterado"
        )
        lines.append(f"- {f.path} ({label}, +{f.additions} −{f.deletions})")
    if len(ctx.files) > _MAX_DESCRIPTION_FILES:
        lines.append(f"- e mais {len(ctx.files) - _MAX_DESCRIPTION_FILES} arquivos")
    return CommitSuggestion(title=title, description="\n".join(lines))


# ---------------------------------------------------------------------------
# Modelo
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = (
    "Você escreve mensagens de commit a partir de um diff. Descreva o QUE mudou "
    "e POR QUÊ, em termos do comportamento, não liste arquivos nem copie código. "
    "Título: uma linha, imperativo ou indicativo curto, até 72 caracteres, sem "
    "ponto final. Descrição: 1 a 4 linhas (ou tópicos curtos) com o que importa "
    "para quem revisa; pode ficar vazia se o título bastar. Escreva no mesmo "
    "idioma e no mesmo estilo dos commits recentes.\n\n"
    'Responda APENAS com JSON válido: {"title": "...", "description": "..."}'
)


def _build_user_prompt(ctx: ChangeContext) -> str:
    parts: list[str] = []
    if ctx.recent_subjects:
        parts.append("Commits recentes (siga este estilo):")
        parts.extend(f"- {s}" for s in ctx.recent_subjects)
        if ctx.uses_conventional_commits:
            parts.append(
                "O repositório usa Conventional Commits: comece o título com o tipo."
            )
    parts.append("\nArquivos alterados:")
    parts.extend(
        f"- {f.path} [{f.status}] +{f.additions} −{f.deletions}" for f in ctx.files
    )
    parts.append("\nDiff:")
    budget = _MAX_DIFF_CHARS_TOTAL
    for f in ctx.files:
        if budget <= 0:
            parts.append("(diff restante omitido)")
            break
        chunk = f.diff[:budget]
        budget -= len(chunk)
        if chunk:
            parts.append(chunk)
    return "\n".join(parts)


def _parse_model_answer(raw: str) -> CommitSuggestion | None:
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    try:
        data = json.loads(text)
    except ValueError:
        # Tolera texto em volta: pega o primeiro objeto JSON.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except ValueError:
            return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title", "")).strip().splitlines()[0:1]
    title_text = title[0].strip().rstrip(".") if title else ""
    if not title_text:
        return None
    if len(title_text) > _MAX_TITLE_CHARS:
        title_text = title_text[: _MAX_TITLE_CHARS - 1].rstrip() + "…"
    return CommitSuggestion(
        title=title_text, description=str(data.get("description", "")).strip()
    )


async def generate_commit_suggestion(ctx: ChangeContext) -> CommitSuggestion:
    """Pede ao modelo; sem modelo ou com resposta inválida, usa a heurística."""
    if not ctx.files:
        return CommitSuggestion(title="", description="")
    try:
        from backend.services.utils import load_native_llm
        from backend.vtypes.message import MessageRole, text_message

        llm = load_native_llm()
        result = await llm.agenerate(
            [
                text_message(MessageRole.SYSTEM, _SYSTEM_PROMPT),
                text_message(MessageRole.USER, _build_user_prompt(ctx)),
            ]
        )
        parsed = _parse_model_answer(result.text())
        if parsed is not None:
            return parsed
        logger.info("commit_message: resposta do modelo inválida; usando heurística")
    except Exception as exc:
        logger.info("commit_message: modelo indisponível (%s); usando heurística", exc)
    return heuristic_suggestion(ctx)
