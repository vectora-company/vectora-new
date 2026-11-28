"""Contratos comuns dos backends de persistência e filas."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from backend.vtypes.contracts import JsonObject


class HealthResult(TypedDict):
    """Resultado uniforme de health checks de qualquer backend."""

    ok: bool
    error: str | None


class QueueTask(TypedDict, total=False):
    """Envelope mínimo de uma tarefa de fila persistida."""

    id: str
    kind: str
    payload: JsonObject
    attempts: int
    available_at: str
    error: NotRequired[str | None]


class StorageHealthEntry(TypedDict, total=False):
    """Linha do health check agregado: um backend ou o resumo de configuração."""

    ok: bool | None
    error: str | None
    internal: bool
    tables: list[str]
    collections: list[str]
    storage_mode: str
    postgres_configured: bool
    redis_configured: bool
    qdrant_configured: bool


class RecipeHealth(TypedDict):
    """Smoke test de um provedor gerenciado."""

    ok: bool
    error: NotRequired[str]
    pg_version: NotRequired[str]
    version: NotRequired[str]
    pgvector: NotRequired[bool]
    tables: NotRequired[list[str]]
    collections: NotRequired[list[str]]


class QdrantCloudConfig(TypedDict):
    """Configuração de um cluster Qdrant Cloud."""

    url: str
    api_key: str
    collections: list[str]
    retrieval_mode: str


class BackupCategoryResult(TypedDict):
    """Resultado da restauração de uma categoria do backup."""

    status: Literal["imported", "skipped"]
    count: int
    skipped: NotRequired[int]


class TableMigration(TypedDict):
    """Linhas copiadas e ignoradas de uma tabela."""

    rows: int
    skipped: int


class PostgresMigration(TypedDict):
    """Resultado da migração SQLite para Postgres."""

    tables: dict[str, TableMigration]
    total_rows: int
    dry_run: bool


class VectorMigration(TypedDict):
    """Resultado da migração de uma coleção vetorial."""

    collection: str
    total: int
    upserted: int
    dry_run: NotRequired[bool]
    error: NotRequired[str]


class MemoryMigration(TypedDict):
    """Resultado da migração das memórias para o store nativo."""

    total: int
    migrated: int
    skipped: int
    dry_run: bool


class SchemaPlan(TypedDict):
    """Mudança de schema pendente, sem expor o SQL."""

    checksum: str
    applied: bool
    drift: bool
    statement_count: int
    will_apply: bool
