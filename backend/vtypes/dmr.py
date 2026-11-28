"""Contratos do Docker Model Runner expostos pelo backend."""

from __future__ import annotations

from typing import Literal, TypedDict

from backend.vtypes.contracts import JsonObject

type DmrOperation = Literal["prepare", "start"]
type DmrJobPhase = Literal[
    "queued",
    "pulling",
    "configuring",
    "starting",
    "checking",
    "completed",
    "failed",
    "cancelled",
    "interrupted",
]
type DmrJobState = Literal[
    "queued", "running", "completed", "failed", "cancelled", "interrupted"
]


class DockerHostInfo(TypedDict, total=False):
    """Capacidades do host Docker obtidas de ``docker info``."""

    os: str | None
    architecture: str | None
    server_version: str | None
    cpus: int | None
    memory_bytes: int | None
    runtimes: list[str]
    gpu_backends: list[str]
    warnings: list[str]


class DmrCapabilities(TypedDict):
    """Recursos do host relevantes para executar modelos."""

    cpus: int | None
    memory_bytes: int | None
    runtimes: list[str]
    gpu_backends: list[str]
    warnings: list[str]


class DmrHttpStatus(TypedDict):
    """Estado do endpoint HTTP do runner, sem consultar o CLI Docker."""

    configured: bool
    base_url: str
    model: str
    reachable: bool
    contract: str | None
    models: list[str]
    managed_models: list[str]
    detail: str | None
    status_code: int | None


class DmrStatus(DmrHttpStatus):
    """Estado completo do runner, incluindo host e CLI Docker."""

    state: Literal["ready", "plugin_unavailable", "docker_unavailable", "stopped"]
    platform: str
    architecture: str
    backend: str | None
    capabilities: DmrCapabilities
    cli_available: bool
    cli_detail: str | None


class DmrModelList(TypedDict):
    """Catálogo anunciado pelo endpoint HTTP."""

    reachable: bool
    contract: str | None
    models: list[str]


class DmrOperationResult(TypedDict):
    """Resultado de uma operação do plugin sobre um modelo."""

    status: str
    reference: str
    output: str


class DmrStartResult(DmrStatus):
    """Estado do runner depois de um modelo ficar pronto."""

    status: str
    reference: str
    output: str


class DmrJobSummary(TypedDict):
    """Identificação e progresso de uma operação local."""

    id: str
    operation: DmrOperation
    reference: str
    context_size: int | None
    progress: int
    phase: DmrJobPhase
    status: DmrJobState


class DmrJobOutcome(DmrJobSummary):
    """Progresso acompanhado da saída recente e do erro."""

    output: str
    error: str | None


class DmrJobDetail(DmrJobOutcome):
    """Operação completa, com os metadados registrados."""

    metadata: JsonObject


class DmrJobList(TypedDict):
    """Operações persistidas."""

    jobs: list[DmrJobDetail]


class DmrCancellation(TypedDict):
    """Resultado do cancelamento de uma operação."""

    id: str
    status: DmrJobState
    error: str | None
