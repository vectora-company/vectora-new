"""Settings de RAG + gestão de coleções (aba de memória do workbench).

Endpoints (exigem auth via middleware):
    GET    /rag/settings             — settings de RAG em runtime
    PATCH  /rag/settings             — atualiza (reranker on/off, top_k, providers, tipos)
    GET    /rag/collections          — lista as coleções (tabelas LanceDB) e tamanho
    DELETE /rag/collections/{name}   — apaga uma coleção inteira
    GET    /rag/workspace-summary    — o que já está indexado num workspace específico
    POST   /rag/search               — busca direta do usuário (thin wrapper sobre vector_search)

Os settings persistem em ``runtime_settings`` (``~/.vectora/settings.json``) e são
lidos pelo build do reranker/embeddings (``backend/tools/rag.py``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from backend.vtypes.api import (
    CollectionCount,
    CollectionList,
    DeletedCollection,
    RagSearchResults,
)
from backend.vtypes.contracts import JsonObject
from backend.vtypes.services import RagSettings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"])


class RagSettingsBody(BaseModel):
    reranker_enabled: bool | None = None
    reranker_top_k: int | None = None
    rerank_model: str | None = None
    embed_model: str | None = None
    ingest_file_types: list[str] | None = None


@router.get("/settings")
async def get_rag_settings() -> RagSettings:
    from backend.workspace.runtime_settings import runtime_settings

    return runtime_settings.rag_settings


@router.patch("/settings")
async def patch_rag_settings(body: RagSettingsBody) -> RagSettings:
    from backend.workspace.runtime_settings import runtime_settings

    runtime_settings.set_rag_settings(**body.model_dump())
    return await get_rag_settings()


@router.get("/collections")
async def list_collections() -> CollectionList:
    from backend.storage.factory import get_vector_store_backend

    backend = await get_vector_store_backend()
    names = await backend.list_collections()
    collections: list[CollectionCount] = [
        {"name": name, "count": await backend.count(name)} for name in names
    ]
    return {"collections": collections}


@router.get("/workspace-summary")
async def get_workspace_rag_summary(
    request: Request, workspace_id: str
) -> CollectionList:
    """O que já está indexado NESTE workspace, por coleção.

    RAG é escopo de workspace (persiste no LanceDB entre sessões), não de
    thread — sem este endpoint, a aba Memória do workbench só via
    `ragCitations` da thread atual (evento de streaming) e mostrava "vazio"
    numa sessão nova mesmo com o workspace já indexado antes. `workspace_id`
    vive dentro do JSON de `metadata` de cada linha (não é coluna própria do
    LanceDB), então a contagem exige ler o metadata de cada linha — mesmo
    custo já pago por `manage_retriever(action="list")` em `tools/rag.py`.
    """
    from backend.api.handlers.workspaces import require_workspace_access
    from backend.storage.factory import get_vector_store_backend

    require_workspace_access(workspace_id, request)

    backend = await get_vector_store_backend()
    names = await backend.list_collections()

    summary: list[CollectionCount] = []
    for name in names:
        try:
            rows = await backend.list_rows(name)
        except Exception:
            # Coleção corrompida/sem tabela não deve derrubar as demais.
            logger.warning(
                "rag: falha ao ler coleção %r pro resumo do workspace",
                name,
                exc_info=True,
            )
            continue
        count = sum(
            1 for row in rows if row.metadata.get("workspace_id") == workspace_id
        )
        if count > 0:
            summary.append({"name": name, "count": count})
    return {"collections": summary}


def _ranking_score(result: JsonObject) -> float:
    """Relevância do reranking; sem ela, a distância vetorial invertida."""
    relevance = result.get("relevance_score")
    if isinstance(relevance, int | float) and relevance:
        return relevance
    score = result.get("score", 0)
    return -score if isinstance(score, int | float) else 0.0


class RagSearchBody(BaseModel):
    query: str
    workspace_id: str | None = None
    collection: str | None = None
    limit: int = 5


@router.post("/search")
async def search_rag(request: Request, body: RagSearchBody) -> RagSearchResults:
    """Busca direta do usuário na base RAG — mesma `vector_search` que o agente usa.

    Sem `collection`, busca em toda coleção indexada com o `workspace_id`
    dado (via `/rag/workspace-summary`); resultados de todas as coleções
    são combinados e ordenados por score.
    """
    import json as _json

    from backend.tools.context import ToolContext
    from backend.tools.rag import vector_search

    collections: list[str]
    if body.collection:
        collections = [body.collection]
    elif body.workspace_id:
        summary = await get_workspace_rag_summary(request, body.workspace_id)
        collections = [c["name"] for c in summary["collections"]]
    else:
        collections = ["articles"]

    if not collections:
        return {"results": []}

    all_results: list[JsonObject] = []
    ctx = ToolContext(workspace_id=body.workspace_id or "")
    for collection in collections:
        raw = await vector_search(
            ctx=ctx, query=body.query, collection=collection, limit=body.limit
        )
        try:
            parsed = _json.loads(raw)
        except ValueError:
            continue
        found = parsed.get("results", [])
        for r in found:
            r["collection"] = collection
            all_results.append(r)

    all_results.sort(key=_ranking_score, reverse=True)
    return {"results": all_results[: body.limit], "query": body.query}


@router.delete("/collections/{name}")
async def delete_collection(name: str) -> DeletedCollection:
    from backend.storage.factory import _embedding_meta_db, get_vector_store_backend

    backend = await get_vector_store_backend()
    try:
        await backend.purge(name)
    except Exception as exc:
        logger.warning("rag: falha ao apagar coleção %r", name, exc_info=True)
        raise HTTPException(
            status_code=404, detail=f"Coleção {name!r} não encontrada"
        ) from exc
    db = await _embedding_meta_db()
    await db.execute("DELETE FROM embedding_index_meta WHERE collection = ?", (name,))
    await db.commit()
    return {"status": "deleted", "name": name}
