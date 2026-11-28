"""Contrato dos modelos de embedding usados por RAG e memórias."""

from __future__ import annotations

from typing import Protocol


class EmbeddingModel(Protocol):
    """Gera vetores para documentos e consultas, em modo síncrono e assíncrono."""

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def aembed_query(self, text: str) -> list[float]: ...
