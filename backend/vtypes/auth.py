"""Formas trocadas pela autenticação e pelas permissões."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from backend.rbac.auth import Role


class Principal(Protocol):
    """Usuário autenticado visto pelas regras de permissão."""

    @property
    def id(self) -> str: ...

    @property
    def role(self) -> str: ...


class InviteInfo(TypedDict):
    """Convite ainda utilizável."""

    email: str
    role: Role
    expires_at: str


class InviteRecord(InviteInfo):
    """Convite pendente como listado para a administração."""

    token_hash: str
    created_by: str
    created_at: str


class DatabaseHealth(TypedDict):
    ok: bool
    error: str | None


type PostgresValue = str | int | float | bool | datetime | None
type PostgresRow = dict[str, PostgresValue]


class PolicyVersionStore(Protocol):
    """Versões de política compartilhadas entre réplicas."""

    async def get_tool_policy_versions(self) -> dict[str, int]: ...

    async def bump_tool_policy_version(self, scope: str) -> int: ...
