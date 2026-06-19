"""Engine Graph 上的有界 BFS 扩展与分数融合。"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Iterable

from app.rag.models import GraphEdge, GraphNode, SearchResult

logger = logging.getLogger(__name__)


class GraphFusion:
    def __init__(self, nodes: dict[str, GraphNode] | None = None, edges: Iterable[GraphEdge] = (), max_depth: int = 2, max_neighbors: int = 5) -> None:
        self.nodes = nodes or {}
        self.max_depth = max(0, max_depth)
        self.max_neighbors = max(1, max_neighbors)
        self.adjacency: dict[str, list[tuple[str, GraphEdge]]] = defaultdict(list)
        for edge in edges:
            self.adjacency[edge.source].append((edge.target, edge))
            self.adjacency[edge.target].append((edge.source, edge))

    def expand(self, seeds: list[SearchResult]) -> list[SearchResult]:
        started = time.perf_counter()
        output = list(seeds)
        seen = {seed.id for seed in seeds}
        for seed in seeds:
            starts = [seed.id]
            if seed.id not in self.nodes and seed.file_path:
                starts.extend(
                    node_id for node_id, node in self.nodes.items()
                    if node.file_path == seed.file_path
                )
            queue: deque[tuple[str, int]] = deque((node_id, 0) for node_id in starts)
            visited = set(starts)
            while queue:
                current, depth = queue.popleft()
                if depth >= self.max_depth:
                    continue
                neighbors = sorted(self.adjacency.get(current, []), key=lambda value: value[0])[: self.max_neighbors]
                for target, edge in neighbors:
                    if target in visited:
                        continue
                    visited.add(target)
                    queue.append((target, depth + 1))
                    node = self.nodes.get(target)
                    if node is None or target in seen:
                        continue
                    distance = depth + 1
                    output.append(SearchResult(
                        id=target, content=node.content or f"{node.kind}: {node.label}", source="scene_graph" if node.kind in {"scene", "scene_node"} else "signal_graph" if node.kind in {"signal", "receiver"} else "asset",
                        score=min(1.0, seed.score * edge.strength / (distance + 1)), file_path=node.file_path,
                        graph_meta={"graph_distance": distance, "edge_type": edge.edge_type, "expanded_from": seed.id, "dependency_strength": edge.strength},
                    ))
                    seen.add(target)
        logger.debug(
            "Graph expansion complete seeds=%d nodes=%d expanded=%d max_depth=%d "
            "max_neighbors=%d elapsed_ms=%.3f",
            len(seeds),
            len(self.nodes),
            len(output) - len(seeds),
            self.max_depth,
            self.max_neighbors,
            (time.perf_counter() - started) * 1000,
        )
        return output


def merge_graphs(*graphs: object) -> tuple[dict[str, GraphNode], list[GraphEdge]]:
    nodes: dict[str, GraphNode] = {}
    edges: list[GraphEdge] = []
    for graph in graphs:
        nodes.update(getattr(graph, "nodes", {}))
        edges.extend(getattr(graph, "edges", []))
    logger.debug("Engine graphs merged graphs=%d nodes=%d edges=%d", len(graphs), len(nodes), len(edges))
    return nodes, edges
