"""Contratos do documento JSON usado pelo Context Graph."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from backend.vtypes.contracts import JsonObject, JsonValue

GraphFileType = Literal["code", "document", "paper", "image", "rationale", "concept"]
GraphConfidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]
GraphRelation = Literal[
    "calls",
    "implements",
    "references",
    "cites",
    "conceptually_related_to",
    "shares_data_with",
]


class GraphNode(TypedDict, total=False):
    """Entidade indexada pelo grafo, com campos opcionais para dados legados."""

    id: str
    label: str
    file_type: GraphFileType
    source_file: str
    source_location: str | None
    docstring: str
    rationale: str


class GraphEdge(TypedDict, total=False):
    """Relação orientada entre duas entidades do grafo."""

    source: str
    target: str
    relation: GraphRelation
    confidence: GraphConfidence
    confidence_score: float
    source_file: str
    source_location: str | None
    weight: float


class GraphHyperedge(TypedDict, total=False):
    """Relação que envolve três ou mais nós simultaneamente."""

    id: str
    label: str
    nodes: list[str]
    relation: Literal["participate_in", "implement", "form"]
    confidence: Literal["EXTRACTED", "INFERRED"]
    confidence_score: float
    source_file: str


class GraphDocument(TypedDict, total=False):
    """Documento completo persistido em ``graph.json``."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    hyperedges: list[GraphHyperedge]
    input_tokens: int
    output_tokens: int


class SymbolCandidateRecord(TypedDict):
    """Definição de código que corresponde exatamente a uma busca."""

    id: str
    label: str
    source_file: str
    source_location: str
    community: JsonValue


class SymbolResolution(TypedDict):
    """Resultado da busca exata de um identificador no grafo."""

    status: Literal["resolved", "ambiguous", "not_found"]
    query: str
    candidates: list[SymbolCandidateRecord]
    total: int
    truncated: bool


class SemanticFragment(TypedDict):
    """Fragmento extraído pelo modelo a partir de um conjunto de arquivos."""

    nodes: list[JsonValue]
    edges: list[JsonValue]
    hyperedges: list[JsonValue]
    input_tokens: int
    output_tokens: int
    finish_reason: NotRequired[Literal["stop", "length", "error"]]


class ExtractionResult(TypedDict):
    """Nós e arestas extraídos deterministicamente de um ou mais arquivos."""

    nodes: list[JsonObject]
    edges: list[JsonObject]
    hyperedges: NotRequired[list[JsonValue]]
    raw_calls: NotRequired[list[JsonObject]]
    error: NotRequired[str]
    skipped: NotRequired[str]
    input_tokens: NotRequired[int]
    output_tokens: NotRequired[int]
    bash_sources: NotRequired[list[JsonObject]]
    swift_extensions: NotRequired[list[JsonObject]]
    swift_type_table: NotRequired[JsonObject]
