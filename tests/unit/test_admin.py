"""Tests — Root Admin Panel (P1+P2).

Verifica:
- handler admin existe com os endpoints esperados
- endpoints exigem role admin/root (decoradores aplicados)
- schemas de resposta têm estrutura correta
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiosqlite import Connection
from starlette.requests import Request

from backend.vtypes.contracts import JsonValue


class TestAdminHandlerExists:
    """src/api/handlers/admin.py deve existir com os endpoints esperados."""

    def test_module_exists(self) -> None:
        import backend.api.handlers.admin as mod

        assert mod is not None

    def test_router_exists(self) -> None:
        from backend.api.handlers.admin import router

        assert router is not None

    def test_list_users_route_registered(self) -> None:
        from backend.api.handlers.admin import router

        paths = [r.path for r in router.routes]  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        assert any("users" in p for p in paths)

    def test_system_info_route_registered(self) -> None:
        from backend.api.handlers.admin import router

        paths = [r.path for r in router.routes]  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        assert any("system" in p for p in paths)

    def test_config_route_registered(self) -> None:
        from backend.api.handlers.admin import router

        paths = [r.path for r in router.routes]  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        assert any("config" in p for p in paths)

    def test_tools_override_route_registered(self) -> None:
        from backend.api.handlers.admin import router

        paths = [r.path for r in router.routes]  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        assert any("tools" in p for p in paths)


class TestAdminRequiresAdminRole:
    """Endpoints admin devem checar permissão de role."""

    def test_require_admin_decorator_exists(self) -> None:
        from backend.api.handlers.admin import require_admin

        assert callable(require_admin)

    def test_require_admin_raises_for_member(self) -> None:
        """member não tem acesso a endpoints admin."""
        from unittest.mock import AsyncMock, MagicMock

        from backend.api.handlers.admin import require_admin

        mock_user = MagicMock()
        mock_user.role = "member"

        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            require_admin(mock_user)

    def test_require_admin_passes_for_root(self) -> None:
        from unittest.mock import MagicMock

        from backend.api.handlers.admin import require_admin

        mock_user = MagicMock()
        mock_user.role = "root"

        # Não deve levantar exceção
        result = require_admin(mock_user)
        assert result is None or result == mock_user

    def test_require_admin_passes_for_admin(self) -> None:
        from unittest.mock import MagicMock

        from backend.api.handlers.admin import require_admin

        mock_user = MagicMock()
        mock_user.role = "admin"

        result = require_admin(mock_user)
        assert result is None or result == mock_user


class TestAdminSystemInfo:
    """Estrutura do sistema info."""

    def test_system_info_fields_exist(self) -> None:
        from backend.api.handlers.admin import _build_system_info

        info = _build_system_info()
        assert "version" in info
        assert "build_version" in info
        assert "python_version" in info
        assert "platform" in info

    def test_build_version_falls_back_to_version_without_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sem VECTORA_BUILD_VERSION (dev local, sem build oficial), cai pra semver puro."""
        from backend.api.handlers.admin import _build_system_info

        monkeypatch.delenv("VECTORA_BUILD_VERSION", raising=False)
        info = _build_system_info()
        assert info["build_version"] == info["version"]

    def test_build_version_reads_env_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Com VECTORA_BUILD_VERSION setado (pipeline de release), usa o valor com hash."""
        from backend.api.handlers.admin import _build_system_info

        monkeypatch.setenv("VECTORA_BUILD_VERSION", "0.1.1.11325")
        info = _build_system_info()
        assert info["build_version"] == "0.1.1.11325"


class TestApiKeysMaskKey:
    """_mask_key oculta segredos mantendo prefixo e sufixo."""

    def test_mask_key_long(self) -> None:
        from backend.api.handlers.admin import _mask_key

        masked = _mask_key("AIzaSyDEADBEEF12345XYZ")
        assert masked.startswith("AIzaSy")
        assert masked.endswith("5XYZ")
        assert "•" in masked

    def test_mask_key_short(self) -> None:
        from backend.api.handlers.admin import _mask_key

        masked = _mask_key("abc")
        assert masked == "•••"

    def test_mask_key_empty(self) -> None:
        from backend.api.handlers.admin import _mask_key

        assert _mask_key("") == ""


class TestApiKeysEndpoints:
    """GET/PATCH/POST /admin/api-keys — contratos básicos."""

    @pytest.mark.asyncio
    async def test_get_api_keys_structure(self) -> None:
        import os
        from unittest.mock import MagicMock, patch

        request = MagicMock()
        request.state.user = MagicMock(role="root")

        with patch.dict(
            os.environ,
            {
                "GOOGLE_API_KEY": "AIzaSyXXXXLONGKEY123",
                "COHERE_API_KEY": "",
                "TAVILY_API_KEY": "tvly-secretkey987",
            },
        ):
            from backend.api.handlers.admin import get_api_keys

            result = await get_api_keys(request)

        assert "google" in result
        assert "cohere" in result
        assert "tavily" in result
        assert result["google"]["configured"] is True
        assert result["cohere"]["configured"] is False
        assert result["tavily"]["configured"] is True
        assert "AIzaSy" in result["google"]["masked"]

    @pytest.mark.asyncio
    async def test_get_api_keys_returns_masked_not_raw(self) -> None:
        import os
        from unittest.mock import MagicMock, patch

        request = MagicMock()
        request.state.user = MagicMock(role="root")

        raw = "AIzaSyDEADBEEFSECRET9999"
        with patch.dict(os.environ, {"GOOGLE_API_KEY": raw}):
            from backend.api.handlers.admin import get_api_keys

            result = await get_api_keys(request)

        assert result["google"]["masked"] != raw

    @pytest.mark.asyncio
    async def test_test_api_key_empty_returns_error(self) -> None:
        from unittest.mock import MagicMock

        from backend.api.handlers.admin import TestApiKeyBody, test_api_key

        request = MagicMock()
        request.state.user = MagicMock(role="root")
        body = TestApiKeyBody(provider="google", api_key="")
        result = await test_api_key(request, body)

        assert result["ok"] is False
        assert "vazia" in result["error"].lower() or result["error"]

    @pytest.mark.asyncio
    async def test_test_api_key_unknown_provider(self) -> None:
        from unittest.mock import MagicMock

        from backend.api.handlers.admin import TestApiKeyBody, test_api_key

        request = MagicMock()
        request.state.user = MagicMock(role="root")
        body = TestApiKeyBody(provider="openai_unsupported", api_key="sk-123")
        result = await test_api_key(request, body)

        assert result["ok"] is False

    @pytest.mark.asyncio
    async def test_patch_api_keys_calls_upsert(self) -> None:
        from unittest.mock import MagicMock, patch

        from backend.api.handlers.admin import PatchApiKeysBody, patch_api_keys

        request = MagicMock()
        request.state.user = MagicMock(role="root", id="u1")

        with (
            patch("backend.cli.keys.upsert_env_key") as mock_upsert,
            patch("backend.api.handlers.admin._env_file", return_value=MagicMock()),
        ):
            body = PatchApiKeysBody(
                google_api_key="AIzaSyNEW", cohere_api_key=None, tavily_api_key=None
            )
            result = await patch_api_keys(request, body)

        assert result["status"] == "updated"
        assert "GOOGLE_API_KEY" in result["updated"]
        assert mock_upsert.called

    @pytest.mark.asyncio
    async def test_get_api_keys_le_dos_fields_do_registry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leitura passa pelos fields do registry (EnvAdapter), não por
        os.environ direto — mesma fonte usada por CLI/outros handlers."""
        from unittest.mock import MagicMock

        from backend.config import registry

        captured: list[str] = []

        class _FakeAdapter:
            def __init__(self, env_var: str) -> None:
                self.env_var = env_var

            def get(self, key: str) -> JsonValue:
                captured.append(self.env_var)
                return "AIzaSyFAKE123"

            def set(self, key: str, value: JsonValue) -> None:
                pass

        adapters: dict[str, _FakeAdapter] = {
            "google_api_key": _FakeAdapter("GOOGLE_API_KEY"),
            "cohere_api_key": _FakeAdapter("COHERE_API_KEY"),
        }
        monkeypatch.setattr(
            registry,
            "_REGISTRY",
            {
                k: registry.SettingField(
                    key=k,
                    category="integrations",
                    cli_flag=f"--{k}",
                    description="d",
                    adapter=adapter,
                )
                for k, adapter in adapters.items()
            },
        )

        request = MagicMock()
        request.state.user = MagicMock(role="root")

        from backend.api.handlers.admin import get_api_keys

        result = await get_api_keys(request)

        assert set(captured) == {"GOOGLE_API_KEY", "COHERE_API_KEY"}
        assert result["google"]["configured"] is True
        assert "AIzaSy" in result["google"]["masked"]


class TestCreateInvite:
    """Convites da instância local respeitam o papel, não o tier Pro."""

    @pytest.mark.asyncio
    async def test_admin_free_pode_convidar_membro_local(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from backend.api.handlers.admin import CreateInviteBody, create_invite
        from backend.rbac import auth as auth_svc

        expires_at = "2026-11-12T00:00:00+00:00"
        create_invite_mock = AsyncMock(return_value=("invite-token", expires_at))
        monkeypatch.setattr(auth_svc, "create_invite", create_invite_mock)

        request = MagicMock()
        request.state.user = MagicMock(role="admin", id="u1")
        body = CreateInviteBody(role="member", email="novo@example.com")

        result = await create_invite(request, body)

        assert result.token == "invite-token"
        assert result.expires_at == expires_at
        create_invite_mock.assert_awaited_once_with(
            "u1", role="member", email="novo@example.com", ttl_hours=24
        )

    @pytest.mark.asyncio
    async def test_membro_sem_papel_admin_nao_pode_convidar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from backend.api.handlers.admin import CreateInviteBody, create_invite
        from backend.rbac import auth as auth_svc

        create_invite_mock = AsyncMock()
        monkeypatch.setattr(auth_svc, "create_invite", create_invite_mock)
        request = MagicMock()
        request.state.user = MagicMock(role="member", id="u1")

        with pytest.raises(Exception) as exc:
            await create_invite(
                request,
                CreateInviteBody(role="member", email="novo@example.com"),
            )

        assert exc.value.status_code == 403  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        create_invite_mock.assert_not_awaited()


class TestInviteLinkAndTypedResponses:
    """O link do convite abre o app onde ele é servido; as respostas são tipadas."""

    @staticmethod
    def _admin_request(base_url: str = "http://app.test:8080/") -> MagicMock:
        from unittest.mock import MagicMock

        request = MagicMock()
        request.base_url = base_url
        request.state.user = MagicMock(role="admin", id="u1")
        return request

    @pytest.mark.asyncio
    async def test_link_usa_o_endereco_de_onde_o_app_e_servido(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from backend.api.handlers.admin import CreateInviteBody, create_invite
        from backend.rbac import auth as auth_svc

        monkeypatch.delenv("VECTORA_FRONTEND_URL", raising=False)
        monkeypatch.setattr(
            auth_svc, "create_invite", AsyncMock(return_value=("tok", "2030-01-01"))
        )

        result = await create_invite(self._admin_request(), CreateInviteBody())

        assert result.url == "http://app.test:8080/auth/signup?invite=tok"

    @pytest.mark.asyncio
    async def test_frontend_url_definida_vence_o_endereco_da_requisicao(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from backend.api.handlers.admin import CreateInviteBody, create_invite
        from backend.rbac import auth as auth_svc

        monkeypatch.setenv("VECTORA_FRONTEND_URL", "http://localhost:3000/")
        monkeypatch.setattr(
            auth_svc, "create_invite", AsyncMock(return_value=("tok", "2030-01-01"))
        )

        result = await create_invite(self._admin_request(), CreateInviteBody())

        assert result.url == "http://localhost:3000/auth/signup?invite=tok"

    def test_validade_fora_de_1_a_720_horas_e_recusada(self) -> None:
        from pydantic import ValidationError

        from backend.api.handlers.admin import CreateInviteBody

        for hours in (0, 721, -5):
            with pytest.raises(ValidationError):
                CreateInviteBody(ttl_hours=hours)
        assert CreateInviteBody(ttl_hours=720).ttl_hours == 720

    @pytest.mark.asyncio
    async def test_papel_invalido_responde_400_sem_criar(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from backend.api.handlers.admin import CreateInviteBody, create_invite
        from backend.rbac import auth as auth_svc

        create_mock = AsyncMock()
        monkeypatch.setattr(auth_svc, "create_invite", create_mock)

        with pytest.raises(Exception) as exc:
            await create_invite(self._admin_request(), CreateInviteBody(role="root"))

        assert exc.value.status_code == 400  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        create_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_lista_devolve_convites_tipados_e_o_total(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from backend.api.handlers.admin import list_invites
        from backend.rbac import auth as auth_svc

        row = {
            "token_hash": "h1",
            "email": None,
            "role": "member",
            "created_by": "u1",
            "expires_at": "2030-01-02",
            "created_at": "2030-01-01",
        }
        monkeypatch.setattr(auth_svc, "list_invites", AsyncMock(return_value=[row]))

        result = await list_invites(self._admin_request())

        assert result.total == 1
        assert result.invites[0].token_hash == "h1"
        assert result.invites[0].email is None

    @pytest.mark.asyncio
    async def test_so_admin_lista_e_revoga(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from backend.api.handlers.admin import list_invites, revoke_invite
        from backend.rbac import auth as auth_svc

        list_mock = AsyncMock(return_value=[])
        revoke_mock = AsyncMock(return_value=True)
        monkeypatch.setattr(auth_svc, "list_invites", list_mock)
        monkeypatch.setattr(auth_svc, "revoke_invite", revoke_mock)
        member = MagicMock()
        member.state.user = MagicMock(role="member", id="u2")

        for call in (list_invites(member), revoke_invite(member, "h1")):
            with pytest.raises(Exception) as exc:
                await call
            assert exc.value.status_code == 403  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        list_mock.assert_not_awaited()
        revoke_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_revogar_devolve_o_hash_e_convite_inexistente_e_404(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from backend.api.handlers.admin import revoke_invite
        from backend.rbac import auth as auth_svc

        monkeypatch.setattr(auth_svc, "revoke_invite", AsyncMock(return_value=True))
        result = await revoke_invite(self._admin_request(), "h1")
        assert (result.status, result.token_hash) == ("revoked", "h1")

        monkeypatch.setattr(auth_svc, "revoke_invite", AsyncMock(return_value=False))
        with pytest.raises(Exception) as exc:
            await revoke_invite(self._admin_request(), "h2")
        assert exc.value.status_code == 404  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]


class TestToggleToolGlobal:
    """Kill-switch global de tools (/admin/tools, /admin/tools/{name}/toggle).

    Antes, o toggle não persistia nada (TODO em código) e list_tools_admin
    sempre devolvia enabled=True — um admin podia desligar uma tool
    "destructive" e ela continuar ativa pro agente. Estes testes cobrem a
    persistência real via tool_policy.GLOBAL_SCOPE e a leitura de volta.
    """

    @pytest.fixture(autouse=True)
    def iso_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from backend.rbac import tool_policy

        monkeypatch.setattr(tool_policy, "_policy_dir", lambda: tmp_path / "tools")

    @staticmethod
    def _admin_request() -> MagicMock:
        from unittest.mock import MagicMock

        request = MagicMock()
        request.state.user = MagicMock(role="admin", id="admin-1")
        return request

    @pytest.mark.asyncio
    async def test_list_tools_all_enabled_by_default(self) -> None:
        from backend.api.handlers.admin import list_tools_admin

        result = await list_tools_admin(self._admin_request())
        assert result["total"] > 0
        assert all(t["enabled"] for t in result["tools"])

    @pytest.mark.asyncio
    async def test_toggle_off_persists_and_reflects_in_list(self) -> None:
        from backend.api.handlers.admin import (
            ToolToggleBody,
            list_tools_admin,
            toggle_tool,
        )
        from backend.nodes.tools import ALL_TOOLS

        target = ALL_TOOLS[0].name

        res = await toggle_tool(
            self._admin_request(), target, ToolToggleBody(enabled=False)
        )
        assert res == {"status": "ok", "tool": target, "enabled": False}

        listing = await list_tools_admin(self._admin_request())
        entry = next(t for t in listing["tools"] if t["name"] == target)
        assert entry["enabled"] is False

    @pytest.mark.asyncio
    async def test_toggle_persists_via_tool_policy_global_scope(self) -> None:
        """A persistência real é tool_policy — não um dict/atributo local do handler."""
        from backend.api.handlers.admin import ToolToggleBody, toggle_tool
        from backend.nodes.tools import ALL_TOOLS
        from backend.rbac import tool_policy

        target = ALL_TOOLS[0].name
        await toggle_tool(self._admin_request(), target, ToolToggleBody(enabled=False))

        assert target in tool_policy.get_disabled(tool_policy.GLOBAL_SCOPE)
        assert tool_policy.is_allowed("qualquer-usuario", target) is False

    @pytest.mark.asyncio
    async def test_toggle_back_on_reenables(self) -> None:
        from backend.api.handlers.admin import ToolToggleBody, toggle_tool
        from backend.nodes.tools import ALL_TOOLS
        from backend.rbac import tool_policy

        target = ALL_TOOLS[0].name
        await toggle_tool(self._admin_request(), target, ToolToggleBody(enabled=False))
        await toggle_tool(self._admin_request(), target, ToolToggleBody(enabled=True))

        assert tool_policy.is_allowed("qualquer-usuario", target) is True

    @pytest.mark.asyncio
    async def test_toggle_unknown_tool_404(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.admin import ToolToggleBody, toggle_tool

        with pytest.raises(HTTPException) as exc:
            await toggle_tool(
                self._admin_request(),
                "ferramenta-que-nao-existe",
                ToolToggleBody(enabled=False),
            )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_toggle_requires_admin_role(self) -> None:
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from backend.api.handlers.admin import ToolToggleBody, toggle_tool
        from backend.nodes.tools import ALL_TOOLS

        request = MagicMock()
        request.state.user = MagicMock(role="member", id="u1")

        with pytest.raises(HTTPException) as exc:
            await toggle_tool(request, ALL_TOOLS[0].name, ToolToggleBody(enabled=False))
        assert exc.value.status_code == 403


class TestTimezoneEndpoint:
    """Timezone do usuário — o backend já usava `user_timezone` no scheduler,
    mas só dava pra configurar por API interna; sem estes endpoints a UI não
    tem como expor o campo."""

    def test_rotas_registradas(self) -> None:
        from backend.api.handlers.admin import router

        paths = [r.path for r in router.routes]  # type: ignore[attr-defined]  # ty: ignore[missing-attribute]
        assert any("timezone" in p for p in paths)

    @pytest.mark.asyncio
    async def test_salva_timezone_valido_e_rejeita_invalido(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from backend.api.handlers import admin
        from backend.workspace.runtime_settings import runtime_settings

        gravados: list[str] = []
        monkeypatch.setattr(admin, "require_admin", lambda _u: None)
        monkeypatch.setattr(runtime_settings, "set_user_timezone", gravados.append)
        monkeypatch.setattr(
            type(runtime_settings),
            "user_timezone",
            property(lambda _s: gravados[-1] if gravados else ""),
        )

        class _Req:
            state = type("S", (), {"user": object()})()

        # Happy: zona IANA real é aceita e persistida.
        out = await admin.patch_timezone(
            _Req(),  # ty: ignore[invalid-argument-type]
            admin.TimezoneBody(timezone="America/Sao_Paulo"),
        )
        assert out["timezone"] == "America/Sao_Paulo"
        assert gravados == ["America/Sao_Paulo"]

        # Erro/borda: zona inventada é REJEITADA com 422 em vez de aceita e
        # degradada — salvar outra coisa faria os agendamentos dispararem num
        # fuso que o usuário não pediu, sem aviso nenhum.
        with pytest.raises(HTTPException) as exc:
            await admin.patch_timezone(
                _Req(),  # ty: ignore[invalid-argument-type]
                admin.TimezoneBody(timezone="Marte/Olympus"),
            )
        assert exc.value.status_code == 422
        assert gravados == ["America/Sao_Paulo"], "gravou apesar de inválido"

        # Borda: string vazia é válida (volta ao fuso local do SO).
        await admin.patch_timezone(
            _Req(),  # ty: ignore[invalid-argument-type]
            admin.TimezoneBody(timezone="  "),
        )
        assert gravados[-1] == ""

    @pytest.mark.asyncio
    async def test_get_devolve_lista_de_zonas_para_o_seletor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.api.handlers import admin

        monkeypatch.setattr(admin, "require_admin", lambda _u: None)

        class _Req:
            state = type("S", (), {"user": object()})()

        out = await admin.get_timezone(_Req())  # ty: ignore[invalid-argument-type]

        # A lista vem do zoneinfo do próprio backend — copiá-la no frontend
        # faria a UI divergir do que o backend aceita.
        assert "America/Sao_Paulo" in out["available"]
        assert "UTC" in out["available"]
        assert len(out["available"]) > 100


class TestServiceTokenEndpoints:
    """POST/GET/DELETE /admin/service-tokens — apenas root."""

    @pytest.fixture(autouse=True)
    async def _isolate_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> AsyncIterator[None]:
        import aiosqlite

        import backend.rbac.auth as auth_mod

        auth_mod._db_conn = None
        db_file = str(tmp_path / "admin_service_tokens.db")

        async def _patched_get_db() -> Connection:
            if auth_mod._db_conn is not None:
                return auth_mod._db_conn
            conn = await aiosqlite.connect(db_file)
            conn.row_factory = aiosqlite.Row
            await auth_mod._ensure_schema(conn)
            auth_mod._db_conn = conn
            return conn

        monkeypatch.setattr(auth_mod, "_get_db", _patched_get_db)
        yield
        if auth_mod._db_conn is not None:
            await auth_mod._db_conn.close()
            auth_mod._db_conn = None

    def _req(self, role: str = "root") -> MagicMock:
        request = MagicMock(spec=Request)
        request.state = SimpleNamespace(user=SimpleNamespace(role=role, id="root-1"))
        return request

    async def test_root_cria_lista_e_revoga_token(self) -> None:
        from backend.api.handlers import admin

        criado = await admin.create_service_token_endpoint(
            self._req(), admin.CreateServiceTokenBody(name="ci-bot", scopes=["*"])
        )
        assert criado["raw_token"].startswith("vst_")
        token_id = criado["token"]["id"]

        listado = await admin.list_service_tokens_endpoint(self._req())
        assert any(t["id"] == token_id for t in listado["tokens"])

        revogado = await admin.revoke_service_token_endpoint(self._req(), token_id)
        assert revogado == {"revoked": True}
        # Erro/borda: revogar de novo não é erro, só devolve False.
        revogado_de_novo = await admin.revoke_service_token_endpoint(
            self._req(), token_id
        )
        assert revogado_de_novo == {"revoked": False}

    async def test_role_nao_root_e_negada_com_403(self) -> None:
        """Erro/borda: admin (não-root) não pode criar/listar/revogar
        tokens de serviço — mesmo peso de criar um usuário novo."""
        from fastapi import HTTPException

        from backend.api.handlers import admin

        with pytest.raises(HTTPException) as exc:
            await admin.create_service_token_endpoint(
                self._req(role="admin"),
                admin.CreateServiceTokenBody(name="x", scopes=[]),
            )
        assert exc.value.status_code == 403

        with pytest.raises(HTTPException) as exc:
            await admin.list_service_tokens_endpoint(self._req(role="admin"))
        assert exc.value.status_code == 403


class TestResolveAgent:
    """GET /admin/agents/resolve — dump do NativeAgent resolvido,
    equivalente nativo ao `dsh --dump-config` do deepseek-harness. Existe
    pra debugar por que uma tool/subagente não aparece pra um usuário sem
    precisar vasculhar logs."""

    @staticmethod
    def _req(role: str = "admin") -> MagicMock:
        from unittest.mock import MagicMock

        request = MagicMock()
        request.state.user = MagicMock(role=role, id="admin-1")
        return request

    @pytest.mark.asyncio
    async def test_admin_resolve_devolve_tools_prompt_e_subagentes(self) -> None:
        from backend.api.handlers.admin import resolve_agent

        result = await resolve_agent(self._req(), user_id="alice", chat_mode=True)

        assert result["user_id"] == "alice"
        assert result["chat_mode"] is True
        assert result["workspace_id"] is None
        assert isinstance(result["tools"], list)
        assert len(result["tools"]) > 0
        assert all(
            {"name", "category", "destructive", "render_hint"} <= t.keys()
            for t in result["tools"]
        )
        assert result["system_prompt_length"] == len(result["system_prompt"])
        assert result["subagents"] == []

    @pytest.mark.asyncio
    async def test_sem_workspace_e_user_id_usa_defaults(self) -> None:
        from backend.api.handlers.admin import resolve_agent

        result = await resolve_agent(self._req())

        assert result["user_id"] == "local"
        assert result["workspace_id"] is None
        assert len(result["tools"]) > 0

    @pytest.mark.asyncio
    async def test_usuario_nao_admin_recebe_403(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.admin import resolve_agent

        with pytest.raises(HTTPException) as exc:
            await resolve_agent(self._req(role="member"), user_id="alice")

        assert exc.value.status_code == 403


class TestTracerAdministration:
    @staticmethod
    def _request(role: str = "admin") -> MagicMock:
        from unittest.mock import MagicMock

        request = MagicMock()
        request.state.user = MagicMock(role=role, id="admin-1")
        return request

    @pytest.mark.asyncio
    async def test_list_traces_excludes_freeform_metadata(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from backend.api.handlers import admin
        from backend.persistence.tracer import tracer

        spans = [
            {
                "span_id": "span-1",
                "node": "llm",
                "status": "ok",
                "metadata": '{"prompt":"private"}',
            }
        ]
        get_recent = AsyncMock(return_value=spans)
        monkeypatch.setattr(tracer, "get_recent", get_recent)

        result = await admin.list_traces(self._request(), limit=20)

        assert result == {
            "traces": [
                {
                    "span_id": "span-1",
                    "parent_id": None,
                    "session_id": None,
                    "node": "llm",
                    "event": None,
                    "status": "ok",
                    "started_at": None,
                    "ended_at": None,
                    "duration_ms": None,
                    "in_tokens": None,
                    "out_tokens": None,
                }
            ]
        }
        assert "private" not in str(result)
        get_recent.assert_awaited_once_with(n=20)

    @pytest.mark.asyncio
    async def test_member_cannot_list_or_clear_traces(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from fastapi import HTTPException

        from backend.api.handlers import admin
        from backend.persistence.tracer import tracer

        get_recent = AsyncMock()
        clear_all = AsyncMock()
        monkeypatch.setattr(tracer, "get_recent", get_recent)
        monkeypatch.setattr(tracer, "clear_all", clear_all)

        with pytest.raises(HTTPException) as list_error:
            await admin.list_traces(self._request("member"))
        with pytest.raises(HTTPException) as clear_error:
            await admin.clear_traces(self._request("member"), confirm=True)

        assert list_error.value.status_code == clear_error.value.status_code == 403
        get_recent.assert_not_awaited()
        clear_all.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_clear_traces_requires_confirmation(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers import admin

        with pytest.raises(HTTPException) as exc:
            await admin.clear_traces(self._request(), confirm=False)

        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_clear_traces_can_be_scoped_to_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from unittest.mock import AsyncMock

        from backend.api.handlers import admin
        from backend.persistence.tracer import tracer

        clear_session = AsyncMock(return_value=3)
        clear_all = AsyncMock()
        monkeypatch.setattr(tracer, "clear_session", clear_session)
        monkeypatch.setattr(tracer, "clear_all", clear_all)

        result = await admin.clear_traces(
            self._request("root"), confirm=True, session_id=42
        )

        assert result == {"deleted": 3}
        clear_session.assert_awaited_once_with(42)
        clear_all.assert_not_awaited()


class TestToolAccessByRole:
    """RBAC de tools: GET/PUT /admin/tool-access."""

    @pytest.fixture(autouse=True)
    def iso_dir(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from backend.rbac import tool_policy

        monkeypatch.setattr(tool_policy, "_policy_dir", lambda: tmp_path / "tools")

    @staticmethod
    def _request(role: str = "admin") -> MagicMock:
        from unittest.mock import MagicMock

        request = MagicMock()
        request.state.user = MagicMock(role=role, id="u-1")
        return request

    @pytest.mark.asyncio
    async def test_lista_todas_as_tools_liberadas_para_todos_os_roles(self) -> None:
        from backend.api.handlers.admin import get_tool_access

        result = await get_tool_access(self._request())

        assert result["roles"] == ["member", "viewer"]
        assert result["always_allowed"] == ["admin", "root"]
        assert all(t["roles"] == ["member", "viewer"] for t in result["tools"])

    @pytest.mark.asyncio
    async def test_put_restringe_e_a_leitura_reflete(self) -> None:
        from backend.api.handlers.admin import (
            ToolAccessBody,
            get_tool_access,
            put_tool_access,
        )
        from backend.nodes.tools import ALL_TOOLS

        target = ALL_TOOLS[0].name
        await put_tool_access(
            self._request(), ToolAccessBody(access={target: ["member"]})
        )

        entry = next(
            t
            for t in (await get_tool_access(self._request()))["tools"]
            if t["name"] == target
        )
        assert entry["roles"] == ["member"]

    @pytest.mark.asyncio
    async def test_apenas_admin_pode_ler_e_gravar(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.admin import (
            ToolAccessBody,
            get_tool_access,
            put_tool_access,
        )

        with pytest.raises(HTTPException) as read_error:
            await get_tool_access(self._request("member"))
        with pytest.raises(HTTPException) as write_error:
            await put_tool_access(self._request("member"), ToolAccessBody(access={}))
        assert read_error.value.status_code == 403
        assert write_error.value.status_code == 403

    @pytest.mark.asyncio
    async def test_rejeita_tool_e_role_desconhecidos(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.admin import ToolAccessBody, put_tool_access
        from backend.nodes.tools import ALL_TOOLS

        with pytest.raises(HTTPException) as unknown_tool:
            await put_tool_access(
                self._request(), ToolAccessBody(access={"nao_existe": ["member"]})
            )
        with pytest.raises(HTTPException) as unknown_role:
            await put_tool_access(
                self._request(),
                ToolAccessBody(access={ALL_TOOLS[0].name: ["superuser"]}),
            )
        assert unknown_tool.value.status_code == 400
        assert unknown_role.value.status_code == 400
