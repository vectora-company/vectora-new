"""Resultados dos checkpoints de workspace usados pelo rewind."""

from __future__ import annotations

from typing import Literal, TypedDict


class CheckpointFailure(TypedDict):
    """Operação de checkpoint que não pôde ser concluída."""

    status: Literal["error"]
    message: str


class GitCheckpointCreated(TypedDict):
    """Commit solto que fotografa o worktree."""

    status: Literal["ok"]
    sha: str
    tree: str


class GitCheckpointRestored(TypedDict):
    """Worktree restaurado a partir de um commit."""

    status: Literal["ok"]
    sha: str


class GitCheckpointEntry(TypedDict):
    """Um checkpoint na lista de uma conversa."""

    sha: str
    message: str
    date: str


class GitCheckpointList(TypedDict):
    """Checkpoints de uma conversa, do mais recente ao mais antigo."""

    status: Literal["ok"]
    checkpoints: list[GitCheckpointEntry]


class SnapshotCreated(TypedDict):
    """Tarball gravado para um workspace sem git."""

    status: Literal["ok"]
    snapshot_path: str
    files_touched: list[str]


class SnapshotRestored(TypedDict):
    """Workspace restaurado a partir de um tarball."""

    status: Literal["ok"]
    snapshot_path: str
