"""Registros da fila de embedding e dos jobs de ingestão do RAG."""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict


class RagJobMeta(TypedDict, total=False):
    """Preparação de um job de ingestão acompanhada em memória."""

    workspace_id: str
    path: str
    total_chunks: int | None
    enqueue_done: bool
    enqueue_error: str | None
    preparation_unknown: bool


class IngestionJob(TypedDict):
    """Job de ingestão persistido, sem o texto dos documentos."""

    job_id: str
    workspace_id: str
    path: str
    total_chunks: int | None
    enqueue_done: bool
    enqueue_error: str | None


class EmbeddingFailure(TypedDict):
    """Item que falhou ou foi para a fila de mensagens mortas."""

    queue_id: str
    collection: str
    status: str
    attempt_count: int
    error: str


class IngestionResult(TypedDict):
    """Pasta varrida e enfileirada para indexação."""

    job_id: str
    total_files: int
    total_chunks: int
    status: Literal["no_files", "enqueued"]


class WorkerQueue(Protocol):
    """Operações que o worker de embedding usa em qualquer fila."""

    async def mark_processing(self, queue_id: str) -> None: ...

    async def mark_retry(self, queue_id: str) -> None: ...

    async def mark_success(self, queue_id: str) -> None: ...

    async def mark_failed(self, queue_id: str, reason: str, /) -> None: ...

    async def mark_dlq(self, queue_id: str, reason: str, /) -> None: ...

    async def archive_pending(self, reason: str) -> int: ...
