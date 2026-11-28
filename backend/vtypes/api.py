"""Respostas das rotas que não têm um modelo Pydantic próprio."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from backend.vtypes.contracts import JsonObject
from backend.vtypes.services import ModelEntry
from backend.vtypes.storage import BackupCategoryResult


class SystemInfo(TypedDict):
    version: str
    build_version: str
    python_version: str
    platform: str
    architecture: str


class BrowserTabState(TypedDict):
    """Estado de uma aba do navegador remoto."""

    url: str
    title: str
    loading: bool
    can_go_back: bool
    can_go_forward: bool


class BackupPreviewPayload(TypedDict):
    """Prévia de um arquivo de backup antes da importação."""

    version: int
    app_version: str
    size_bytes: int
    categories: dict[str, int]
    compatible: bool
    storage_mode: str
    results: dict[str, BackupCategoryResult]


class Integration(TypedDict):
    """Integração oferecida pela interface."""

    id: str
    name: str
    env_var: str
    kind: str
    description: str
    docs_url: str
    icon: str
    env_var_aliases: NotRequired[list[str]]
    oauth_scopes: NotRequired[list[str]]
    parent: NotRequired[str]
    setup_hint: NotRequired[str]
    extra_vars: NotRequired[list[str]]


class ThreadSelector(TypedDict, total=False):
    """Agente usado no último turno de uma thread."""

    model: str
    chat_mode: bool
    workspace_id: str | None
    context_max_tokens: int | None
    context_compaction_enabled: bool
    reasoning_effort: str | None


class CollectionCount(TypedDict):
    name: str
    count: int | None


class CollectionList(TypedDict):
    """Coleções indexadas e a quantidade de documentos."""

    collections: list[CollectionCount]


class RagSearchResults(TypedDict):
    results: list[JsonObject]
    query: NotRequired[str]


class DeletedCollection(TypedDict):
    status: str
    name: str


class ProviderUsage(TypedDict):
    """Consumo de um provider exibido no medidor da interface."""

    provider: str
    label: str
    used: float | None
    limit: float | None
    remaining: float | None
    plan: str | None
    unit: str
    error: str | None
    key_limit: NotRequired[float | None]
    key_remaining: NotRequired[float | None]


class ProviderUsageList(TypedDict):
    providers: list[ProviderUsage]


class RemoteFileStat(TypedDict):
    """Metadados limitados de um caminho no host remoto."""

    exists: bool
    is_file: bool
    is_dir: bool
    size: int


class ModelCapabilities(TypedDict):
    """Capacidades conhecidas e os modelos que as anunciam."""

    capabilities: list[str]
    models: list[ModelEntry]
