"""Candidatos reordenados pelos Decision Models."""

from __future__ import annotations

from typing import NotRequired, TypedDict

from backend.vtypes.contracts import JsonObject, JsonValue


class RerankCandidate(TypedDict, total=False):
    """O que o reranking lê de um candidato; outros campos seguem junto."""

    id: str
    snippet: str
    text: str


class RagHit(TypedDict):
    """Resultado de retrieval devolvido pela busca do RAG."""

    id: str
    score: float
    content: str
    metadata: JsonObject
    collection: str
    provenance: NotRequired[JsonObject]
    relevance_score: NotRequired[JsonValue]
    reranking_status: NotRequired[str]
