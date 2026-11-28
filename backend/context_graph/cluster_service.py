"""Resource-bounded community detection shared by API and agent tools."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

MAX_CLUSTER_NODES = 10_000
MAX_CLUSTER_EDGES = 40_000
CLUSTER_TIMEOUT_SECONDS = 30

_cluster_semaphore = asyncio.Semaphore(1)


from backend.vtypes.contracts import JsonObject


class ClusterBusyError(Exception):
    """Raised when another community detection job is already running."""


class ClusterTimeoutError(Exception):
    """Raised when community detection exceeds its time budget."""


class GraphTooLargeError(Exception):
    """Raised when a graph exceeds the supported interactive size."""


class InvalidGraphDataError(Exception):
    """Raised when the persisted graph has an invalid shape."""


@dataclass(frozen=True)
class Community:
    id: int
    name: str
    node_ids: list[str]


@dataclass(frozen=True)
class ClusterResult:
    communities: list[Community]
    node_count: int


def _release_when_background_task_finishes(task: asyncio.Task[ClusterResult]) -> None:
    if not task.cancelled():
        task.exception()
    _cluster_semaphore.release()


def _detect(data: JsonObject) -> ClusterResult:
    from backend.context_graph.build import build_from_json
    from backend.context_graph.cluster import cluster, label_communities_by_hub

    graph = build_from_json(data)
    communities = cluster(graph)
    labels = label_communities_by_hub(graph, communities)
    result = [
        Community(
            id=community_id,
            name=(
                labels.get(community_id, f"Comunidade {community_id}")
                .replace("\n", " ")
                .replace("\r", " ")[:120]
            ),
            node_ids=sorted(str(node_id) for node_id in node_ids),
        )
        for community_id, node_ids in sorted(communities.items())
    ]
    return ClusterResult(
        communities=result,
        node_count=sum(len(community.node_ids) for community in result),
    )


async def cluster_graph_data(data: JsonObject) -> ClusterResult:
    """Validate and cluster one graph without mutating its persisted JSON."""
    nodes = data.get("nodes")
    links = data.get("links", data.get("edges", []))
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise InvalidGraphDataError
    if len(nodes) > MAX_CLUSTER_NODES or len(links) > MAX_CLUSTER_EDGES:
        raise GraphTooLargeError
    if _cluster_semaphore.locked():
        raise ClusterBusyError

    await _cluster_semaphore.acquire()
    task = asyncio.create_task(asyncio.to_thread(_detect, data))
    try:
        result = await asyncio.wait_for(
            asyncio.shield(task), timeout=CLUSTER_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        task.add_done_callback(_release_when_background_task_finishes)
        raise ClusterTimeoutError from exc
    except asyncio.CancelledError:
        task.add_done_callback(_release_when_background_task_finishes)
        raise
    except BaseException:
        _cluster_semaphore.release()
        raise
    else:
        _cluster_semaphore.release()
        return result
