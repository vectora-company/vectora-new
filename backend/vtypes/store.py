"""Operações do store de memórias que o contexto de execução oferece."""

from __future__ import annotations

from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from backend.persistence.native.store import Item, SearchItem
    from backend.vtypes.contracts import JsonObject


class AgentStore(Protocol):
    """Leitura, escrita e busca por namespace, comum a SQLite e Postgres."""

    async def aget(self, namespace: tuple[str, ...], key: str) -> Item | None: ...

    async def aput(
        self, namespace: tuple[str, ...], key: str, value: JsonObject
    ) -> None: ...

    async def adelete(self, namespace: tuple[str, ...], key: str) -> None: ...

    async def asearch(
        self,
        namespace: tuple[str, ...],
        *,
        query: str | None = None,
        limit: int = 10,
        include_expired: bool = False,
    ) -> list[SearchItem]: ...


class EmbeddingIndex(TypedDict):
    """Busca semântica do store: dimensão, função de embedding e campos."""

    dims: int
    embed: Callable[[list[str]], Awaitable[list[list[float]]]]
    fields: NotRequired[list[str]]
