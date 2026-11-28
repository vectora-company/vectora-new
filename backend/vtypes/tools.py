"""Contratos compartilhados pelas ferramentas."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import (
    TYPE_CHECKING,
    Literal,
    NotRequired,
    Protocol,
    TypedDict,
    runtime_checkable,
)

from backend.vtypes.contracts import JsonObject

if TYPE_CHECKING:
    import asyncio
    from types import GenericAlias, UnionType
    from typing import TypeAliasType


class OpenAiFunction(TypedDict):
    name: str
    description: str
    parameters: JsonObject


class OpenAiToolSchema(TypedDict):
    """Ferramenta no formato ``function`` aceito pelos clientes de chat."""

    type: Literal["function"]
    function: OpenAiFunction


type BackendResult = list[JsonObject] | Awaitable[list[JsonObject]]


@runtime_checkable
class InvokableBackend(Protocol):
    """Backend de busca chamado por ``invoke``, usado pelos testes."""

    def invoke(self, payload: JsonObject, /) -> BackendResult: ...


@runtime_checkable
class ExtractingBackend(Protocol):
    """Backend que extrai o conteúdo de URLs."""

    def extract(self, urls: list[str], *, extract_depth: str) -> BackendResult: ...


@runtime_checkable
class SearchingBackend(Protocol):
    """Backend que busca por texto."""

    def search(self, query: str, /) -> BackendResult: ...


class TerminalReadState(TypedDict):
    """Leitores em andamento de um comando interativo."""

    both: asyncio.Future[tuple[None, None]]
    last_activity: list[float]


class PendingTerminal(TypedDict):
    """Comando de terminal que ainda espera entrada."""

    proc: asyncio.subprocess.Process
    output_lines: list[str]
    read_state: TerminalReadState


class GitInfo(TypedDict):
    """Informações git de um diretório; só ``is_git_repo`` fora de um repositório."""

    is_git_repo: bool
    git_current_branch: NotRequired[str | None]
    git_remote: NotRequired[str | None]
    git_remotes: NotRequired[list[str]]


type Annotation = type | UnionType | GenericAlias | TypeAliasType
"""Anotação de tipo de um parâmetro, lida em tempo de execução."""
