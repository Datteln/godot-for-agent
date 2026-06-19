"""EARS 多路并行检索、图扩展、RRF 融合与最终重排。"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from collections import defaultdict
from collections.abc import Callable

from app.rag.graph_fusion import GraphFusion
from app.rag.models import SearchResult
from app.rag.query_router import QueryRouter
from app.rag.reranker import Reranker

logger = logging.getLogger(__name__)

RETRIEVAL_STRATEGY_VERSION = "ears-v2.2-rrf60-vec0.5-kw0.3-sym0.2-graph2"
DEFAULT_WEIGHTS = {"vec": 0.5, "kw": 0.3, "sym": 0.2, "scene_graph": 0.3, "signal_graph": 0.3, "asset": 0.3}


class HybridRetriever:
    def __init__(
        self,
        channels: dict[str, Callable[[str, int], list[SearchResult]]],
        *,
        router: QueryRouter | None = None,
        graph_fusion: GraphFusion | None = None,
        reranker: Reranker | None = None,
        weights: dict[str, float] | None = None,
        candidate_limit: int = 10,
        final_limit: int = 4,
    ) -> None:
        self.channels = channels
        self.router = router or QueryRouter(enabled=False)
        self.graph_fusion = graph_fusion
        self.reranker = reranker or Reranker(top_n=candidate_limit, top_k=final_limit)
        self.weights = {**DEFAULT_WEIGHTS, **(weights or {})}
        self.candidate_limit = candidate_limit
        self.final_limit = final_limit
        self.last_metrics: dict[str, object] = {}

    def search(self, query: str) -> list[SearchResult]:
        started = time.perf_counter()
        route = self.router.route(query)
        selected = {name: fn for name, fn in self.channels.items() if name in route.channels}
        per_channel: dict[str, list[SearchResult]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            futures = {pool.submit(fn, query, self.candidate_limit): name for name, fn in selected.items()}
            for future, name in [(future, name) for future, name in futures.items()]:
                try:
                    per_channel[name] = future.result()
                except Exception as exc:
                    logger.warning("Retrieval channel %s unavailable: %s", name, exc)
        fused = self._rrf(per_channel)
        expanded_count = 0
        if self.graph_fusion:
            expanded = self.graph_fusion.expand(fused)
            expanded_count = max(0, len(expanded) - len(fused))
            fused = self._rrf({"fused": fused, "expanded": expanded})
        # Rerank is deliberately the last sorting operation in the pipeline.
        rerank_started = time.perf_counter()
        results = self.reranker.rerank(query, fused) if fused else []
        rerank_ms = (time.perf_counter() - rerank_started) * 1000
        total_ms = (time.perf_counter() - started) * 1000
        counts = {name: len(values) for name, values in per_channel.items()}
        self.last_metrics = {
            "route": route.type.value,
            "channels": sorted(per_channel),
            "counts": counts,
            "graph_depth": self.graph_fusion.max_depth if self.graph_fusion else 0,
            "expanded_count": expanded_count,
            "rerank_ms": round(rerank_ms, 3),
            "total_ms": round(total_ms, 3),
            "result_count": len(results),
        }
        logger.info(
            "EARS retrieval route=%s channels=%s counts=%s graph_depth=%d expanded=%d "
            "rerank_ms=%.3f total_ms=%.3f results=%d",
            route.type.value,
            sorted(per_channel),
            counts,
            self.graph_fusion.max_depth if self.graph_fusion else 0,
            expanded_count,
            rerank_ms,
            total_ms,
            len(results),
        )
        return results

    def _rrf(self, groups: dict[str, list[SearchResult]]) -> list[SearchResult]:
        scores: defaultdict[str, float] = defaultdict(float)
        best: dict[str, SearchResult] = {}
        aliases: dict[tuple[str, int], str] = {}
        for channel, results in groups.items():
            weight = self.weights.get(channel, 1.0)
            for rank, item in enumerate(results, 1):
                overlap_key = (item.file_path, item.span[0])
                canonical = aliases.setdefault(overlap_key, item.id) if item.file_path and item.span != (0, 0) else item.id
                scores[canonical] += weight / (60 + rank)
                current = best.get(canonical)
                if current is None or item.score > current.score:
                    best[canonical] = item
        if not scores:
            return []
        maximum = max(scores.values()) or 1.0
        output = []
        for key, score in scores.items():
            item = best[key]
            item.score = score / maximum
            output.append(item)
        output.sort(key=lambda item: (-item.score, item.id))
        return output[: self.candidate_limit]
