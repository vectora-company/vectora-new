"""Contratos estruturais compartilhados pelo Browser Workbench e CDP tools."""

from __future__ import annotations

from typing import Literal, NotRequired

from pydantic import TypeAdapter
from typing_extensions import TypedDict

NetworkProfileName = Literal["offline", "slow-3g", "fast-3g"]
BrowserProfileKind = Literal["research", "authenticated"]


class BrowserTarget(TypedDict):
    """Identidade imutável do alvo Browser compartilhado por uma execução.

    ``view_id`` identifica a WebContentsView criada pelo Electron e
    ``navigation_generation`` invalida referências DOM obtidas antes de uma
    navegação. O perfil autenticado só deve ser usado após aprovação explícita.
    """

    workspace_id: str
    profile_id: str
    profile_kind: BrowserProfileKind
    view_id: str
    navigation_generation: int


class NetworkProfile(TypedDict):
    """Parâmetros aceitos por ``Network.emulateNetworkConditions``."""

    offline: bool
    latency: int
    downloadThroughput: int
    uploadThroughput: int


class BrowserTab(TypedDict):
    """Resumo de uma aba que pode ser serializado para a UI."""

    tab_id: str
    url: str
    active: bool
    title: NotRequired[str]


class BrowserConsoleEntry(TypedDict):
    """Linha normalizada do console de uma aba."""

    type: str
    text: str


class BrowserNetworkEntry(TypedDict):
    """Requisição/resposta resumida no buffer de observabilidade."""

    request_id: str
    url: str
    method: str
    resource_type: str
    status: int | Literal["failed"] | None
    error: NotRequired[str | None]


_BROWSER_TAB_ADAPTER = TypeAdapter(BrowserTab)
_BROWSER_TARGET_ADAPTER = TypeAdapter(BrowserTarget)
_BROWSER_CONSOLE_ENTRY_ADAPTER = TypeAdapter(BrowserConsoleEntry)
_BROWSER_NETWORK_ENTRY_ADAPTER = TypeAdapter(BrowserNetworkEntry)


def validate_browser_tab(value: BrowserTab) -> None:
    """Validate a tab payload before exposing it to a caller."""
    _BROWSER_TAB_ADAPTER.validate_python(value)


def validate_browser_target(value: BrowserTarget) -> None:
    """Validate the target identity before it crosses a tool boundary."""
    _BROWSER_TARGET_ADAPTER.validate_python(value)


def validate_browser_console_entry(value: BrowserConsoleEntry) -> None:
    """Validate a console event before storing it in the ring buffer."""
    _BROWSER_CONSOLE_ENTRY_ADAPTER.validate_python(value)


def validate_browser_network_entry(value: BrowserNetworkEntry) -> None:
    """Validate a network event before storing it in the ring buffer."""
    _BROWSER_NETWORK_ENTRY_ADAPTER.validate_python(value)


class AxValue(TypedDict):
    value: NotRequired[str | int | float | bool | None]


class AxNode(TypedDict):
    """Nó da árvore de acessibilidade do Chromium."""

    nodeId: str
    childIds: NotRequired[list[str]]
    ignored: NotRequired[bool]
    role: NotRequired[AxValue]
    name: NotRequired[AxValue]
    backendDOMNodeId: NotRequired[int]


class HeapSnapshotMeta(TypedDict):
    """Descrição das colunas de um heap snapshot do V8."""

    node_fields: list[str]
    node_types: list[list[str] | str]


class HeapSnapshotHeader(TypedDict):
    meta: HeapSnapshotMeta


class HeapSnapshot(TypedDict):
    """Partes de um heap snapshot usadas no resumo."""

    snapshot: HeapSnapshotHeader
    strings: list[str]
    nodes: list[int]


class HeapGroup(TypedDict):
    """Memória somada por construtor."""

    constructor: str
    total_size: int
    count: int
    outgoing_edges: NotRequired[int]


class HeapDelta(TypedDict):
    constructor: str
    size_delta: int
    count_delta: int


class LongTask(TypedDict):
    name: str
    start_ms: float
    duration_ms: float


class TraceAnalysis(TypedDict):
    """Leitura estruturada de um trace do Chromium."""

    total_duration_ms: float
    lcp_ms: float | None
    long_task_count: int
    top_long_tasks: list[LongTask]
