"""Tests para src/services/tool_policy.py — política de tools por usuário (S5).

Allow-all por padrão; admin/self podem desabilitar tools por usuário. Persistido
por arquivo, isolado entre usuários. Diretório base redirecionado para tmp_path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from backend.persistence.native.session_store import SessionStore
from backend.rbac import tool_policy
from backend.services.policy_compiler import PolicySnapshot
from backend.storage.sqlite.pool import AsyncConnectionPool


@pytest.fixture(autouse=True)
def iso_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(tool_policy, "_policy_dir", lambda: tmp_path / "tools")
    monkeypatch.setattr(tool_policy, "_versions", {})
    monkeypatch.setattr(tool_policy, "_version_store", None)


@pytest.fixture
async def version_store(tmp_path) -> AsyncIterator[SessionStore]:
    pool = AsyncConnectionPool(str(tmp_path / "sessions.db"), min_size=1, max_size=2)
    await pool.open()
    store = SessionStore(pool)
    await store.setup()
    tool_policy.bind_version_store(store)
    try:
        yield store
    finally:
        tool_policy.bind_version_store(None)
        await pool.close()


async def test_allow_all_by_default() -> None:
    assert tool_policy.is_allowed("u1", "terminal") is True
    assert tool_policy.get_disabled("u1") == []


async def test_disable_tool() -> None:
    await tool_policy.set_disabled("u1", ["terminal"])
    assert tool_policy.is_allowed("u1", "terminal") is False
    assert tool_policy.is_allowed("u1", "file_read") is True


async def test_get_disabled_returns_list() -> None:
    await tool_policy.set_disabled("u1", ["terminal", "file_write"])
    assert set(tool_policy.get_disabled("u1")) == {"terminal", "file_write"}


async def test_reenable_by_setting_empty() -> None:
    await tool_policy.set_disabled("u1", ["terminal"])
    await tool_policy.set_disabled("u1", [])
    assert tool_policy.is_allowed("u1", "terminal") is True


async def test_users_isolated() -> None:
    await tool_policy.set_disabled("a", ["terminal"])
    assert tool_policy.is_allowed("a", "terminal") is False
    assert tool_policy.is_allowed("b", "terminal") is True


async def test_persists_across_reads() -> None:
    await tool_policy.set_disabled("u1", ["grep"])
    # Releitura vai ao disco
    assert tool_policy.get_disabled("u1") == ["grep"]


# ---------------------------------------------------------------------------
# GLOBAL_SCOPE — kill-switch do admin (aplica a todas as sessões, mesmo local)
# ---------------------------------------------------------------------------


async def test_global_disable_blocks_any_user() -> None:
    await tool_policy.set_disabled(tool_policy.GLOBAL_SCOPE, ["terminal"])
    assert tool_policy.is_allowed("u1", "terminal") is False
    assert tool_policy.is_allowed("u2", "terminal") is False
    assert tool_policy.is_allowed("local", "terminal") is False


async def test_global_disable_does_not_block_other_tools() -> None:
    await tool_policy.set_disabled(tool_policy.GLOBAL_SCOPE, ["terminal"])
    assert tool_policy.is_allowed("u1", "file_read") is True


async def test_global_reenable_by_setting_empty() -> None:
    await tool_policy.set_disabled(tool_policy.GLOBAL_SCOPE, ["terminal"])
    await tool_policy.set_disabled(tool_policy.GLOBAL_SCOPE, [])
    assert tool_policy.is_allowed("u1", "terminal") is True


async def test_effective_disabled_unions_global_and_user() -> None:
    await tool_policy.set_disabled(tool_policy.GLOBAL_SCOPE, ["terminal"])
    await tool_policy.set_disabled("u1", ["grep"])
    assert tool_policy.effective_disabled("u1") == {"terminal", "grep"}
    # Outro usuário não herda o disable pessoal de u1, só o global.
    assert tool_policy.effective_disabled("u2") == {"terminal"}


async def test_effective_disabled_without_user_id_is_global_only() -> None:
    await tool_policy.set_disabled(tool_policy.GLOBAL_SCOPE, ["terminal"])
    assert tool_policy.effective_disabled(None) == {"terminal"}


async def test_effective_disabled_empty_by_default() -> None:
    assert tool_policy.effective_disabled("u1") == set()
    assert tool_policy.effective_disabled(None) == set()


async def test_versao_gravada_no_banco_sobrevive_ao_reinicio(
    version_store: SessionStore,
) -> None:
    await tool_policy.set_disabled("u1", ["terminal"])
    await tool_policy.set_disabled("u1", [])
    assert tool_policy.policy_version("u1") == 2
    assert await version_store.get_tool_policy_versions() == {"u1": 2}

    tool_policy._versions.clear()
    assert tool_policy.policy_version("u1") == 0
    await tool_policy.sync_persisted_versions()
    assert tool_policy.policy_version("u1") == 2


async def test_revogacao_feita_em_outra_replica_invalida_snapshot_existente(
    version_store: SessionStore,
) -> None:
    await tool_policy.set_disabled("u1", ["terminal"])
    snapshot = PolicySnapshot(
        user_id="u1",
        disabled_tools=frozenset({"terminal"}),
        user_policy_version=tool_policy.policy_version("u1"),
    )
    assert snapshot.is_revoked() is False

    # Outra réplica grava no banco sem que esta receba o pubsub.
    await version_store.bump_tool_policy_version("u1")
    assert snapshot.is_revoked() is False

    await tool_policy.sync_persisted_versions()
    assert snapshot.is_revoked() is True


async def test_falha_do_banco_mantem_ultima_versao_lida(
    version_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    await tool_policy.set_disabled("u1", ["terminal"])
    assert tool_policy.policy_version("u1") == 1

    async def _indisponivel[**P](*_args: P.args, **_kwargs: P.kwargs) -> dict[str, int]:
        msg = "banco fora do ar"
        raise ConnectionError(msg)

    monkeypatch.setattr(version_store, "get_tool_policy_versions", _indisponivel)
    await tool_policy.sync_persisted_versions()
    assert tool_policy.policy_version("u1") == 1


async def test_set_disabled_com_banco_fora_ainda_grava_arquivo_e_avanca_local(
    version_store: SessionStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _indisponivel[**P](*_args: P.args, **_kwargs: P.kwargs) -> int:
        msg = "banco fora do ar"
        raise ConnectionError(msg)

    monkeypatch.setattr(version_store, "bump_tool_policy_version", _indisponivel)
    await tool_policy.set_disabled("u1", ["terminal"])
    assert tool_policy.is_allowed("u1", "terminal") is False
    assert tool_policy.policy_version("u1") == 1


# ── Acesso por role (RBAC) ───────────────────────────────────────────────────


@pytest.fixture
def roles(monkeypatch):
    """Roles por usuário, sem banco: ``roles['u']`` define o role de ``u``."""
    table: dict[str, str] = {}
    monkeypatch.setattr(tool_policy, "user_role", lambda uid: table.get(uid or ""))
    return table


async def test_sem_restricao_todos_os_roles_usam(roles) -> None:
    roles["m"] = "member"
    assert tool_policy.is_allowed("m", "terminal") is True


async def test_tool_liberada_so_para_member_bloqueia_viewer(roles) -> None:
    roles.update({"m": "member", "v": "viewer"})
    await tool_policy.set_role_access({"terminal": ["member"]})

    assert tool_policy.is_allowed("m", "terminal") is True
    assert tool_policy.is_allowed("v", "terminal") is False
    assert "terminal" in tool_policy.effective_disabled("v")
    assert "terminal" not in tool_policy.effective_disabled("m")


async def test_admin_e_root_sempre_tem_acesso(roles) -> None:
    roles.update({"a": "admin", "r": "root"})
    await tool_policy.set_role_access({"terminal": []})

    assert tool_policy.is_allowed("a", "terminal") is True
    assert tool_policy.is_allowed("r", "terminal") is True


async def test_role_desconhecido_nao_usa_tool_restrita(roles) -> None:
    await tool_policy.set_role_access({"terminal": ["member"]})

    assert tool_policy.is_allowed("fantasma", "terminal") is False
    assert tool_policy.is_allowed("fantasma", "file_read") is True


async def test_liberar_todos_os_roles_remove_a_restricao(roles) -> None:
    roles["v"] = "viewer"
    await tool_policy.set_role_access({"terminal": ["member"]})
    await tool_policy.set_role_access({"terminal": ["member", "viewer"]})

    assert tool_policy.get_role_access() == {}
    assert tool_policy.is_allowed("v", "terminal") is True


async def test_usuario_pode_remover_o_proprio_acesso_mas_nao_recuperar_o_bloqueado(
    roles,
) -> None:
    roles["m"] = "member"
    await tool_policy.set_role_access({"terminal": []})
    await tool_policy.set_disabled("m", ["file_read"])

    # Zerar a própria lista não devolve o que o admin bloqueou.
    await tool_policy.set_disabled("m", [])
    assert tool_policy.is_allowed("m", "terminal") is False

    await tool_policy.set_disabled("m", ["file_read"])
    assert tool_policy.is_allowed("m", "file_read") is False


async def test_sessao_local_sem_login_vale_como_root() -> None:
    assert tool_policy.user_role(None) == "root"
    assert tool_policy.user_role("local") == "root"
    await tool_policy.set_role_access({"terminal": []})
    assert tool_policy.is_allowed("local", "terminal") is True


async def test_alteracao_de_role_sobe_a_versao_global() -> None:
    before = tool_policy.policy_version(tool_policy.GLOBAL_SCOPE)
    await tool_policy.set_role_access({"terminal": ["member"]})
    assert tool_policy.policy_version(tool_policy.GLOBAL_SCOPE) == before + 1
