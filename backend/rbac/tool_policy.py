"""Política de tools por usuário — ABAC simples.

Cada usuário tem uma lista de tools desabilitadas, persistida em
``~/.vectora/tools/<user_id>.json``. Por padrão, todas as tools são permitidas
(allow-all). Admin/root e o próprio usuário podem desabilitar tools; a resolução
de toolset (``tool_resolver``) consulta esta política a cada request.

``GLOBAL_SCOPE`` é um "usuário" virtual: o kill-switch do admin
(``POST /admin/tools/{name}/toggle``) grava nele, reaproveitando o mesmo
arquivo/versionamento/pubsub — não é um usuário real e nunca aparece em
``/admin/users``. ``is_allowed`` nega se a tool estiver desabilitada
globalmente OU para o usuário específico.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Protocol

from backend.settings import settings

logger = logging.getLogger(__name__)

#: "Usuário" virtual para o disable global (admin kill-switch), aplicado a
#: todas as sessões independente de user_id — ver módulo docstring.
GLOBAL_SCOPE = "__global__"

#: Contador de versão por escopo — cache local da versão compartilhada no
#: banco. Só sobe: uma réplica nunca regride para um valor já visto.
_versions: dict[str, int] = {}


class PolicyVersionStore(Protocol):
    """Subconjunto do SessionStore que guarda as versões compartilhadas."""

    async def bump_tool_policy_version(self, scope: str) -> int: ...

    async def get_tool_policy_versions(self) -> dict[str, int]: ...


_version_store: PolicyVersionStore | None = None


def bind_version_store(store: PolicyVersionStore | None) -> None:
    """Liga as versões ao banco compartilhado; sem store, só o cache local."""
    global _version_store
    _version_store = store


def policy_version(user_id: str) -> int:
    """Versão atual da política de tools do usuário (muda em set_disabled)."""
    return _versions.get(user_id, 0)


def _advance(scope: str, version: int) -> None:
    if version > _versions.get(scope, 0):
        _versions[scope] = version


async def sync_persisted_versions(*, strict: bool = False) -> None:
    """Puxa do banco as versões de todos os escopos.

    No modo estrito, falha de leitura propaga ao executor e impede o efeito.
    Fora dele, a falha é registrada e mantém a última versão lida: a réplica
    continua com o cache, sem derrubar a execução de tools.
    """
    store = _version_store
    if store is None:
        return
    try:
        versions = await store.get_tool_policy_versions()
    except Exception:
        logger.warning(
            "tool_policy: versões compartilhadas indisponíveis", exc_info=True
        )
        if strict:
            raise
        return
    for scope, version in versions.items():
        _advance(scope, version)


async def bump_policy_version(scope: str) -> int:
    """Incrementa a versão do escopo no banco e no cache local."""
    store = _version_store
    local_next = _versions.get(scope, 0) + 1
    if store is not None:
        try:
            persisted = await store.bump_tool_policy_version(scope)
        except Exception:
            logger.warning("tool_policy: falha ao gravar versão", exc_info=True)
        else:
            local_next = max(local_next, persisted)
    _advance(scope, local_next)
    return _versions[scope]


def _policy_dir() -> Path:
    return settings.vectora_home / "tools"


def _user_file(user_id: str) -> Path:
    safe = (user_id or "local").replace("/", "_").replace("\\", "_")
    return _policy_dir() / f"{safe}.json"


def get_disabled(user_id: str) -> list[str]:
    """Lista de nomes de tools desabilitadas para o usuário."""
    path = _user_file(user_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        disabled = data.get("disabled", [])
        return [str(n) for n in disabled]
    except Exception:
        logger.warning("tool_policy: arquivo inválido para %s", user_id)
        return []


async def set_disabled(user_id: str, names: list[str]) -> None:
    """Define o conjunto de tools desabilitadas do usuário (substitui)."""
    path = _user_file(user_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"disabled": sorted(set(names))}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    version = await bump_policy_version(user_id)
    # Avisa as demais réplicas — no modo lite é um no-op local.
    from backend.persistence.kv import publish_soon

    publish_soon(
        "vectora:policy",
        json.dumps({"user_id": user_id, "version": version}),
    )


def apply_remote_version(user_id: str, version: int) -> None:
    """Aplica um bump de versão vindo de outra réplica (via cache_sync).

    A política em si vive em arquivo (relido a cada request); avançar a
    versão local basta para invalidar o LLM bindado, cuja chave a inclui.
    """
    _advance(user_id, version)


def is_allowed(user_id: str, tool_name: str) -> bool:
    """True se a tool está permitida (nem globalmente, nem por role, nem pelo usuário)."""
    if tool_name in set(get_disabled(GLOBAL_SCOPE)):
        return False
    if tool_name in role_denied(user_id):
        return False
    return tool_name not in set(get_disabled(user_id))


# ── Acesso por role (RBAC) ───────────────────────────────────────────────────
#
# O admin escolhe, por tool, quais roles podem usá-la. Tool sem entrada vale
# para todos. ``admin`` e ``root`` sempre têm acesso (não dá para trancar o
# próprio admin); as caixas de seleção cobrem os demais roles.

#: Roles que o admin pode liberar ou bloquear por tool.
GRANTABLE_ROLES: tuple[str, ...] = ("member", "viewer")
#: Roles com acesso garantido a qualquer tool (a menos do kill-switch global).
ALWAYS_ALLOWED_ROLES: frozenset[str] = frozenset({"root", "admin"})
_ROLE_ACCESS_FILE = "__role_access__.json"
_ROLE_CACHE_TTL_S = 30.0
_role_cache: dict[str, tuple[float, str | None]] = {}


def _role_access_path() -> Path:
    return _policy_dir() / _ROLE_ACCESS_FILE


def get_role_access() -> dict[str, list[str]]:
    """Mapa ``tool -> roles liberados``; tool ausente = liberada para todos."""
    path = _role_access_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get("tools", {})
        return {
            str(name): [str(r) for r in roles if str(r) in GRANTABLE_ROLES]
            for name, roles in raw.items()
            if isinstance(roles, list)
        }
    except Exception:
        logger.warning("tool_policy: arquivo de acesso por role inválido")
        return {}


async def set_role_access(access: dict[str, list[str] | None]) -> None:
    """Substitui o acesso por role. ``None`` (ou todos os roles) = sem restrição."""
    tools: dict[str, list[str]] = {}
    for name, roles in access.items():
        if roles is None:
            continue
        granted = sorted({r for r in roles if r in GRANTABLE_ROLES})
        if set(granted) == set(GRANTABLE_ROLES):
            continue
        tools[name] = granted
    path = _role_access_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"tools": tools}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Muda a visão de todos os usuários: o escopo global invalida os caches.
    version = await bump_policy_version(GLOBAL_SCOPE)
    from backend.persistence.kv import publish_soon

    publish_soon(
        "vectora:policy",
        json.dumps({"user_id": GLOBAL_SCOPE, "version": version}),
    )
    _role_cache.clear()


def user_role(user_id: str | None) -> str | None:
    """Role do usuário. Sessão local sem login vale como root; desconhecido = None."""
    if not user_id or user_id == "local":
        return "root"
    now = time.monotonic()
    cached = _role_cache.get(user_id)
    if cached is not None and now - cached[0] < _ROLE_CACHE_TTL_S:
        return cached[1]
    role: str | None = None
    try:
        conn = sqlite3.connect(
            str(settings.vectora_home / "checkpoints.db"), timeout=2.0
        )
        try:
            row = conn.execute(
                "SELECT role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        finally:
            conn.close()
        role = str(row[0]) if row else None
    except Exception:
        logger.debug("tool_policy: role de %s indisponível", user_id, exc_info=True)
    _role_cache[user_id] = (now, role)
    return role


def role_denied(user_id: str | None) -> set[str]:
    """Tools que o role do usuário não pode usar (fail-closed se o role é desconhecido)."""
    access = get_role_access()
    if not access:
        return set()
    role = user_role(user_id)
    if role in ALWAYS_ALLOWED_ROLES:
        return set()
    return {name for name, roles in access.items() if role not in roles}


def effective_disabled(user_id: str | None) -> set[str]:
    """União do disable global, do acesso por role e do usuário (ABAC)."""
    disabled = set(get_disabled(GLOBAL_SCOPE))
    disabled |= role_denied(user_id)
    if user_id:
        disabled |= set(get_disabled(user_id))
    return disabled
