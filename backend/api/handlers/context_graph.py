"""Handler REST do Context Graph — build, query e export por workspace.

Endpoints (todos exigem autenticação; workspace deve existir):
    POST   /workspaces/{workspace_id}/context-graph/build      — inicia build
    DELETE /workspaces/{workspace_id}/context-graph/build      — cancela build
    GET    /workspaces/{workspace_id}/context-graph/status     — status do build
    GET    /workspaces/{workspace_id}/context-graph            — graph.json
    GET    /workspaces/{workspace_id}/context-graph/report     — GRAPH_REPORT.md
    GET    /workspaces/{workspace_id}/context-graph/html       — graph.html
    POST   /workspaces/{workspace_id}/context-graph/query      — pergunta livre
    POST   /workspaces/{workspace_id}/context-graph/explain    — explica nó
    POST   /workspaces/{workspace_id}/context-graph/path       — caminho entre nós
    POST   /workspaces/{workspace_id}/context-graph/affected   — impacto de mudança
    POST   /workspaces/{workspace_id}/context-graph/cluster    — agrupa comunidades
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, TypeAdapter

from backend.vtypes.contracts import JsonObject, JsonValue, as_int

logger = logging.getLogger(__name__)

_active_builds: dict[str, asyncio.Task[None]] = {}


def _check_workspace_access(request: Request, workspace_id: str) -> None:
    """Dependência de router: 404/403 antes de qualquer handler rodar.

    Garante que o Context Graph de um workspace só é lido/mutado por quem
    tem acesso a esse workspace — o workspace_id sozinho não basta.
    """
    from backend.api.handlers.workspaces import require_workspace_access

    require_workspace_access(workspace_id, request)


router = APIRouter(
    prefix="/workspaces/{workspace_id}/context-graph",
    tags=["context-graph"],
    dependencies=[Depends(_check_workspace_access)],
)

_GRAPH_DIR = ".vectora/context-graph"
_STATUS_FILE = "build_status.json"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class BuildRequest(BaseModel):
    model: str = ""
    mode: str = "semantic"
    update: bool = False
    resume: bool = False
    # Tipos de arquivo a indexar ("code"/"document"/"paper"); vazio = todos.
    file_types: list[str] = []


class BuildResponse(BaseModel):
    status: str
    message: str


class StatusResponse(BaseModel):
    status: str
    node_count: int | None = None
    edge_count: int | None = None
    error: str | None = None
    step: int | None = None
    step_total: int | None = None
    step_label: str | None = None
    files_total: int | None = None
    files_done: int | None = None
    files_list: list[str] | None = None
    partial: bool = False
    provider_switched_to: str | None = None


class QueryRequest(BaseModel):
    question: str
    top_k: int = 10


class ExplainRequest(BaseModel):
    node_id: str
    depth: int = 1


class PathRequest(BaseModel):
    source: str
    target: str


class AffectedRequest(BaseModel):
    node_query: str
    depth: int = 2


class GraphCommunity(BaseModel):
    id: int
    name: str
    node_ids: list[str]


class ClusterResponse(BaseModel):
    communities: list[GraphCommunity]
    node_count: int


class SymbolResolutionRequest(BaseModel):
    symbol: str


class SymbolCandidate(BaseModel):
    id: str
    label: str
    source_file: str
    source_location: str
    community: int | None = None


class SymbolResolutionResponse(BaseModel):
    status: Literal["resolved", "ambiguous", "not_found"]
    query: str
    candidates: list[SymbolCandidate]
    total: int
    truncated: bool


_JSON_OBJECTS = TypeAdapter(list[JsonObject])


class GraphQueryResponse(BaseModel):
    answer: str
    nodes: list[JsonObject] = []
    edges: list[JsonObject] = []


class FalkorDBConfigRequest(BaseModel):
    host: str
    port: int = 6379
    graph_name: str = "context graph"
    username: str | None = None
    password: str | None = None


class FalkorDBConfigResponse(BaseModel):
    configured: bool
    host: str | None = None
    port: int | None = None
    graph_name: str | None = None
    has_username: bool = False
    has_password: bool = False


class FalkorDBPushRequest(BaseModel):
    confirmed: bool


class FalkorDBPushResponse(BaseModel):
    nodes: int
    edges: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user_id(request: Request) -> str:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Não autenticado")
    return str(user.id)


def _graph_dir(workspace_id: str) -> Path | None:
    from backend.api.handlers.workspaces import _resolve_inside

    return _resolve_inside(workspace_id, _GRAPH_DIR)


def _require_graph_dir(workspace_id: str) -> Path:
    d = _graph_dir(workspace_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Workspace não encontrado")
    return d


def _graph_nodes(data: JsonObject) -> list[JsonObject]:
    """Nós do ``graph.json`` que têm o formato de objeto."""
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _require_graph_json(workspace_id: str) -> JsonObject:
    d = _require_graph_dir(workspace_id)
    graph_file = d / "graph.json"
    if not graph_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Grafo não encontrado — execute o build primeiro",
        )
    try:
        return json.loads(graph_file.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.exception("context-graph: falha ao ler graph.json")
        raise HTTPException(status_code=500, detail="Grafo corrompido") from exc


_FALKORDB_CONFIG_PREFIX = "context_graph:falkordb:"


def _falkordb_config_key(user_id: str) -> str:
    return f"{_FALKORDB_CONFIG_PREFIX}{user_id}"


async def _falkordb_secrets(user_id: str) -> tuple[str | None, str | None]:
    from backend.secrets import get_secrets_provider

    provider = get_secrets_provider()
    username = await provider.get(user_id, "CONTEXT_GRAPH_FALKORDB_USERNAME")
    password = await provider.get(user_id, "CONTEXT_GRAPH_FALKORDB_PASSWORD")
    return username, password


async def _validate_falkordb_destination(
    host: str, port: int, workspace_id: str
) -> str:
    """Aplica allow/deny de egress e SSRF e retorna o IP resolvido validado."""
    from backend.services.egress_policy import EgressPolicy, async_is_url_allowed

    if not host or host != host.strip() or any(c in host for c in "/\\@?#"):
        raise HTTPException(status_code=422, detail="Host FalkorDB inválido")
    if not 1 <= port <= 65535:
        raise HTTPException(status_code=422, detail="Porta FalkorDB inválida")
    allowed, reason, ip = await async_is_url_allowed(
        f"https://{host}:{port}", EgressPolicy.for_workspace(workspace_id)
    )
    if not allowed or not ip:
        raise HTTPException(
            status_code=403,
            detail=f"Destino FalkorDB bloqueado pela política de egress: {reason}",
        )
    return ip


def _write_status(graph_dir: Path, status: str, **extra: JsonValue) -> None:
    payload: JsonObject = {
        "status": status,
        "built_at": datetime.now(UTC).isoformat(),
        **extra,
    }
    try:
        (graph_dir / _STATUS_FILE).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        logger.exception("context-graph: falha ao escrever build_status.json")


def _read_status_file(graph_dir: Path) -> StatusResponse | None:
    status_file = graph_dir / _STATUS_FILE
    if not status_file.exists():
        return None
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
        return StatusResponse(
            status=data.get("status", "unknown"),
            node_count=data.get("node_count"),
            edge_count=data.get("edge_count"),
            error=data.get("error"),
            step=data.get("step"),
            step_total=data.get("step_total"),
            step_label=data.get("step_label"),
            files_total=data.get("files_total"),
            files_done=data.get("files_done"),
            files_list=data.get("files_list"),
            partial=bool(data.get("partial", False)),
            provider_switched_to=data.get("provider_switched_to"),
        )
    except Exception:
        return None


async def _run_build(workspace_id: str, req: BuildRequest) -> None:
    d = _graph_dir(workspace_id)
    if d is None:
        return
    d.mkdir(parents=True, exist_ok=True)
    _write_status(
        d,
        "running",
        step=0,
        step_total=9,
        step_label="Iniciando...",
        files_total=0,
        files_done=0,
    )

    _state: JsonObject = {
        "files_list": [],
        "step": None,
        "step_total": None,
        "step_label": None,
        "files_total": 0,
        "files_done": 0,
    }

    def on_progress(
        step: int,
        step_total: int,
        label: str,
        files_done: int,
        files_total: int,
        files_list: list[str] | None = None,
    ) -> None:
        _state["step"] = step
        _state["step_total"] = step_total
        _state["step_label"] = label
        _state["files_done"] = files_done
        _state["files_total"] = files_total
        if files_list is not None:
            _state["files_list"] = [*files_list]
        kw: JsonObject = {
            "step": step,
            "step_total": step_total,
            "step_label": label,
            "files_total": files_total,
            "files_done": files_done,
        }
        if _state["files_list"]:
            kw["files_list"] = _state["files_list"]
        _write_status(d, "running", **kw)

    try:
        from backend.context_graph.pipeline import build_workspace_graph

        result = await build_workspace_graph(
            workspace_id,
            model=req.model,
            mode=req.mode,
            update=req.update,
            resume=req.resume,
            file_types=req.file_types or None,
            on_progress=on_progress,
        )
        if result.error:
            _write_status(d, "error", error=result.error)
        else:
            _write_status(
                d,
                "done",
                node_count=result.node_count,
                edge_count=result.edge_count,
            )
    except Exception as exc:
        from backend.llm.provider_fallback import QuotaExhaustedError

        if isinstance(exc, QuotaExhaustedError):
            # Quota esgotada em TODOS os providers — não é erro de pipeline:
            # pausa o build (grafo parcial servido) p/ o usuário renovar e retomar.
            logger.warning(
                "context-graph: quota esgotada em todos os providers — build pausado",
                extra={"workspace_id": workspace_id},
            )
            paused_kw: JsonObject = {"error": str(exc), "partial": True}
            if _state["step"] is not None:
                paused_kw["step"] = _state["step"]
                paused_kw["step_total"] = _state["step_total"]
                paused_kw["step_label"] = _state["step_label"]
                paused_kw["files_total"] = _state["files_total"]
                paused_kw["files_done"] = _state["files_done"]
            _write_status(d, "paused", **paused_kw)
            return
        logger.exception(
            "context-graph: falha no build em background",
            extra={"workspace_id": workspace_id},
        )
        _write_status(d, "error", error="Falha interna no pipeline")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/build", response_model=BuildResponse)
async def post_build(
    request: Request,
    workspace_id: str,
    body: BuildRequest,
) -> BuildResponse:
    _user_id(request)
    d = _require_graph_dir(workspace_id)

    status_resp = _read_status_file(d)
    if status_resp and status_resp.status == "running":
        return BuildResponse(status="running", message="Build já em andamento")

    task = asyncio.create_task(_run_build(workspace_id, body))
    _active_builds[workspace_id] = task
    task.add_done_callback(lambda _: _active_builds.pop(workspace_id, None))
    return BuildResponse(status="queued", message="Build enfileirado")


@router.delete("/build", status_code=204)
async def delete_build(request: Request, workspace_id: str) -> None:
    _user_id(request)
    task = _active_builds.pop(workspace_id, None)
    if task and not task.done():
        task.cancel()
    d = _graph_dir(workspace_id)
    if d is not None:
        status_file = d / _STATUS_FILE
        if status_file.exists():
            status_file.unlink()


@router.get("/status", response_model=StatusResponse)
async def get_status(request: Request, workspace_id: str) -> StatusResponse:
    _user_id(request)
    d = _graph_dir(workspace_id)
    if d is None:
        raise HTTPException(status_code=404, detail="Workspace não encontrado")

    from_file = _read_status_file(d)
    if from_file is not None:
        # Status "running"/"queued" sem task ativa neste processo é órfão (o build
        # morreu — restart/OOM). Não devolve spinner travado: se há checkpoint AST,
        # oferece resume ("paused"); senão, volta a "not_built" (botão Construir).
        if from_file.status in ("running", "queued") and (
            workspace_id not in _active_builds
        ):
            if (d / "checkpoint_ast.json").exists():
                _write_status(
                    d, "paused", error="Build interrompido — retome do checkpoint."
                )
                stale = _read_status_file(d)
                return stale if stale is not None else StatusResponse(status="paused")
            _write_status(d, "not_built")
            return StatusResponse(status="not_built")
        return from_file

    graph_file = d / "graph.json"
    if not graph_file.exists():
        return StatusResponse(status="not_built")
    try:
        data = json.loads(graph_file.read_text(encoding="utf-8"))
        return StatusResponse(
            status="done",
            node_count=len(data.get("nodes", [])),
            edge_count=len(data.get("edges", [])),
        )
    except Exception:
        return StatusResponse(status="error", error="graph.json ilegível")


@router.get("", response_model=dict)
async def get_graph(request: Request, workspace_id: str) -> dict:
    _user_id(request)
    return _require_graph_json(workspace_id)


@router.get("/report")
async def get_report(request: Request, workspace_id: str) -> dict[str, str]:
    _user_id(request)
    d = _require_graph_dir(workspace_id)
    report_file = d / "GRAPH_REPORT.md"
    if not report_file.exists():
        raise HTTPException(status_code=404, detail="Relatório não encontrado")
    return {"report": report_file.read_text(encoding="utf-8")}


@router.get("/html", response_class=HTMLResponse)
async def get_html(request: Request, workspace_id: str) -> HTMLResponse:
    _user_id(request)
    d = _require_graph_dir(workspace_id)
    html_file = d / "graph.html"
    if not html_file.exists():
        raise HTTPException(status_code=404, detail="HTML do grafo não encontrado")
    return HTMLResponse(
        content=html_file.read_text(encoding="utf-8"),
        headers={
            "Content-Security-Policy": (
                "sandbox allow-scripts; default-src 'none'; "
                "script-src 'unsafe-inline' https://unpkg.com; "
                "style-src 'unsafe-inline' https://unpkg.com; "
                "img-src data:; font-src data: https://unpkg.com; "
                "connect-src 'none'; form-action 'none'; base-uri 'none'; "
                "object-src 'none'; frame-ancestors 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/query", response_model=GraphQueryResponse)
async def post_query(
    request: Request, workspace_id: str, body: QueryRequest
) -> GraphQueryResponse:
    _user_id(request)
    data = _require_graph_json(workspace_id)

    from backend.context_graph.query import query_nodes

    matched_nodes, neighborhood_edges = query_nodes(
        data, body.question, top_k=body.top_k
    )
    matched_ids = {n.get("id") for n in matched_nodes}
    neighbor_ids = (
        {e.get("source") for e in neighborhood_edges}
        | {e.get("target") for e in neighborhood_edges}
    ) - matched_ids
    neighbor_nodes = [n for n in _graph_nodes(data) if n.get("id") in neighbor_ids]

    summary = f"Encontrei {len(matched_nodes)} nó(s) correspondente(s) à consulta."
    return GraphQueryResponse(
        answer=summary,
        nodes=_JSON_OBJECTS.validate_python([*matched_nodes, *neighbor_nodes]),
        edges=_JSON_OBJECTS.validate_python(neighborhood_edges),
    )


@router.post("/explain", response_model=GraphQueryResponse)
async def post_explain(
    request: Request, workspace_id: str, body: ExplainRequest
) -> GraphQueryResponse:
    _user_id(request)
    data = _require_graph_json(workspace_id)

    from backend.context_graph.query import explain_node

    target, neighbors, connected = explain_node(data, body.node_id, depth=body.depth)
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"Nó '{body.node_id}' não encontrado"
        )

    summary = (
        f"Nó: {target.get('label', body.node_id)} | "
        f"{len(connected)} arestas | {len(neighbors)} vizinhos"
    )
    return GraphQueryResponse(
        answer=summary,
        nodes=_JSON_OBJECTS.validate_python([target, *neighbors]),
        edges=_JSON_OBJECTS.validate_python(connected),
    )


@router.post("/path", response_model=GraphQueryResponse)
async def post_path(
    request: Request, workspace_id: str, body: PathRequest
) -> GraphQueryResponse:
    _user_id(request)
    data = _require_graph_json(workspace_id)

    from backend.context_graph.query import path_between

    path_nodes, path_edges = path_between(data, body.source, body.target)
    if not path_nodes:
        raise HTTPException(
            status_code=404,
            detail=f"Caminho não encontrado entre '{body.source}' e '{body.target}'",
        )

    summary = f"Caminho de {body.source} → {body.target}: {len(path_nodes)} nós"
    return GraphQueryResponse(answer=summary, nodes=path_nodes, edges=path_edges)


@router.post("/affected", response_model=GraphQueryResponse)
async def post_affected(
    request: Request, workspace_id: str, body: AffectedRequest
) -> GraphQueryResponse:
    _user_id(request)
    data = _require_graph_json(workspace_id)

    from backend.context_graph.query import affected_summary

    answer = affected_summary(data, body.node_query, depth=body.depth)
    return GraphQueryResponse(answer=answer, nodes=[], edges=[])


@router.post("/cluster", response_model=ClusterResponse)
async def post_cluster(request: Request, workspace_id: str) -> ClusterResponse:
    """Detecta comunidades sem alterar o grafo persistido do workspace."""
    _user_id(request)
    data = _require_graph_json(workspace_id)
    from backend.context_graph.cluster_service import (
        ClusterBusyError,
        ClusterTimeoutError,
        GraphTooLargeError,
        InvalidGraphDataError,
        cluster_graph_data,
    )

    try:
        result = await cluster_graph_data(data)
    except GraphTooLargeError as exc:
        raise HTTPException(
            status_code=413, detail="Grafo grande demais para agrupar pela interface"
        ) from exc
    except InvalidGraphDataError as exc:
        raise HTTPException(status_code=500, detail="Grafo inválido") from exc
    except ClusterBusyError as exc:
        raise HTTPException(
            status_code=429, detail="Outro agrupamento de grafo já está em execução"
        ) from exc
    except ClusterTimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="O agrupamento excedeu o tempo disponível"
        ) from exc
    except Exception as exc:
        logger.exception("context-graph: falha ao agrupar comunidades")
        raise HTTPException(
            status_code=500, detail="Não foi possível agrupar este grafo"
        ) from exc

    return ClusterResponse(
        communities=[
            GraphCommunity(
                id=community.id,
                name=community.name,
                node_ids=community.node_ids,
            )
            for community in result.communities
        ],
        node_count=result.node_count,
    )


@router.post("/resolve-symbol", response_model=SymbolResolutionResponse)
async def post_resolve_symbol(
    request: Request, workspace_id: str, body: SymbolResolutionRequest
) -> SymbolResolutionResponse:
    """Resolve an exact code symbol, returning ambiguity instead of guessing."""
    _user_id(request)
    data = _require_graph_json(workspace_id)
    from backend.context_graph.symbol_resolution import resolve_graph_symbol

    try:
        result = resolve_graph_symbol(_graph_nodes(data), body.symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return SymbolResolutionResponse.model_validate(result)


@router.get("/export/{export_format}")
def get_export(
    request: Request,
    workspace_id: str,
    export_format: Literal["graphml", "cypher", "canvas", "svg", "obsidian"],
) -> FileResponse:
    """Gera um arquivo de exportação temporário a partir do grafo autorizado."""
    import shutil
    import tempfile

    import networkx as nx

    from backend.context_graph import export as graph_export

    _user_id(request)
    supported = {
        "graphml": ("graph.graphml", "application/graphml+xml"),
        "cypher": ("graph.cypher", "text/plain; charset=utf-8"),
        "canvas": ("graph.canvas", "application/json"),
        "svg": ("graph.svg", "image/svg+xml"),
        "obsidian": ("context-graph-obsidian.zip", "application/zip"),
    }
    if export_format not in supported:
        raise HTTPException(status_code=422, detail="Formato de exportação inválido")
    graph_dir = _require_graph_dir(workspace_id)
    graph_file = graph_dir / "graph.json"
    if not graph_file.is_file():
        raise HTTPException(status_code=404, detail="Grafo não encontrado")
    from backend.context_graph.security import check_graph_file_size_cap

    try:
        check_graph_file_size_cap(graph_file)
    except ValueError as exc:
        raise HTTPException(status_code=413, detail="Grafo excede o limite") from exc
    data = _require_graph_json(workspace_id)
    nodes = data.get("nodes", [])
    links = data.get("links", data.get("edges", []))
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise HTTPException(status_code=422, detail="Estrutura do grafo inválida")
    node_limit = (
        500
        if export_format == "svg"
        else 50_000
        if export_format == "obsidian"
        else 100_000
    )
    edge_limit = 10_000 if export_format == "svg" else 500_000
    if len(nodes) > node_limit or len(links) > edge_limit:
        raise HTTPException(
            status_code=413, detail="Grafo excede o limite de exportação"
        )
    if (
        export_format in {"svg", "obsidian"}
        and graph_file.stat().st_size > 64 * 1024 * 1024
    ):
        raise HTTPException(
            status_code=413, detail="Grafo excede o limite deste formato de exportação"
        )

    graph_data = {**data, "links": links}
    try:
        graph = nx.node_link_graph(graph_data, edges="links")
    except TypeError:
        graph = nx.node_link_graph(graph_data)
    communities: dict[int, list[str]] = {}
    community_labels: dict[int, str] = {}
    for node_id, attrs in graph.nodes(data=True):
        cid = attrs.get("community")
        if cid is not None:
            try:
                cid = int(cid)
            except (TypeError, ValueError):
                continue
            communities.setdefault(cid, []).append(str(node_id))
            if attrs.get("community_name"):
                community_labels[cid] = str(attrs["community_name"])

    filename, media_type = supported[export_format]
    temp_dir = Path(tempfile.mkdtemp(prefix="vectora-context-graph-export-"))
    output = temp_dir / filename
    try:
        if export_format == "graphml":
            # GraphML só aceita atributos escalares; preserve estruturas como JSON.
            for key, value in list(graph.graph.items()):
                if value is None:
                    graph.graph[key] = ""
                elif not isinstance(value, (str, int, float, bool)):
                    graph.graph[key] = json.dumps(
                        value, ensure_ascii=False, default=str
                    )
            for _, attrs in graph.nodes(data=True):
                for key, value in list(attrs.items()):
                    if value is None:
                        attrs[key] = ""
                    elif not isinstance(value, (str, int, float, bool)):
                        attrs[key] = json.dumps(value, ensure_ascii=False, default=str)
            for _, _, attrs in graph.edges(data=True):
                for key, value in list(attrs.items()):
                    if value is None:
                        attrs[key] = ""
                    elif not isinstance(value, (str, int, float, bool)):
                        attrs[key] = json.dumps(value, ensure_ascii=False, default=str)
            graph_export.to_graphml(graph, communities, str(output))
        elif export_format == "cypher":
            graph_export.to_cypher(graph, str(output))
        elif export_format == "canvas":
            graph_export.to_canvas(
                graph, communities, str(output), community_labels=community_labels
            )
        elif export_format == "svg":
            graph_export.to_svg(
                graph, communities, str(output), community_labels=community_labels
            )
        else:
            import zipfile

            vault_dir = temp_dir / "Context Graph"
            graph_export.to_obsidian(
                graph,
                communities,
                str(vault_dir),
                community_labels=community_labels,
            )
            with zipfile.ZipFile(
                output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
            ) as archive:
                for note in sorted(vault_dir.glob("*.md")):
                    relative = note.relative_to(vault_dir)
                    if (
                        note.is_symlink()
                        or relative.is_absolute()
                        or ".." in relative.parts
                    ):
                        raise ValueError("O vault gerou um caminho de arquivo inválido")
                    archive.write(note, f"Context Graph/{relative.as_posix()}")
                    if output.stat().st_size > 128 * 1024 * 1024:
                        raise HTTPException(
                            status_code=413,
                            detail="Vault Obsidian excede o limite de exportação",
                        )
        if not output.is_file():
            raise RuntimeError("O exportador não produziu um arquivo")
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except ImportError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(
            status_code=501, detail=f"Dependência de exportação indisponível: {exc}"
        ) from exc
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.exception(
            "context-graph: falha ao exportar",
            extra={"format": export_format},
        )
        raise HTTPException(
            status_code=500, detail="Falha ao gerar exportação"
        ) from exc

    from starlette.background import BackgroundTask

    return FileResponse(
        output,
        media_type=media_type,
        filename=filename,
        background=BackgroundTask(lambda: shutil.rmtree(temp_dir, ignore_errors=True)),
    )


@router.get("/falkordb/config", response_model=FalkorDBConfigResponse)
async def get_falkordb_config(request: Request) -> FalkorDBConfigResponse:
    from backend.workspace.runtime_settings import runtime_settings

    user_id = _user_id(request)
    raw = runtime_settings.get(_falkordb_config_key(user_id))
    if not isinstance(raw, dict):
        return FalkorDBConfigResponse(configured=False)
    return FalkorDBConfigResponse(
        configured=True,
        host=str(raw.get("host", "")),
        port=as_int(raw.get("port", 6379)),
        graph_name=str(raw.get("graph_name", "context graph")),
        has_username=bool(raw.get("has_username", False)),
        has_password=bool(raw.get("has_password", False)),
    )


@router.put("/falkordb/config", response_model=FalkorDBConfigResponse)
async def put_falkordb_config(
    request: Request, workspace_id: str, body: FalkorDBConfigRequest
) -> FalkorDBConfigResponse:
    from backend.secrets import get_secrets_provider
    from backend.workspace.runtime_settings import runtime_settings

    user_id = _user_id(request)
    host = body.host.strip().lower().rstrip(".")
    graph_name = body.graph_name.strip()
    if (
        not graph_name
        or len(graph_name) > 128
        or any(ord(char) < 32 for char in graph_name)
    ):
        raise HTTPException(status_code=422, detail="Nome do grafo FalkorDB inválido")
    await _validate_falkordb_destination(host, body.port, workspace_id)

    provider = get_secrets_provider()
    try:
        if body.username is not None:
            if body.username:
                await provider.set(
                    user_id, "CONTEXT_GRAPH_FALKORDB_USERNAME", body.username
                )
            else:
                await provider.delete(user_id, "CONTEXT_GRAPH_FALKORDB_USERNAME")
        if body.password is not None:
            if body.password:
                await provider.set(
                    user_id, "CONTEXT_GRAPH_FALKORDB_PASSWORD", body.password
                )
            else:
                await provider.delete(user_id, "CONTEXT_GRAPH_FALKORDB_PASSWORD")
    except Exception as exc:
        logger.warning("context-graph: credencial FalkorDB não pôde ser salva")
        raise HTTPException(
            status_code=503,
            detail="O cofre de credenciais precisa estar desbloqueado para salvar a conexão",
        ) from exc

    previous = runtime_settings.get(_falkordb_config_key(user_id))
    previous = previous if isinstance(previous, dict) else {}
    has_username = (
        bool(body.username)
        if body.username is not None
        else bool(previous.get("has_username", False))
    )
    has_password = (
        bool(body.password)
        if body.password is not None
        else bool(previous.get("has_password", False))
    )
    runtime_settings.set(
        _falkordb_config_key(user_id),
        {
            "host": host,
            "port": body.port,
            "graph_name": graph_name,
            "has_username": has_username,
            "has_password": has_password,
        },
    )
    return FalkorDBConfigResponse(
        configured=True,
        host=host,
        port=body.port,
        graph_name=graph_name,
        has_username=has_username,
        has_password=has_password,
    )


@router.delete("/falkordb/config", response_model=FalkorDBConfigResponse)
async def delete_falkordb_config(request: Request) -> FalkorDBConfigResponse:
    from backend.secrets import get_secrets_provider
    from backend.workspace.runtime_settings import runtime_settings

    user_id = _user_id(request)
    provider = get_secrets_provider()
    try:
        await provider.delete(user_id, "CONTEXT_GRAPH_FALKORDB_USERNAME")
        await provider.delete(user_id, "CONTEXT_GRAPH_FALKORDB_PASSWORD")
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Não foi possível acessar o cofre de credenciais"
        ) from exc
    runtime_settings.set(_falkordb_config_key(user_id), None)
    return FalkorDBConfigResponse(configured=False)


@router.post("/falkordb/push", response_model=FalkorDBPushResponse)
async def push_graph_to_falkordb(
    request: Request, workspace_id: str, body: FalkorDBPushRequest
) -> FalkorDBPushResponse:
    from backend.secrets import get_secrets_provider
    from backend.workspace.runtime_settings import runtime_settings

    user_id = _user_id(request)
    if not body.confirmed:
        raise HTTPException(
            status_code=400, detail="A gravação exige confirmação explícita"
        )
    raw = runtime_settings.get(_falkordb_config_key(user_id))
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=409, detail="Configure um destino FalkorDB primeiro"
        )

    host = str(raw.get("host", ""))
    try:
        port = as_int(raw.get("port", 6379))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=409, detail="Configuração FalkorDB inválida"
        ) from exc
    pinned_ip = await _validate_falkordb_destination(host, port, workspace_id)
    try:
        username, password = await _falkordb_secrets(user_id)
    except Exception as exc:
        raise HTTPException(
            status_code=503, detail="Desbloqueie o cofre de credenciais antes de enviar"
        ) from exc

    data = _require_graph_json(workspace_id)
    graph_file = _require_graph_dir(workspace_id) / "graph.json"
    if graph_file.stat().st_size > 32 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Grafo excede o limite de envio")
    nodes = data.get("nodes", [])
    edges = data.get("links", data.get("edges", []))
    if (
        not isinstance(nodes, list)
        or not isinstance(edges, list)
        or len(nodes) > 10_000
        or len(edges) > 50_000
    ):
        raise HTTPException(status_code=413, detail="Grafo excede o limite de envio")

    from backend.context_graph import export as graph_export
    from backend.context_graph.build import build_from_json

    graph = build_from_json(data, directed=bool(data.get("directed", False)))
    community_nodes: dict[int, list[str]] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        community = node.get("community")
        if not isinstance(community, int):
            continue
        community_nodes.setdefault(community, []).append(str(node.get("id", "")))
    uri_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    uri = f"redis://{uri_host}:{port}"
    try:
        result = await asyncio.to_thread(
            graph_export.push_to_falkordb,
            graph,
            uri,
            username,
            password,
            community_nodes,
            str(raw.get("graph_name", "context graph")),
            True,
            pinned_ip=pinned_ip,
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=501, detail="A integração FalkorDB não está instalada"
        ) from exc
    except Exception as exc:
        logger.warning(
            "context-graph: gravação FalkorDB falhou (%s)", type(exc).__name__
        )
        raise HTTPException(
            status_code=502,
            detail="Falha ao gravar no FalkorDB; verifique a conexão e as credenciais",
        ) from exc
    return FalkorDBPushResponse(nodes=result["nodes"], edges=result["edges"])
