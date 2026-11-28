"""Testes para backend/api/handlers/context_graph.py.

Cobre helpers (_graph_dir, _read_status_file, _require_graph_json) e
os endpoints assíncronos (query, explain, path, status, affected) via chamada
direta das funções com request mockado — sem TestClient para evitar dependência
de toda a stack FastAPI.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Literal, NoReturn
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
from tests.json_values import as_list, as_object


def _make_registry(tmp_path: Path) -> tuple[MagicMock, MagicMock]:
    ws = MagicMock()
    ws.cwd = str(tmp_path)
    registry = MagicMock()
    registry.get = MagicMock(return_value=ws)
    return registry, ws


def _write_graph(tmp_path: Path, data: dict) -> Path:
    d = tmp_path / ".vectora" / "context-graph"
    d.mkdir(parents=True, exist_ok=True)
    (d / "graph.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def _write_status(graph_dir: Path, status: str, **extra) -> None:
    payload = {"status": status, "built_at": "2026-01-01T00:00:00Z", **extra}
    (graph_dir / "build_status.json").write_text(json.dumps(payload), encoding="utf-8")


def _fake_request(user_id: str = "u1") -> MagicMock:
    req = MagicMock()
    req.state.user = MagicMock()
    req.state.user.id = user_id
    return req


# ---------------------------------------------------------------------------
# _graph_dir
# ---------------------------------------------------------------------------


class TestGraphDir:
    def test_workspace_not_found_returns_none(self) -> None:
        from backend.api.handlers.context_graph import _graph_dir

        registry = MagicMock()
        registry.get = MagicMock(return_value=None)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            assert _graph_dir("nonexistent") is None

    def test_workspace_found_returns_expected_path(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import _graph_dir

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            result = _graph_dir("ws1")
        assert result == tmp_path / ".vectora" / "context-graph"


# ---------------------------------------------------------------------------
# _read_status_file
# ---------------------------------------------------------------------------


class TestReadStatusFile:
    def test_returns_none_when_file_absent(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import _read_status_file

        graph_dir = tmp_path / ".vectora" / "context-graph"
        graph_dir.mkdir(parents=True)
        assert _read_status_file(graph_dir) is None

    def test_reads_done_status_with_counts(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import _read_status_file

        graph_dir = tmp_path / ".vectora" / "context-graph"
        graph_dir.mkdir(parents=True)
        _write_status(graph_dir, "done", node_count=5, edge_count=12)
        s = _read_status_file(graph_dir)
        assert s is not None
        assert s.status == "done"
        assert s.node_count == 5
        assert s.edge_count == 12

    def test_reads_running_status(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import _read_status_file

        graph_dir = tmp_path / ".vectora" / "context-graph"
        graph_dir.mkdir(parents=True)
        _write_status(graph_dir, "running")
        s = _read_status_file(graph_dir)
        assert s is not None
        assert s.status == "running"

    def test_reads_error_status(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import _read_status_file

        graph_dir = tmp_path / ".vectora" / "context-graph"
        graph_dir.mkdir(parents=True)
        _write_status(graph_dir, "error", error="Pipeline falhou")
        s = _read_status_file(graph_dir)
        assert s is not None
        assert s.status == "error"
        assert "Pipeline falhou" in (s.error or "")

    def test_reads_running_with_step_info(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import _read_status_file

        graph_dir = tmp_path / ".vectora" / "context-graph"
        graph_dir.mkdir(parents=True)
        _write_status(
            graph_dir,
            "running",
            step=2,
            step_total=9,
            step_label="Extraindo AST...",
            files_total=10,
            files_done=3,
            files_list=["src/a.py", "src/b.py"],
        )
        s = _read_status_file(graph_dir)
        assert s is not None
        assert s.step == 2
        assert s.step_total == 9
        assert s.step_label == "Extraindo AST..."
        assert s.files_total == 10
        assert s.files_done == 3
        assert s.files_list == ["src/a.py", "src/b.py"]


# ---------------------------------------------------------------------------
# _require_graph_json
# ---------------------------------------------------------------------------


class TestRequireGraphJson:
    def test_raises_404_when_file_missing(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import _require_graph_json

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                _require_graph_json("ws1")
        assert exc.value.status_code == 404

    def test_returns_parsed_data(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import _require_graph_json

        _write_graph(tmp_path, {"nodes": [{"id": "x"}], "edges": []})
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            data = _require_graph_json("ws1")
        assert as_object(as_list(data["nodes"])[0])["id"] == "x"


# ---------------------------------------------------------------------------
# get_status — leitura via build_status.json no disco
# ---------------------------------------------------------------------------


class TestGetStatus:
    @pytest.mark.asyncio
    async def test_running_from_status_file(self, tmp_path: Path) -> None:
        from backend.api.handlers import context_graph as cg
        from backend.api.handlers.context_graph import get_status

        graph_dir = _write_graph(tmp_path, {})
        _write_status(graph_dir, "running")
        registry, _ = _make_registry(tmp_path)
        # Build ativo neste processo → status "running" é legítimo (não-órfão).
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch.dict(cg._active_builds, {"ws1": object()}, clear=False),
        ):
            s = await get_status(_fake_request(), "ws1")
        assert s.status == "running"

    @pytest.mark.asyncio
    async def test_stale_running_without_active_task_returns_not_built(
        self, tmp_path: Path
    ) -> None:
        # Status "running" no disco mas sem task ativa (processo morreu) → órfão.
        from backend.api.handlers import context_graph as cg
        from backend.api.handlers.context_graph import get_status

        graph_dir = _write_graph(tmp_path, {})
        _write_status(graph_dir, "running")
        registry, _ = _make_registry(tmp_path)
        cg._active_builds.pop("ws1", None)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            s = await get_status(_fake_request(), "ws1")
        assert s.status == "not_built"

    @pytest.mark.asyncio
    async def test_stale_running_with_checkpoint_returns_paused(
        self, tmp_path: Path
    ) -> None:
        # Órfão mas com checkpoint AST → oferece resume (paused), não do zero.
        from backend.api.handlers import context_graph as cg
        from backend.api.handlers.context_graph import get_status

        graph_dir = _write_graph(tmp_path, {})
        _write_status(graph_dir, "running")
        (graph_dir / "checkpoint_ast.json").write_text("{}", encoding="utf-8")
        registry, _ = _make_registry(tmp_path)
        cg._active_builds.pop("ws1", None)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            s = await get_status(_fake_request(), "ws1")
        assert s.status == "paused"

    @pytest.mark.asyncio
    async def test_error_from_status_file(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import get_status

        graph_dir = _write_graph(tmp_path, {})
        _write_status(graph_dir, "error", error="Pipeline falhou")
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            s = await get_status(_fake_request(), "ws1")
        assert s.status == "error"
        assert "Pipeline falhou" in (s.error or "")

    @pytest.mark.asyncio
    async def test_done_from_status_file(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import get_status

        graph_dir = _write_graph(tmp_path, {})
        _write_status(graph_dir, "done", node_count=10, edge_count=25)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            s = await get_status(_fake_request(), "ws1")
        assert s.status == "done"
        assert s.node_count == 10
        assert s.edge_count == 25

    @pytest.mark.asyncio
    async def test_falls_back_to_graph_json_when_no_status_file(
        self, tmp_path: Path
    ) -> None:
        from backend.api.handlers.context_graph import get_status

        _write_graph(tmp_path, {"nodes": [{"id": "a"}], "edges": []})
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            s = await get_status(_fake_request(), "ws1")
        assert s.status == "done"

    @pytest.mark.asyncio
    async def test_not_built_when_no_files(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import get_status

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            s = await get_status(_fake_request(), "ws1")
        assert s.status == "not_built"


# ---------------------------------------------------------------------------
# post_query
# ---------------------------------------------------------------------------


SAMPLE_DATA = {
    "nodes": [
        {"id": "n_auth", "label": "AuthService", "file_type": "code"},
        {"id": "n_login", "label": "login", "file_type": "code"},
        {"id": "n_token", "label": "Token", "file_type": "code"},
    ],
    "edges": [
        {
            "source": "n_auth",
            "target": "n_login",
            "relation": "calls",
            "label": "calls",
        },
        {
            "source": "n_login",
            "target": "n_token",
            "relation": "calls",
            "label": "returns",
        },
    ],
}


class TestPostQuery:
    @pytest.mark.asyncio
    async def test_matches_by_label(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import QueryRequest, post_query

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            resp = await post_query(
                _fake_request(), "ws1", QueryRequest(question="auth")
            )
        assert any(n["id"] == "n_auth" for n in resp.nodes)
        assert "1" in resp.answer

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import QueryRequest, post_query

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            resp = await post_query(
                _fake_request(), "ws1", QueryRequest(question="zzznothingmatches")
            )
        assert resp.nodes == []

    @pytest.mark.asyncio
    async def test_includes_neighborhood_edges(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import QueryRequest, post_query

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            resp = await post_query(
                _fake_request(), "ws1", QueryRequest(question="auth", top_k=5)
            )
        assert len(resp.edges) >= 1


# ---------------------------------------------------------------------------
# post_explain
# ---------------------------------------------------------------------------


class TestPostExplain:
    @pytest.mark.asyncio
    async def test_returns_node_and_neighbors(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import ExplainRequest, post_explain

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            resp = await post_explain(
                _fake_request(), "ws1", ExplainRequest(node_id="n_auth")
            )
        ids = {n["id"] for n in resp.nodes}
        assert "n_auth" in ids
        assert "n_login" in ids

    @pytest.mark.asyncio
    async def test_raises_404_for_unknown_node(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import ExplainRequest, post_explain

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                await post_explain(
                    _fake_request(), "ws1", ExplainRequest(node_id="ghost")
                )
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# post_path
# ---------------------------------------------------------------------------


class TestPostPath:
    @pytest.mark.asyncio
    async def test_returns_path_between_nodes(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import PathRequest, post_path

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            resp = await post_path(
                _fake_request(), "ws1", PathRequest(source="n_auth", target="n_token")
            )
        ids = {n["id"] for n in resp.nodes}
        assert "n_auth" in ids
        assert "n_token" in ids
        assert "n_auth" in resp.answer

    @pytest.mark.asyncio
    async def test_raises_404_when_no_path(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import PathRequest, post_path

        _write_graph(
            tmp_path,
            {
                "nodes": [
                    {"id": "a", "label": "A", "file_type": "code"},
                    {"id": "b", "label": "B", "file_type": "code"},
                ],
                "edges": [],
            },
        )
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                await post_path(
                    _fake_request(), "ws1", PathRequest(source="a", target="b")
                )
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_raises_404_for_unknown_source(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import PathRequest, post_path

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                await post_path(
                    _fake_request(), "ws1", PathRequest(source="ghost", target="n_auth")
                )
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# post_affected
# ---------------------------------------------------------------------------


class TestPostAffected:
    @pytest.mark.asyncio
    async def test_seed_found_returns_answer(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import AffectedRequest, post_affected

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            resp = await post_affected(
                _fake_request(), "ws1", AffectedRequest(node_query="login")
            )
        assert len(resp.answer) > 0

    @pytest.mark.asyncio
    async def test_no_graph_raises_404(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import AffectedRequest, post_affected

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                await post_affected(
                    _fake_request(), "ws1", AffectedRequest(node_query="anything")
                )
        assert exc.value.status_code == 404


class TestPostCluster:
    @pytest.mark.asyncio
    async def test_returns_community_members_without_persisting_them(
        self, tmp_path: Path
    ) -> None:
        from backend.api.handlers.context_graph import post_cluster

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        communities = {0: ["n_auth", "n_login"], 1: ["n_token"]}
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch("backend.context_graph.cluster.cluster", return_value=communities),
        ):
            response = await post_cluster(_fake_request(), "ws1")

        assert response.node_count == len(SAMPLE_DATA["nodes"])
        assert [item.id for item in response.communities] == [0, 1]
        assert response.communities[0].node_ids == ["n_auth", "n_login"]
        persisted = json.loads(
            (tmp_path / ".vectora" / "context-graph" / "graph.json").read_text()
        )
        assert all("community" not in node for node in persisted["nodes"])


class TestPostResolveSymbol:
    @pytest.mark.asyncio
    async def test_returns_exact_candidates_and_rejects_bad_query(
        self, tmp_path: Path
    ) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import (
            SymbolResolutionRequest,
            post_resolve_symbol,
        )

        graph = {
            "nodes": [
                {
                    "id": "one",
                    "label": "login",
                    "file_type": "code",
                    "source_file": "auth.py",
                },
                {
                    "id": "two",
                    "label": "login",
                    "file_type": "code",
                    "source_file": "admin.py",
                },
                {"id": "doc", "label": "login", "file_type": "document"},
            ],
            "links": [],
        }
        _write_graph(tmp_path, graph)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            result = await post_resolve_symbol(
                _fake_request(), "ws1", SymbolResolutionRequest(symbol="login")
            )
            assert result.status == "ambiguous"
            assert [item.id for item in result.candidates] == ["two", "one"]
            with pytest.raises(HTTPException) as exc:
                await post_resolve_symbol(
                    _fake_request(), "ws1", SymbolResolutionRequest(symbol=" ")
                )
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_requires_authentication_and_graph(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import (
            SymbolResolutionRequest,
            post_resolve_symbol,
        )

        request = _fake_request()
        request.state.user = None
        with pytest.raises(HTTPException) as exc:
            await post_resolve_symbol(
                request, "ws1", SymbolResolutionRequest(symbol="login")
            )
        assert exc.value.status_code == 401

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as missing:
                await post_resolve_symbol(
                    _fake_request(), "ws1", SymbolResolutionRequest(symbol="login")
                )
        assert missing.value.status_code == 404

    @pytest.mark.asyncio
    async def test_rejects_graph_over_the_processing_limit(
        self, tmp_path: Path
    ) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import post_cluster

        data = {"nodes": [{"id": str(i)} for i in range(10_001)], "links": []}
        _write_graph(tmp_path, data)
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                await post_cluster(_fake_request(), "ws1")
        assert exc.value.status_code == 413

    @pytest.mark.asyncio
    async def test_keeps_isolated_nodes_as_singleton_communities(
        self, tmp_path: Path
    ) -> None:
        from backend.api.handlers.context_graph import post_cluster

        _write_graph(tmp_path, {"nodes": [{"id": "n1"}, {"id": "n2"}], "links": []})
        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            response = await post_cluster(_fake_request(), "ws1")

        assert [community.node_ids for community in response.communities] == [
            ["n1"],
            ["n2"],
        ]

    @pytest.mark.asyncio
    async def test_sanitizes_detector_failure(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import post_cluster

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch(
                "backend.context_graph.cluster.cluster",
                side_effect=RuntimeError("private graph details"),
            ),
        ):
            with pytest.raises(HTTPException) as exc:
                await post_cluster(_fake_request(), "ws1")

        assert exc.value.status_code == 500
        assert exc.value.detail == "Não foi possível agrupar este grafo"

    @pytest.mark.asyncio
    async def test_rejects_a_second_concurrent_cluster_request(
        self, tmp_path: Path
    ) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import post_cluster
        from backend.context_graph import cluster_service

        _write_graph(tmp_path, SAMPLE_DATA)
        registry, _ = _make_registry(tmp_path)
        await cluster_service._cluster_semaphore.acquire()
        try:
            with patch("backend.workspace.workspace.workspace_registry", registry):
                with pytest.raises(HTTPException) as exc:
                    await post_cluster(_fake_request(), "ws1")
            assert exc.value.status_code == 429
        finally:
            cluster_service._cluster_semaphore.release()

    @pytest.mark.asyncio
    async def test_missing_graph_returns_404(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import post_cluster

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                await post_cluster(_fake_request(), "ws1")
        assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# post_build / delete_build
# ---------------------------------------------------------------------------


class TestPostBuild:
    @pytest.mark.asyncio
    async def test_enfileira_build_e_retorna_queued(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import BuildRequest, post_build

        registry, _ = _make_registry(tmp_path)
        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()

        with patch("backend.workspace.workspace.workspace_registry", registry):
            with patch(
                "backend.api.handlers.context_graph.asyncio.create_task",
                return_value=mock_task,
            ):
                resp = await post_build(_fake_request(), "ws1", BuildRequest())

        assert resp.status == "queued"
        mock_task.add_done_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_build_ja_em_andamento_retorna_running(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import BuildRequest, post_build

        graph_dir = tmp_path / ".vectora" / "context-graph"
        graph_dir.mkdir(parents=True)
        _write_status(graph_dir, "running")
        registry, _ = _make_registry(tmp_path)

        with patch("backend.workspace.workspace.workspace_registry", registry):
            resp = await post_build(_fake_request(), "ws1", BuildRequest())

        assert resp.status == "running"

    @pytest.mark.asyncio
    async def test_workspace_nao_encontrado_levanta_404(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import BuildRequest, post_build

        registry = MagicMock()
        registry.get = MagicMock(return_value=None)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with pytest.raises(HTTPException) as exc:
                await post_build(_fake_request(), "ws_miss", BuildRequest())

        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_resume_repassa_flag_ao_pipeline(self, tmp_path: Path) -> None:
        """resume=True no BuildRequest deve chegar ao build_workspace_graph via _run_build."""
        from backend.api.handlers.context_graph import BuildRequest, _run_build

        registry, _ = _make_registry(tmp_path)

        async def pipeline_ok(*_a, resume=False, **_k) -> MagicMock:
            return MagicMock(error=None, node_count=0, edge_count=0)

        mock_pipeline = AsyncMock(side_effect=pipeline_ok)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch(
                "backend.context_graph.pipeline.build_workspace_graph",
                new=mock_pipeline,
            ),
        ):
            await _run_build("ws1", BuildRequest(resume=True))

        assert mock_pipeline.call_count == 1
        _, kwargs = mock_pipeline.call_args
        assert kwargs.get("resume") is True

    @pytest.mark.asyncio
    async def test_file_types_repassa_ao_pipeline(self, tmp_path: Path) -> None:
        # file_types do BuildRequest deve chegar ao pipeline (None quando vazio).
        from backend.api.handlers.context_graph import BuildRequest, _run_build

        registry, _ = _make_registry(tmp_path)

        async def pipeline_ok(*_a, **_k) -> MagicMock:
            return MagicMock(error=None, node_count=0, edge_count=0)

        mock_pipeline = AsyncMock(side_effect=pipeline_ok)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch(
                "backend.context_graph.pipeline.build_workspace_graph",
                new=mock_pipeline,
            ),
        ):
            await _run_build("ws1", BuildRequest(file_types=["document"]))
            await _run_build("ws1", BuildRequest(file_types=[]))

        first = mock_pipeline.call_args_list[0].kwargs
        second = mock_pipeline.call_args_list[1].kwargs
        assert first.get("file_types") == ["document"]
        assert second.get("file_types") is None  # vazio → None (todos)


class TestDeleteBuild:
    @pytest.mark.asyncio
    async def test_cancela_task_ativa(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import delete_build

        graph_dir = tmp_path / ".vectora" / "context-graph"
        graph_dir.mkdir(parents=True)
        _write_status(graph_dir, "running")

        mock_task = MagicMock()
        mock_task.done = MagicMock(return_value=False)
        mock_task.cancel = MagicMock()

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with patch(
                "backend.api.handlers.context_graph._active_builds", {"ws1": mock_task}
            ):
                await delete_build(_fake_request(), "ws1")

        mock_task.cancel.assert_called_once()
        assert not (graph_dir / "build_status.json").exists()

    @pytest.mark.asyncio
    async def test_sem_task_ativa_nao_falha(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import delete_build

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            await delete_build(_fake_request(), "ws1")

    @pytest.mark.asyncio
    async def test_task_ja_concluida_nao_cancela(self, tmp_path: Path) -> None:
        from backend.api.handlers.context_graph import delete_build

        mock_task = MagicMock()
        mock_task.done = MagicMock(return_value=True)
        mock_task.cancel = MagicMock()

        registry, _ = _make_registry(tmp_path)
        with patch("backend.workspace.workspace.workspace_registry", registry):
            with patch(
                "backend.api.handlers.context_graph._active_builds", {"ws1": mock_task}
            ):
                await delete_build(_fake_request(), "ws1")

        mock_task.cancel.assert_not_called()


# ---------------------------------------------------------------------------
# _run_build — status final (paused/error/done)
# ---------------------------------------------------------------------------


class TestRunBuildStatus:
    async def _run(self, tmp_path: Path, side_effect):
        from backend.api.handlers.context_graph import BuildRequest, _run_build

        registry, _ = _make_registry(tmp_path)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch(
                "backend.context_graph.pipeline.build_workspace_graph",
                side_effect=side_effect,
            ),
        ):
            await _run_build("ws1", BuildRequest())
        from backend.api.handlers.context_graph import _read_status_file

        return _read_status_file(tmp_path / ".vectora" / "context-graph")

    @pytest.mark.asyncio
    async def test_quota_exhausted_writes_paused(self, tmp_path: Path) -> None:
        from backend.llm.provider_fallback import QuotaExhaustedError

        async def boom(*a, **k) -> NoReturn:
            raise QuotaExhaustedError("todos os providers esgotaram a quota")

        s = await self._run(tmp_path, boom)
        assert s is not None
        assert s.status == "paused"
        assert "esgotaram" in (s.error or "")

    @pytest.mark.asyncio
    async def test_generic_error_writes_error(self, tmp_path: Path) -> None:
        async def boom(*a, **k) -> NoReturn:
            raise ValueError("pipeline quebrou")

        s = await self._run(tmp_path, boom)
        assert s is not None
        assert s.status == "error"

    @pytest.mark.asyncio
    async def test_quota_not_classified_as_error(self, tmp_path: Path) -> None:
        from backend.llm.provider_fallback import QuotaExhaustedError

        async def boom(*a, **k) -> NoReturn:
            raise QuotaExhaustedError("quota")

        s = await self._run(tmp_path, boom)
        assert s is not None
        assert s.status != "error"

    @pytest.mark.asyncio
    async def test_success_writes_done_with_counts(self, tmp_path: Path) -> None:
        result = MagicMock()
        result.error = None
        result.node_count = 7
        result.edge_count = 9

        async def ok(*a, **k):
            return result

        s = await self._run(tmp_path, ok)
        assert s is not None
        assert s.status == "done"
        assert s.node_count == 7
        assert s.edge_count == 9

    @pytest.mark.asyncio
    async def test_result_error_writes_error(self, tmp_path: Path) -> None:
        result = MagicMock()
        result.error = "extração falhou"

        async def err(*a, **k):
            return result

        s = await self._run(tmp_path, err)
        assert s is not None
        assert s.status == "error"
        assert s.error == "extração falhou"

    @pytest.mark.asyncio
    async def test_paused_preserva_step_do_ultimo_on_progress(
        self, tmp_path: Path
    ) -> None:
        """Ao pausar por quota, step/step_total do último on_progress devem
        aparecer no status paused (para o frontend mostrar x/y passos)."""
        from backend.api.handlers.context_graph import BuildRequest, _run_build
        from backend.llm.provider_fallback import QuotaExhaustedError

        registry, _ = _make_registry(tmp_path)

        async def boom_after_progress(*a, on_progress=None, **k) -> NoReturn:
            if on_progress:
                on_progress(3, 9, "Análise semântica", 5, 10)
            raise QuotaExhaustedError("quota")

        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch(
                "backend.context_graph.pipeline.build_workspace_graph",
                side_effect=boom_after_progress,
            ),
        ):
            await _run_build("ws1", BuildRequest())

        from backend.api.handlers.context_graph import _read_status_file

        s = _read_status_file(tmp_path / ".vectora" / "context-graph")
        assert s is not None
        assert s.status == "paused"
        assert s.step == 3
        assert s.step_total == 9
        assert s.partial is True

    @pytest.mark.asyncio
    async def test_paused_sem_on_progress_nao_tem_step(self, tmp_path: Path) -> None:
        """Se quota estoura antes de qualquer on_progress, step deve ser None."""
        from backend.llm.provider_fallback import QuotaExhaustedError

        async def boom_imediato(*a, **k) -> NoReturn:
            raise QuotaExhaustedError("quota imediata")

        s = await self._run(tmp_path, boom_imediato)
        assert s is not None
        assert s.status == "paused"
        assert s.step is None
        assert s.partial is True


# ---------------------------------------------------------------------------
# _check_workspace_access — dependência do router que garante que um usuário
# autenticado só lê/muta o Context Graph de workspaces que possui.
# ---------------------------------------------------------------------------


class TestCheckWorkspaceAccess:
    def _fake_owned_workspace(self, owner_id: str | None) -> MagicMock:
        ws = MagicMock()
        ws.owner_id = owner_id
        return ws

    def test_raises_403_for_other_users_workspace(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import _check_workspace_access

        ws = self._fake_owned_workspace("alice")
        registry = MagicMock()
        registry.get = MagicMock(return_value=ws)
        req = _fake_request(user_id="bob")
        req.state.user.role = "member"

        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            pytest.raises(HTTPException) as exc,
        ):
            _check_workspace_access(req, "ws-1")
        assert exc.value.status_code == 403

    def test_allows_owner(self) -> None:
        from backend.api.handlers.context_graph import _check_workspace_access

        ws = self._fake_owned_workspace("alice")
        registry = MagicMock()
        registry.get = MagicMock(return_value=ws)
        req = _fake_request(user_id="alice")
        req.state.user.role = "member"

        with patch("backend.workspace.workspace.workspace_registry", registry):
            _check_workspace_access(req, "ws-1")  # não levanta

    def test_allows_workspace_without_owner(self) -> None:
        from backend.api.handlers.context_graph import _check_workspace_access

        ws = self._fake_owned_workspace(None)
        registry = MagicMock()
        registry.get = MagicMock(return_value=ws)
        req = _fake_request(user_id="bob")
        req.state.user.role = "member"

        with patch("backend.workspace.workspace.workspace_registry", registry):
            _check_workspace_access(req, "ws-1")  # não levanta — legado/sem dono

    def test_allows_root_regardless_of_owner(self) -> None:
        from backend.api.handlers.context_graph import _check_workspace_access

        ws = self._fake_owned_workspace("alice")
        registry = MagicMock()
        registry.get = MagicMock(return_value=ws)
        req = _fake_request(user_id="bob")
        req.state.user.role = "root"

        with patch("backend.workspace.workspace.workspace_registry", registry):
            _check_workspace_access(req, "ws-1")  # não levanta


class TestGraphExport:
    @pytest.mark.parametrize(
        "export_format", ["graphml", "cypher", "canvas", "svg", "obsidian"]
    )
    def test_exports_workspace_graph_to_download(
        self,
        tmp_path: Path,
        export_format: Literal["graphml", "cypher", "canvas", "svg", "obsidian"],
    ) -> None:
        from backend.api.handlers.context_graph import get_export

        graph_dir = _write_graph(
            tmp_path,
            {
                "directed": True,
                "nodes": [
                    {
                        "id": "module:a",
                        "label": "A",
                        "file_type": "python",
                        "community": 0,
                        "community_name": "Core",
                    },
                    {"id": "module:b", "label": "B", "community": 0},
                ],
                "links": [
                    {"source": "module:a", "target": "module:b", "relation": "calls"}
                ],
            },
        )
        registry, _ = _make_registry(tmp_path)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch("backend.context_graph.export.to_svg") as render_svg,
        ):
            render_svg.side_effect = lambda *_args, **_kwargs: Path(
                _args[2]
            ).write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"></svg>', encoding="utf-8"
            )
            response = get_export(_fake_request(), "ws1", export_format)

        try:
            output = Path(response.path).read_bytes()
            assert output
            filename = (
                "context-graph-obsidian.zip"
                if export_format == "obsidian"
                else f"graph.{export_format}"
            )
            assert response.headers["content-disposition"].endswith(
                f'filename="{filename}"'
            )
            if export_format == "graphml":
                assert b"<graphml" in output
            elif export_format == "cypher":
                assert b"MERGE" in output
            elif export_format == "canvas":
                exported = json.loads(output)
                assert exported["nodes"]
            elif export_format == "svg":
                assert b"<svg" in output
            elif export_format == "obsidian":
                import zipfile

                with zipfile.ZipFile(io.BytesIO(output)) as archive:
                    assert "Context Graph/A.md" in archive.namelist()
                    assert "[[B]]" in archive.read("Context Graph/A.md").decode()
        finally:
            if response.background is not None:
                import asyncio

                asyncio.run(response.background())
        assert not Path(response.path).exists()


class TestFalkorDBDestination:
    async def test_configuration_keeps_credentials_out_of_runtime_settings(
        self,
    ) -> None:
        from backend.api.handlers.context_graph import (
            FalkorDBConfigRequest,
            put_falkordb_config,
        )

        settings = MagicMock()
        provider = MagicMock()
        provider.set = AsyncMock()
        with (
            patch("backend.workspace.runtime_settings.runtime_settings", settings),
            patch("backend.secrets.get_secrets_provider", return_value=provider),
            patch(
                "backend.api.handlers.context_graph._validate_falkordb_destination",
                new=AsyncMock(return_value="203.0.113.8"),
            ),
        ):
            response = await put_falkordb_config(
                _fake_request(),
                "ws1",
                FalkorDBConfigRequest(
                    host="graph.example.com",
                    port=6379,
                    graph_name="workspace graph",
                    username="operator",
                    password="top-secret",
                ),
            )

        assert response.configured and response.has_password
        stored = settings.set.call_args.args[1]
        assert stored == {
            "host": "graph.example.com",
            "port": 6379,
            "graph_name": "workspace graph",
            "has_username": True,
            "has_password": True,
        }
        assert "top-secret" not in repr(stored)
        provider.set.assert_any_await(
            "u1", "CONTEXT_GRAPH_FALKORDB_PASSWORD", "top-secret"
        )

    async def test_rejects_destination_before_saving_credentials(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import (
            FalkorDBConfigRequest,
            put_falkordb_config,
        )

        provider = MagicMock()
        provider.set = AsyncMock()
        with (
            patch("backend.secrets.get_secrets_provider", return_value=provider),
            patch(
                "backend.api.handlers.context_graph._validate_falkordb_destination",
                new=AsyncMock(
                    side_effect=HTTPException(status_code=403, detail="bloqueado")
                ),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await put_falkordb_config(
                _fake_request(),
                "ws1",
                FalkorDBConfigRequest(
                    host="localhost", port=6379, password="must-not-save"
                ),
            )
        assert exc.value.status_code == 403
        provider.set.assert_not_awaited()

    async def test_push_requires_explicit_confirmation(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import (
            FalkorDBPushRequest,
            push_graph_to_falkordb,
        )

        with pytest.raises(HTTPException) as exc:
            await push_graph_to_falkordb(
                _fake_request(), "ws1", FalkorDBPushRequest(confirmed=False)
            )
        assert exc.value.status_code == 400

    async def test_push_uses_validated_destination_and_returns_counts(
        self, tmp_path: Path
    ) -> None:
        from backend.api.handlers.context_graph import (
            FalkorDBPushRequest,
            push_graph_to_falkordb,
        )

        settings = MagicMock()
        settings.get.return_value = {
            "host": "graph.example.com",
            "port": 6380,
            "graph_name": "workspace graph",
        }
        provider = MagicMock()
        provider.get = AsyncMock(return_value="credential")
        _write_graph(tmp_path, {"nodes": [{"id": "a"}], "links": []})
        with (
            patch("backend.workspace.runtime_settings.runtime_settings", settings),
            patch("backend.secrets.get_secrets_provider", return_value=provider),
            patch(
                "backend.api.handlers.context_graph._validate_falkordb_destination",
                new=AsyncMock(return_value="203.0.113.8"),
            ),
            patch(
                "backend.api.handlers.context_graph._require_graph_json",
                return_value={"nodes": [{"id": "a"}], "links": []},
            ),
            patch(
                "backend.api.handlers.context_graph._require_graph_dir",
                return_value=tmp_path / ".vectora" / "context-graph",
            ),
            patch("backend.context_graph.build.build_from_json", return_value=object()),
            patch(
                "backend.context_graph.export.push_to_falkordb",
                return_value={"nodes": 1, "edges": 0},
            ) as push,
        ):
            response = await push_graph_to_falkordb(
                _fake_request(), "ws1", FalkorDBPushRequest(confirmed=True)
            )

        assert response.nodes == 1 and response.edges == 0
        args = push.call_args.args
        assert args[1] == "redis://graph.example.com:6380"
        assert args[-1] is True  # TLS obrigatório no endpoint de envio
        assert push.call_args.kwargs["pinned_ip"] == "203.0.113.8"

    async def test_push_fails_closed_when_vault_is_locked(self) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import (
            FalkorDBPushRequest,
            push_graph_to_falkordb,
        )

        settings = MagicMock()
        settings.get.return_value = {
            "host": "graph.example.com",
            "port": 6379,
            "graph_name": "project",
        }
        provider = MagicMock()
        provider.get = AsyncMock(side_effect=RuntimeError("vault locked"))
        with (
            patch("backend.workspace.runtime_settings.runtime_settings", settings),
            patch("backend.secrets.get_secrets_provider", return_value=provider),
            patch(
                "backend.api.handlers.context_graph._validate_falkordb_destination",
                new=AsyncMock(return_value="203.0.113.8"),
            ),
            patch("backend.api.handlers.context_graph._require_graph_json") as graph,
            pytest.raises(HTTPException) as exc,
        ):
            await push_graph_to_falkordb(
                _fake_request(), "ws1", FalkorDBPushRequest(confirmed=True)
            )

        assert exc.value.status_code == 503
        assert "vault locked" not in str(exc.value.detail)
        graph.assert_not_called()

    async def test_push_sanitizes_remote_authentication_failure(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import (
            FalkorDBPushRequest,
            push_graph_to_falkordb,
        )

        settings = MagicMock()
        settings.get.return_value = {
            "host": "graph.example.com",
            "port": 6379,
            "graph_name": "project",
        }
        provider = MagicMock()
        provider.get = AsyncMock(side_effect=["operator", "remote-secret"])
        _write_graph(tmp_path, {"nodes": [{"id": "a"}], "links": []})
        with (
            patch("backend.workspace.runtime_settings.runtime_settings", settings),
            patch("backend.secrets.get_secrets_provider", return_value=provider),
            patch(
                "backend.api.handlers.context_graph._validate_falkordb_destination",
                new=AsyncMock(return_value="203.0.113.8"),
            ),
            patch(
                "backend.api.handlers.context_graph._require_graph_json",
                return_value={"nodes": [{"id": "a"}], "links": []},
            ),
            patch(
                "backend.api.handlers.context_graph._require_graph_dir",
                return_value=tmp_path / ".vectora" / "context-graph",
            ),
            patch("backend.context_graph.build.build_from_json", return_value=object()),
            patch(
                "backend.context_graph.export.push_to_falkordb",
                side_effect=RuntimeError("AUTH remote-secret failed"),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            await push_graph_to_falkordb(
                _fake_request(), "ws1", FalkorDBPushRequest(confirmed=True)
            )

        assert exc.value.status_code == 502
        assert "remote-secret" not in str(exc.value.detail)
        assert "remote-secret" not in caplog.text

    def test_exporter_pins_socket_and_keeps_tls_hostname(self) -> None:
        import socket
        import sys
        import types

        import networkx as nx

        from backend.context_graph.export import push_to_falkordb

        connections = {}
        queries = []

        class FakeSSLConnection:
            def __init__(self, **kwargs) -> None:
                self.__dict__.update(kwargs)

            def _wrap_socket_with_ssl(self, sock):
                host = self.__dict__["host"]
                connections["tls_hostname"] = host
                return (sock, host)

        class FakeConnectionPool:
            def __init__(self, **kwargs) -> None:
                connections["pool"] = kwargs

        class FakeGraph:
            def query(self, query, params) -> None:
                queries.append((query, params))

        class FakeFalkorDB:
            def __init__(self, *, connection_pool) -> None:
                self.connection_pool = connection_pool

            def select_graph(self, _name):
                return FakeGraph()

            def close(self) -> None:
                connections["closed"] = True

        falkordb_module = types.ModuleType("falkordb")
        vars(falkordb_module)["FalkorDB"] = FakeFalkorDB
        redis_module = types.ModuleType("redis")
        redis_module.__path__ = []
        redis_connection = types.ModuleType("redis.connection")
        vars(redis_connection)["ConnectionPool"] = FakeConnectionPool
        vars(redis_connection)["SSLConnection"] = FakeSSLConnection
        graph = nx.Graph()
        graph.add_node("A")

        with (
            patch.dict(
                sys.modules,
                {
                    "falkordb": falkordb_module,
                    "redis": redis_module,
                    "redis.connection": redis_connection,
                },
            ),
            patch.object(socket, "create_connection", return_value="socket") as connect,
        ):
            result = push_to_falkordb(
                graph,
                "redis://graph.example.com:6380",
                "operator",
                "secret",
                ssl=True,
                pinned_ip="203.0.113.8",
            )
            pool = connections["pool"]
            assert pool["host"] == "graph.example.com"
            assert pool["ssl_check_hostname"] is True
            connection = pool["connection_class"](
                validated_ip="203.0.113.8",
                host=pool["host"],
                port=pool["port"],
                socket_connect_timeout=5,
                source_address=None,
            )
            assert connection._connect() == ("socket", "graph.example.com")
            connect.assert_called_with(("203.0.113.8", 6380), 5, None)
        assert connections["tls_hostname"] == "graph.example.com"
        assert connections["closed"] is True
        assert len(queries) == 1
        assert result == {"nodes": 1, "edges": 0}


class TestGraphExportRemaining:
    def test_rejects_unknown_export_format(self) -> None:
        from typing import Literal, cast

        from fastapi import HTTPException

        from backend.api.handlers.context_graph import get_export

        with pytest.raises(HTTPException) as exc:
            get_export(
                _fake_request(),
                "ws1",
                cast(
                    "Literal['graphml', 'cypher', 'canvas', 'svg', 'obsidian']", "neo4j"
                ),
            )
        assert exc.value.status_code == 422

    def test_export_requires_existing_graph(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import get_export

        registry, _ = _make_registry(tmp_path)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            pytest.raises(HTTPException) as exc,
        ):
            get_export(_fake_request(), "ws1", "cypher")
        assert exc.value.status_code == 404

    def test_svg_reports_missing_optional_renderer(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import get_export

        _write_graph(tmp_path, {"nodes": [{"id": "a"}], "links": []})
        registry, _ = _make_registry(tmp_path)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            patch(
                "backend.context_graph.export.to_svg",
                side_effect=ImportError("matplotlib indisponível"),
            ),
            pytest.raises(HTTPException) as exc,
        ):
            get_export(_fake_request(), "ws1", "svg")
        assert exc.value.status_code == 501

    def test_svg_rejects_graph_over_layout_limit(self, tmp_path: Path) -> None:
        from fastapi import HTTPException

        from backend.api.handlers.context_graph import get_export

        _write_graph(
            tmp_path,
            {"nodes": [{"id": f"n{index}"} for index in range(501)], "links": []},
        )
        registry, _ = _make_registry(tmp_path)
        with (
            patch("backend.workspace.workspace.workspace_registry", registry),
            pytest.raises(HTTPException) as exc,
        ):
            get_export(_fake_request(), "ws1", "svg")
        assert exc.value.status_code == 413
