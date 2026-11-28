"""Tests para src/api/handlers/artifacts.py.

Cobre:
- list_artifacts vazia para session_id sem pasta.
- list_artifacts ordena por mtime descendente (mais novos primeiro).
- get_artifact devolve o markdown completo.
- Sanitização anti-traversal em slug e session_id (.. e barras).
- Preview de 200 chars no list.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException, Request

# ---------------------------------------------------------------------------
# Fixture — redireciona ~ para uma pasta temporária por teste
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isola ``settings.vectora_home`` em tmp_path — os helpers do handler
    resolvem o diretório de artifacts a partir dele."""
    from backend.settings import settings

    monkeypatch.setattr(settings, "vectora_home", tmp_path)
    return tmp_path


def _write_artifact(home: Path, session_id: str, slug: str, content: str) -> Path:
    """Cria um arquivo de artifact dentro de <vectora_home>/artifacts/<session>/."""
    base = home / "artifacts" / session_id
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{slug}.md"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# list_artifacts
# ---------------------------------------------------------------------------


class TestListArtifacts:
    @pytest.mark.asyncio
    async def test_lists_markdown_plans_from_the_owned_local_workspace(
        self, fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.api.handlers.artifacts import get_artifact, list_artifacts
        from backend.workspace.workspace import workspace_registry

        workspace_root = tmp_path / "teste"
        plans_dir = workspace_root / ".vectora" / "plans"
        plans_dir.mkdir(parents=True)
        first = plans_dir / (
            "plano-de-execução-refatoração-reutilizável-do-jogo-godot-46.md"
        )
        first.write_text(
            "# Plano de execução: arquitetura reutilizável para Godot 4.6\n\n"
            "Conteúdo A",
            encoding="utf-8",
        )
        second = plans_dir / (
            "plano-de-refatoração-do-jogo-godot-46-com-classes-reutilizáveis-e-autoloads.md"
        )
        second.write_text(
            "# Plano: arquitetura reutilizável para Godot 4.6\n\nConteúdo B",
            encoding="utf-8",
        )
        external_plan = tmp_path / "outside.md"
        external_plan.write_text("# Fora do workspace", encoding="utf-8")
        (plans_dir / "link-externo.md").symlink_to(external_plan)

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/artifacts/",
                "headers": [],
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("testclient", 123),
                "scheme": "http",
                "http_version": "1.1",
            }
        )
        request.state.user = SimpleNamespace(id="owner")

        class SessionStore:
            async def get_session(
                self, thread_id: str, *, user_id: str | None = None
            ) -> dict[str, str]:
                assert thread_id == "thread-with-workspace"
                return {"workspace_id": "workspace-1", "user_id": "owner"}

        async def get_session_store():
            return SessionStore()

        workspace = SimpleNamespace(
            id="workspace-1", cwd=str(workspace_root), transport="local"
        )
        monkeypatch.setattr(
            "backend.api.handlers.threads._get_session_store",
            get_session_store,
        )
        monkeypatch.setattr(
            "backend.api.handlers.threads._assert_existing_thread_ownership",
            AsyncMock(),
        )
        monkeypatch.setattr(workspace_registry, "get", lambda workspace_id: workspace)
        monkeypatch.setattr(
            "backend.api.handlers.workspaces.require_workspace_access",
            lambda workspace_id, request: workspace,
        )

        response = await list_artifacts(
            session_id="thread-with-workspace", request=request
        )

        local_plans = {
            item.path: item
            for item in response.artifacts
            if item.artifact_type == "workspace_plan"
        }
        assert set(local_plans) == {str(first), str(second)}
        assert (
            local_plans[str(first)].title
            == "Plano de execução: arquitetura reutilizável para Godot 4.6"
        )
        assert (
            local_plans[str(second)].title
            == "Plano: arquitetura reutilizável para Godot 4.6"
        )

        opened = await get_artifact(
            slug="plano-de-execução-refatoração-reutilizável-do-jogo-godot-46",
            session_id="thread-with-workspace",
            source="workspace_plan",
            request=request,
        )
        assert opened.content == (
            "# Plano de execução: arquitetura reutilizável para Godot 4.6\n\nConteúdo A"
        )
        assert opened.artifact_type == "workspace_plan"

        def deny_workspace_access(workspace_id: str, request) -> NoReturn:
            raise HTTPException(status_code=403, detail="Sem acesso")

        monkeypatch.setattr(
            "backend.api.handlers.workspaces.require_workspace_access",
            deny_workspace_access,
        )
        with pytest.raises(HTTPException) as exc_info:
            await list_artifacts(session_id="thread-with-workspace", request=request)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_does_not_list_local_plans_for_remote_workspaces(
        self, fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.api.handlers.artifacts import list_artifacts
        from backend.workspace.workspace import workspace_registry

        workspace_root = tmp_path / "remote-mount"
        plans_dir = workspace_root / ".vectora" / "plans"
        plans_dir.mkdir(parents=True)
        (plans_dir / "remote-plan.md").write_text("# Remote", encoding="utf-8")
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/artifacts/",
                "headers": [],
                "query_string": b"",
                "server": ("testserver", 80),
                "client": ("testclient", 123),
                "scheme": "http",
                "http_version": "1.1",
            }
        )
        request.state.user = SimpleNamespace(id="owner")

        class SessionStore:
            async def get_session(
                self, thread_id: str, *, user_id: str | None = None
            ) -> dict[str, str]:
                return {"workspace_id": "workspace-remote", "user_id": "owner"}

        async def get_session_store():
            return SessionStore()

        workspace = SimpleNamespace(
            id="workspace-remote", cwd=str(workspace_root), transport="ssh"
        )
        monkeypatch.setattr(
            "backend.api.handlers.threads._get_session_store",
            get_session_store,
        )
        monkeypatch.setattr(
            "backend.api.handlers.threads._assert_existing_thread_ownership",
            AsyncMock(),
        )
        monkeypatch.setattr(workspace_registry, "get", lambda workspace_id: workspace)
        monkeypatch.setattr(
            "backend.api.handlers.workspaces.require_workspace_access",
            lambda workspace_id, request: workspace,
        )

        response = await list_artifacts(
            session_id="thread-with-remote-workspace", request=request
        )

        assert response.artifacts == []

    @pytest.mark.asyncio
    async def test_storage_read_failure_is_not_reported_as_an_empty_plan(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.artifacts import list_artifacts

        configured = fake_home / "artifacts" / "unreadable-session"
        original_stat = Path.stat

        def fail_for_configured_root(path: Path, *args, **kwargs):
            if path == configured:
                raise PermissionError("storage unavailable")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", fail_for_configured_root)

        with pytest.raises(HTTPException) as exc_info:
            await list_artifacts(session_id="unreadable-session")

        assert exc_info.value.status_code == 500

    @pytest.mark.asyncio
    async def test_merges_configured_and_legacy_artifacts_for_same_session(
        self, fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.api.handlers.artifacts import get_artifact, list_artifacts

        legacy_home = tmp_path / "legacy-home"
        monkeypatch.setattr(Path, "home", lambda: legacy_home)
        current_path = _write_artifact(
            fake_home, "mixed-session", "shared-plan", "# Current version"
        )
        _write_artifact(
            legacy_home / ".vectora",
            "mixed-session",
            "shared-plan",
            "# Legacy duplicate",
        )
        _write_artifact(
            legacy_home / ".vectora",
            "mixed-session",
            "legacy-plan",
            "# Legacy only",
        )

        response = await list_artifacts(session_id="mixed-session")
        by_slug = {Path(item.path).stem: item for item in response.artifacts}

        assert set(by_slug) == {"shared-plan", "legacy-plan"}
        assert Path(by_slug["shared-plan"].path) == current_path
        assert "Current version" in (by_slug["shared-plan"].content_preview or "")
        assert "Legacy only" in (by_slug["legacy-plan"].content_preview or "")

        legacy_content = await get_artifact(
            slug="legacy-plan", session_id="mixed-session"
        )
        assert legacy_content.content == "# Legacy only"

    @pytest.mark.asyncio
    async def test_reads_legacy_artifact_directory_when_profile_home_changed(
        self, fake_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from backend.api.handlers.artifacts import list_artifacts

        legacy_home = tmp_path / "legacy-home"
        monkeypatch.setattr(Path, "home", lambda: legacy_home)
        legacy_path = _write_artifact(
            legacy_home / ".vectora", "legacy-session", "saved-plan", "# Existing plan"
        )

        response = await list_artifacts(session_id="legacy-session")

        assert [Path(item.path).name for item in response.artifacts] == [
            "saved-plan.md"
        ]
        assert response.artifacts[0].content_preview == "# Existing plan"
        assert legacy_path.is_file()

    @pytest.mark.asyncio
    async def test_empty_for_unknown_session(self, fake_home: Path) -> None:
        """Verifica o comportamento de empty for unknown session."""
        from backend.api.handlers.artifacts import list_artifacts

        resp = await list_artifacts(session_id="never-existed")
        assert resp.artifacts == []

    @pytest.mark.asyncio
    async def test_empty_for_blank_session_id(self, fake_home: Path) -> None:
        """Verifica o comportamento de empty for blank session id."""
        from backend.api.handlers.artifacts import list_artifacts

        resp = await list_artifacts(session_id="")
        assert resp.artifacts == []

    @pytest.mark.asyncio
    async def test_lists_existing_artifacts(self, fake_home: Path) -> None:
        """Verifica o comportamento de lists existing artifacts."""
        from backend.api.handlers.artifacts import list_artifacts

        _write_artifact(fake_home, "sess1", "plano-x", "# Plano X\n\ncorpo")
        _write_artifact(fake_home, "sess1", "spec-y", "# Spec Y\n\ndetalhe")

        resp = await list_artifacts(session_id="sess1")
        slugs = [Path(a.path).stem for a in resp.artifacts]
        assert "plano-x" in slugs
        assert "spec-y" in slugs

    @pytest.mark.asyncio
    async def test_orders_by_mtime_desc(self, fake_home: Path) -> None:
        """Mais recentes primeiro — o usuário vê o último plano no topo."""
        from backend.api.handlers.artifacts import list_artifacts

        a = _write_artifact(fake_home, "ord", "antigo", "velho")
        # Garante mtimes diferentes (tolerância de 1s em sistemas com mtime granular)
        past = time.time() - 60
        os.utime(a, (past, past))
        _write_artifact(fake_home, "ord", "novo", "recente")

        resp = await list_artifacts(session_id="ord")
        slugs = [Path(art.path).stem for art in resp.artifacts]
        assert slugs[0] == "novo"
        assert slugs[1] == "antigo"

    @pytest.mark.asyncio
    async def test_content_preview_truncated_to_200(self, fake_home: Path) -> None:
        """O preview no list é limitado — não devolve markdown gigante."""
        from backend.api.handlers.artifacts import list_artifacts

        big = "X" * 1000
        _write_artifact(fake_home, "prev", "huge", big)
        resp = await list_artifacts(session_id="prev")
        assert resp.artifacts[0].content_preview is not None
        assert len(resp.artifacts[0].content_preview) <= 200

    @pytest.mark.asyncio
    async def test_exposes_artifact_type_from_sidecar(self, fake_home: Path) -> None:
        """Verifica o comportamento de exposes artifact type from sidecar."""
        from backend.api.handlers.artifacts import list_artifacts

        path = _write_artifact(fake_home, "typed", "plano-x", "conteudo")
        path.with_suffix(".artifact_type").write_text("plan", encoding="utf-8")

        resp = await list_artifacts(session_id="typed")
        assert resp.artifacts[0].artifact_type == "plan"

    async def test_exposes_integrity_metadata(self, fake_home: Path) -> None:
        """Verifica o comportamento de exposes integrity metadata."""
        from backend.api.handlers.artifacts import list_artifacts

        path = _write_artifact(fake_home, "integrity-1", "plan", "conteúdo verificável")
        path.with_suffix(".artifact.json").write_text(
            json.dumps(
                {
                    "byte_size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "retention_class": "run",
                }
            ),
            encoding="utf-8",
        )
        response = await list_artifacts(session_id="integrity-1")

        artifact = next(item for item in response.artifacts if item.path == str(path))
        assert artifact.byte_size == path.stat().st_size
        assert len(artifact.sha256) == 64
        assert artifact.retention_class == "run"

    @pytest.mark.asyncio
    async def test_legacy_artifact_without_sidecar_defaults_to_other(
        self, fake_home: Path
    ) -> None:
        """Erro/borda: artifact criado antes do campo existir (sem sidecar)
        não quebra a listagem — cai num default sensato."""
        from backend.api.handlers.artifacts import list_artifacts

        _write_artifact(fake_home, "legacy", "plano-antigo", "conteudo")

        resp = await list_artifacts(session_id="legacy")
        assert resp.artifacts[0].artifact_type == "other"

    @pytest.mark.asyncio
    async def test_session_id_traversal_is_stripped(self, fake_home: Path) -> None:
        """`..` e barras no session_id são removidos — não foge da pasta."""
        from backend.api.handlers.artifacts import list_artifacts

        # Cria artifact numa sessão legítima.
        _write_artifact(fake_home, "legit", "plano", "ok")
        # Cria também um artifact "irmão" que NÃO deveria vazar via traversal.
        bad = fake_home / ".vectora" / "artifacts" / "outra" / "secreto.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("segredo", encoding="utf-8")

        # Tenta atacar via "..".
        resp = await list_artifacts(session_id="legit/../outra")
        # O sanitize remove "/" e ".." — vira "legitoutra" (sessão inexistente).
        # Não pode ter "secreto" no resultado.
        paths = [a.path for a in resp.artifacts]
        assert not any("secreto" in p for p in paths)

    @pytest.mark.asyncio
    async def test_authenticated_owner_can_list_artifacts(
        self, fake_home: Path
    ) -> None:
        """Verifica o comportamento de authenticated owner can list artifacts."""
        from backend.api.handlers.artifacts import list_artifacts

        _write_artifact(fake_home, "owned", "plan", "conteudo")
        request = cast(
            "Request",
            SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id="owner"))),
        )
        with patch(
            "backend.api.handlers.threads._assert_existing_thread_ownership",
            new=AsyncMock(),
        ) as ownership:
            response = await list_artifacts(session_id="owned", request=request)

        ownership.assert_awaited_once_with("owned", request)
        assert len(response.artifacts) == 1

    @pytest.mark.asyncio
    async def test_unauthenticated_artifact_request_is_rejected(
        self, fake_home: Path
    ) -> None:
        """Verifica o comportamento de unauthenticated artifact request is rejected."""
        from backend.api.handlers.artifacts import list_artifacts

        request = cast("Request", SimpleNamespace(state=SimpleNamespace(user=None)))
        with pytest.raises(HTTPException) as exc_info:
            await list_artifacts(session_id="owned", request=request)

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_different_authenticated_user_cannot_list_artifacts(
        self, fake_home: Path
    ) -> None:
        """Verifica o comportamento de different authenticated user cannot list artifacts."""
        from backend.api.handlers.artifacts import list_artifacts

        request = cast(
            "Request",
            SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id="other"))),
        )
        with patch(
            "backend.api.handlers.threads._assert_existing_thread_ownership",
            new=AsyncMock(
                side_effect=HTTPException(
                    status_code=404, detail="Thread não encontrada"
                )
            ),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await list_artifacts(session_id="owned", request=request)

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# get_artifact
# ---------------------------------------------------------------------------


class TestGetArtifact:
    @pytest.mark.asyncio
    async def test_read_failure_is_not_returned_as_empty_content(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.artifacts import get_artifact

        path = _write_artifact(fake_home, "unreadable", "plan", "# Existing plan")
        original_read_text = Path.read_text

        def fail_for_artifact(file_path: Path, *args, **kwargs: str):
            if file_path == path:
                raise PermissionError("storage unavailable")
            return original_read_text(file_path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_for_artifact)

        with pytest.raises(HTTPException) as exc_info:
            await get_artifact("plan", session_id="unreadable")

        assert exc_info.value.status_code == 500
        assert (
            exc_info.value.detail
            == "Não foi possível abrir este artifact. Tente novamente."
        )

    @pytest.mark.asyncio
    async def test_returns_content(self, fake_home: Path) -> None:
        """Verifica o comportamento de returns content."""
        from backend.api.handlers.artifacts import get_artifact

        _write_artifact(fake_home, "g1", "meu-plano", "# Conteúdo\n\nlinha")
        resp = await get_artifact("meu-plano", session_id="g1")
        assert "Conteúdo" in resp.content
        assert resp.slug == "meu-plano"
        assert resp.session_id == "g1"
        assert resp.created_at  # ISO 8601 do mtime

    @pytest.mark.asyncio
    async def test_returns_artifact_type_from_sidecar(self, fake_home: Path) -> None:
        """Verifica o comportamento de returns artifact type from sidecar."""
        from backend.api.handlers.artifacts import get_artifact

        path = _write_artifact(fake_home, "g2", "spec-y", "detalhe")
        path.with_suffix(".artifact_type").write_text("spec", encoding="utf-8")

        resp = await get_artifact("spec-y", session_id="g2")
        assert resp.artifact_type == "spec"

    @pytest.mark.asyncio
    async def test_returns_empty_for_missing(self, fake_home: Path) -> None:
        """Verifica o comportamento de returns empty for missing."""
        from backend.api.handlers.artifacts import get_artifact

        resp = await get_artifact("nao-existe", session_id="g1")
        assert resp.content == ""

    @pytest.mark.asyncio
    async def test_slug_traversal_is_stripped(self, fake_home: Path) -> None:
        """Slug com `..` e barras não consegue ler arquivos fora da pasta."""
        from backend.api.handlers.artifacts import get_artifact

        # Cria um arquivo "real" na pasta da sessão e outro fora.
        _write_artifact(fake_home, "safe", "ok", "interno")
        outside = fake_home / "artifacts" / "safe.md"
        outside.write_text("FORA", encoding="utf-8")

        resp = await get_artifact("../safe", session_id="safe")
        # ".." e "/" removidos pelo handler — vira "safe.md" dentro de
        # ~/.vectora/artifacts/safe/safe.md, que não existe → content vazio.
        assert resp.content == ""
        assert "FORA" not in resp.content

    @pytest.mark.asyncio
    async def test_session_id_traversal_is_stripped_in_get(
        self, fake_home: Path
    ) -> None:
        """Verifica o comportamento de session id traversal is stripped in get."""
        from backend.api.handlers.artifacts import get_artifact

        # Cria o "secreto" fora da pasta esperada.
        bad = fake_home / ".vectora" / "artifacts" / "boom" / "secreto.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("segredo", encoding="utf-8")

        resp = await get_artifact("secreto", session_id="legit/../boom")
        # Sanitização junta tudo numa string sem traversal.
        assert "segredo" not in resp.content


class TestMediaNaListagem:
    """Imagem/áudio de `tools/media.py` ficam em `artifacts/{sessão}/media/`
    como binário, fora do `glob("*.md")`. Sem entrarem na listagem, o arquivo
    existe em disco mas não aparece em lugar nenhum da interface — que era o
    ponto de gerar mídia."""

    @pytest.mark.asyncio
    async def test_media_aparece_junto_dos_documentos(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verifica o comportamento de media aparece junto dos documentos."""
        from backend.api.handlers import artifacts as mod

        base = tmp_path / "sessao-1"
        (base / "media").mkdir(parents=True)
        (base / "plano.md").write_text("# Plano", encoding="utf-8")
        (base / "media" / "gato.png").write_bytes(b"\x89PNG-fake")
        (base / "media" / "voz.mp3").write_bytes(b"ID3-fake")

        monkeypatch.setattr(mod, "_artifact_dirs", lambda _s: (base,))

        out = await mod.list_artifacts(session_id="sessao-1")
        tipos = {a.title: a.artifact_type for a in out.artifacts}

        assert tipos.get("gato") == "media"
        assert tipos.get("voz") == "media"
        # Regressão: o markdown continua listado com seu próprio tipo.
        assert "plano" in tipos
        assert tipos["plano"] != "media"

        # Binário não tem preview de texto — `None` em vez de tentar decodificar
        # bytes, que encheria a lista de lixo.
        media = [a for a in out.artifacts if a.artifact_type == "media"]
        assert all(a.content_preview is None for a in media)
        # Listagem de mídia não deve reabrir e hashear payloads binários.
        assert all(a.sha256 == "" for a in media)

    @pytest.mark.asyncio
    async def test_sessao_sem_media_nao_quebra(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Erro/borda: a esmagadora maioria das sessões nunca gera mídia — a
        ausência da pasta é o caso comum, não um erro."""
        from backend.api.handlers import artifacts as mod

        base = tmp_path / "sessao-2"
        base.mkdir(parents=True)
        (base / "spec.md").write_text("# Spec", encoding="utf-8")
        monkeypatch.setattr(mod, "_artifact_dirs", lambda _s: (base,))

        out = await mod.list_artifacts(session_id="sessao-2")

        assert [a.title for a in out.artifacts] == ["spec"]

    @pytest.mark.asyncio
    async def test_media_ilegivel_nao_derruba_a_listagem(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Erro/borda: `iterdir` estourando (permissão, disco) não pode fazer a
        aba inteira sumir — os documentos continuam listados."""
        from backend.api.handlers import artifacts as mod

        base = tmp_path / "sessao-3"
        (base / "media").mkdir(parents=True)
        (base / "guia.md").write_text("# Guia", encoding="utf-8")
        monkeypatch.setattr(mod, "_artifact_dirs", lambda _s: (base,))

        def _explode(_self: Path) -> NoReturn:
            """Executa a operação  explode."""
            raise OSError("sem permissão")

        monkeypatch.setattr(mod.Path, "iterdir", _explode)

        out = await mod.list_artifacts(session_id="sessao-3")
        assert [a.title for a in out.artifacts] == ["guia"]


class TestGetMediaArtifact:
    """`GET /artifacts/{session_id}/media/{filename}` — serve o binário que
    `generate_image`/`text_to_speech`/`generate_video` (`tools/media.py`)
    persistem, pra dar ao frontend uma URL de verdade em vez de um `path`
    de arquivo no servidor (contrato que `ImagePreview`/`ArtifactCard`
    esperam)."""

    @pytest.mark.asyncio
    async def test_serve_arquivo_existente(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Verifica o comportamento de serve arquivo existente."""
        from backend.api.handlers import artifacts as mod
        from backend.settings import settings

        monkeypatch.setattr(settings, "vectora_home", tmp_path)
        base = tmp_path / "artifacts" / "sessao-1" / "media"
        base.mkdir(parents=True)
        (base / "gato.png").write_bytes(b"\x89PNG-fake")

        resp = await mod.get_media_artifact(session_id="sessao-1", filename="gato.png")

        assert Path(resp.path).read_bytes() == b"\x89PNG-fake"

    @pytest.mark.asyncio
    async def test_arquivo_ausente_devolve_404_e_path_traversal_e_neutralizado(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Erro/borda no mesmo teste: arquivo inexistente vira 404 (não
        `FileNotFoundError` cru), e um `filename`/`session_id` tentando
        escapar via `..`/barras não pode servir nada fora de
        `<vectora_home>/artifacts/<session_id>/media/` — mesma sanitização
        anti-traversal já usada em `_artifacts_dir`/`get_artifact`."""
        from fastapi import HTTPException

        from backend.api.handlers import artifacts as mod
        from backend.settings import settings

        monkeypatch.setattr(settings, "vectora_home", tmp_path)
        # Arquivo real fora da pasta de mídia — o alvo do traversal.
        secret = tmp_path / "segredo.txt"
        secret.write_text("nao pode vazar")

        with pytest.raises(HTTPException) as exc:
            await mod.get_media_artifact(
                session_id="sessao-1", filename="nunca-existiu.png"
            )
        assert exc.value.status_code == 404

        with pytest.raises(HTTPException) as exc2:
            await mod.get_media_artifact(session_id="..", filename="../../segredo.txt")
        assert exc2.value.status_code == 404
