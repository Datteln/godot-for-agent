"""语义分数与图距离/依赖强度联合重排。"""

from __future__ import annotations

import logging
import time

from app.rag.models import SearchResult
from app.rag.reranker import Reranker

logger = logging.getLogger(__name__)


class GraphAwareReranker(Reranker):
    def __init__(self, *args: object, alpha: float = 0.7, beta: float = 0.2, gamma: float = 0.1, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.alpha, self.beta, self.gamma = alpha, beta, gamma

    def rerank(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        started = time.perf_counter()
        # Graph factors must see the entire top-N candidate set, otherwise a nearby
        # graph result outside semantic top-K could never be promoted.
        final_top_k = self.top_k
        self.top_k = self.top_n
        try:
            semantic = super().rerank(query, results)
        finally:
            self.top_k = final_top_k
        for item in semantic:
            meta = item.graph_meta or {}
            distance = max(1, int(meta.get("graph_distance", 999)))
            graph_score = 0.0 if distance == 999 else 1.0 / distance
            dependency = float(meta.get("dependency_strength", 0.0))
            item.score = min(1.0, self.alpha * item.score + self.beta * graph_score + self.gamma * dependency)
        semantic.sort(key=lambda item: (-item.score, item.id))
        final = semantic[:final_top_k]
        graph_candidates = sum(
            "graph_distance" in (item.graph_meta or {}) for item in semantic
        )
        logger.debug(
            "Graph-aware rerank complete query_length=%d candidates=%d graph_candidates=%d "
            "results=%d alpha=%.2f beta=%.2f gamma=%.2f elapsed_ms=%.3f",
            len(query),
            len(results),
            graph_candidates,
            len(final),
            self.alpha,
            self.beta,
            self.gamma,
            (time.perf_counter() - started) * 1000,
        )
        return final
