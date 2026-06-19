"""可选 cross-encoder 重排，失败/超时自动降级。"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from collections.abc import Callable

from app.rag.models import SearchResult

logger = logging.getLogger(__name__)


class Reranker:
    def __init__(self, scorer: Callable[[str, list[str]], list[float]] | None = None, model: str = "", timeout_s: float = 2.0, top_n: int = 10, top_k: int = 4) -> None:
        self.scorer = scorer
        self.model = model
        self.timeout_s = timeout_s
        self.top_n = top_n
        self.top_k = top_k
        self._loaded_model: object | None = None

    def _score(self, query: str, contents: list[str]) -> list[float]:
        if self.scorer:
            return self.scorer(query, contents)
        if not self.model:
            return []
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

        if self._loaded_model is None:
            self._loaded_model = CrossEncoder(self.model)
        return [float(value) for value in self._loaded_model.predict([(query, text) for text in contents])]  # type: ignore[attr-defined]

    def rerank(self, query: str, results: list[SearchResult]) -> list[SearchResult]:
        started = time.perf_counter()
        candidates = results[: self.top_n]
        if not candidates:
            logger.debug("Rerank skipped query_length=%d reason=no_candidates", len(query))
            return []
        pool: concurrent.futures.ThreadPoolExecutor | None = None
        try:
            pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            scores = pool.submit(
                self._score, query, [item.content for item in candidates]
            ).result(timeout=self.timeout_s)
            if len(scores) != len(candidates):
                logger.debug(
                    "Rerank skipped query_length=%d reason=no_model_scores candidates=%d "
                    "scores=%d",
                    len(query),
                    len(candidates),
                    len(scores),
                )
                return candidates[: self.top_k]
            lo, hi = min(scores), max(scores)
            normalized = [(score - lo) / (hi - lo) if hi > lo else 1.0 for score in scores]
            for item, score in zip(candidates, normalized, strict=True):
                item.score = score
            candidates.sort(key=lambda item: (-item.score, item.id))
        except Exception as exc:
            logger.warning("Reranker unavailable; using fused order: %s", exc)
        finally:
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
        final = candidates[: self.top_k]
        logger.debug(
            "Rerank complete query_length=%d candidates=%d results=%d model_enabled=%s "
            "elapsed_ms=%.3f",
            len(query),
            len(candidates),
            len(final),
            bool(self.model or self.scorer),
            (time.perf_counter() - started) * 1000,
        )
        return final
