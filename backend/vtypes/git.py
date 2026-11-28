"""Contratos Pydantic compartilhados pelo Git Workbench."""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class GitContract(BaseModel):
    """Base imutável dos contratos do Git Workbench."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)


class GitCommitSnapshot(GitContract):
    """Resumo de um commit exibido no histórico do workspace."""

    hash: str
    author: str
    date: str
    message: str


class GitStatusSnapshot(GitContract):
    """Estado atual do repositório e de uma operação Git em andamento."""

    status: Literal["ok", "error"]
    branch: str = ""
    clean: bool = True
    untracked: list[str] = Field(default_factory=list)
    modified: list[str] = Field(default_factory=list)
    staged: list[str] = Field(default_factory=list)
    ahead: int = 0
    behind: int = 0
    operation_in_progress: GitOperationSnapshot | None = None


class GitLogSnapshot(GitContract):
    """Página de commits retornada pelo histórico do repositório."""

    status: Literal["ok", "error"]
    branch: str = ""
    commits: list[GitCommitSnapshot] = Field(default_factory=list)
    message: str | None = None


class GitDiffSnapshot(GitContract):
    """Diff textual de uma referência do repositório."""

    status: Literal["ok", "error"]
    diff: str = ""
    message: str | None = None


class GitOperationSnapshot(GitContract):
    """Estado persistido de uma operação Git assíncrona."""

    operation_id: str
    workspace_id: str
    operation: str
    state: Literal["queued", "running", "succeeded", "failed"]
    phase: str
    progress: int = Field(ge=0, le=100)
    output: str = ""
    error_code: str | None = None
    error: str | None = None
    created_at: float
    finished_at: float | None = None


GitStatusSnapshot.model_rebuild()
