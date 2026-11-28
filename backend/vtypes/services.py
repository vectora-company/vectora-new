"""Formas trocadas pelos serviços da aplicação."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict

from backend.vtypes.contracts import JsonObject, JsonValue, ToolOption

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from backend.tools.registry import ToolSpec
    from backend.vtypes.tools import OpenAiToolSchema

type ToolDefinitions = (
    Sequence[ToolSpec] | Sequence[OpenAiToolSchema] | Sequence[Mapping[str, JsonValue]]
)
"""Ferramentas enviadas ao modelo, registradas ou já em formato de schema."""


class ProfileChanges(TypedDict, total=False):
    """Campos alteráveis de um perfil de agente."""

    name: str
    title: str
    icon: str
    color: str
    instruction_path: str | None
    tool_scope: list[str]
    model_override: str | None
    budget_cents: int | None
    status: str


class ToolUsageCount(TypedDict):
    """Quantidade de turnos que usaram uma ferramenta."""

    name: str
    count: int


class UsageInsight(TypedDict):
    """Agregado de uso de uma janela, sem eventos brutos."""

    window_weeks: int
    window_start: str
    window_end: str
    event_count: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_cents: float | None
    unknown_cost_events: int
    most_used_model: str | None
    tools: list[ToolUsageCount]


class RagSettings(TypedDict):
    """Settings de RAG ajustáveis em tempo de execução."""

    reranker_enabled: bool
    reranker_top_k: int
    rerank_model: str
    embed_model: str
    ingest_file_types: list[str]


class ServiceStartup(TypedDict):
    """Auto-start de um serviço self-hosted do modo completo."""

    self_hosted: bool
    start_command: str | None


class IgnoredImport(TypedDict):
    """Entrada recusada na prévia de uma configuração externa."""

    reason: str
    name: NotRequired[str]


class McpServerPreview(TypedDict):
    """Servidor MCP reconhecido, sem valores de ambiente."""

    name: str
    transport: Literal["stdio", "http"]
    command: str
    args: list[str]
    url: str
    env_vars: list[str]


class SkillPreview(TypedDict):
    """Skill reconhecida na prévia."""

    id: str
    name: str
    description: str
    source: str


class McpImportPreview(TypedDict):
    valid: list[McpServerPreview]
    ignored: list[IgnoredImport]


class SkillImportPreview(TypedDict):
    valid: list[SkillPreview]
    ignored: list[IgnoredImport]


class PendingInterrupt(TypedDict):
    """Aprovação pendente usada para reidratar o painel de HITL."""

    tool_name: str
    args: JsonObject
    interrupt_id: str
    options: list[ToolOption]
    priority: int
    expires_at: str | None


class CuratedModel(TypedDict):
    """Modelo mantido à mão para providers sem catálogo declarativo."""

    provider: str
    id: str
    capabilities: list[str]


class ModelEntry(TypedDict):
    """Modelo anunciado pelo catálogo unificado."""

    id: str
    model: str
    name: str
    provider: str
    capabilities: list[str]
    capabilities_known: bool
    source: str
    confidence: str
    available: bool
    executable_capabilities: list[str]


class ModelCatalogCache(TypedDict):
    updated: float
    entries: list[ModelEntry]


class LimitSetting(TypedDict):
    """Valor efetivo de um limite de mídia e a faixa aceita."""

    value: int
    default: int
    min: int
    max: int


class SttModelState(TypedDict):
    id: str
    cached: bool


class SttEngines(TypedDict):
    """Motores de transcrição detectados."""

    embedded: bool
    embedded_model: str
    apple_speech: bool
    system: bool
    system_kind: str
    language: str


class SttSettings(TypedDict):
    """Configuração efetiva de transcrição local."""

    model: str
    language: str
    system_command: str
    system_model: str
    models: list[SttModelState]
    languages: list[str]
    engines: SttEngines


class ToolImage(TypedDict):
    """Imagem que o resultado de uma ferramenta pede para enviar ao modelo."""

    asset_id: str
    name: str
    mime_type: str
    size_bytes: int


class SavedToolImage(TypedDict):
    url: str
    path: str
    bytes: int
    model_image: ToolImage


class UsageWindow(TypedDict):
    """Consumo dentro de uma janela de tempo."""

    used: int
    limit: int
    remaining: int
    window_seconds: int
    reset_in_seconds: float


class UsageWindows(UsageWindow):
    """Janela curta no topo, mais as janelas de cinco horas e semanal."""

    five_hour: UsageWindow
    weekly: UsageWindow


class StoreEntry(Protocol):
    """Item de um store com chave e valor."""

    @property
    def key(self) -> str: ...

    @property
    def value(self) -> Mapping[str, JsonValue]: ...
