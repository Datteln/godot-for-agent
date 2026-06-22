"""可降级的 OpenAI/本地 embedding 客户端。"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 重试退避：基准 0.2s，指数增长并封顶 2s，叠加 ±50% 抖动，避免服务限流/短暂
# 5xx 时连续立即重试反而加重故障（§P3 重试无退避）。
_RETRY_BASE_DELAY_S = 0.2
_RETRY_MAX_DELAY_S = 2.0
_OPENAI_MAX_BATCH_SIZE = 10


def _retry_delay_s(attempt: int) -> float:
    """第 `attempt`（从 0 起）次失败后的退避时长，含抖动。"""
    capped = min(_RETRY_BASE_DELAY_S * (2 ** attempt), _RETRY_MAX_DELAY_S)
    return capped * (0.5 + random.random())


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "disabled"
    model: str = "text-embedding-3-small"
    endpoint: str = "https://api.openai.com/v1"
    api_key: str = ""
    timeout_s: float = 3.0
    retries: int = 1


class EmbeddingClient:
    """同步核心 + 异步线程池包装；不可用时返回空列表。"""

    def __init__(
        self,
        config: EmbeddingConfig | None = None,
        encoder: Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None,
    ) -> None:
        self.config = config or EmbeddingConfig()
        self._encoder = encoder
        self._model: Any = None

    @property
    def available(self) -> bool:
        return self._encoder is not None or self.config.provider.lower() in {"openai", "local", "bge-m3"}

    def _embed_openai(self, texts: Sequence[str]) -> list[list[float]]:
        """按兼容接口上限分批请求 Embedding，并保持输入输出顺序一致。"""
        from openai import OpenAI

        client = OpenAI(
            api_key=self.config.api_key,
            base_url=self.config.endpoint,
            timeout=self.config.timeout_s,
        )
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _OPENAI_MAX_BATCH_SIZE):
            batch = list(texts[start : start + _OPENAI_MAX_BATCH_SIZE])
            response = client.embeddings.create(model=self.config.model, input=batch)
            ordered = sorted(response.data, key=lambda item: item.index)
            if len(ordered) != len(batch):
                raise ValueError(
                    f"Embedding 返回数量不匹配: requested={len(batch)} received={len(ordered)}"
                )
            vectors.extend([list(item.embedding) for item in ordered])
            logger.debug(
                "Embedding batch complete model=%s batch_start=%d batch_size=%d",
                self.config.model,
                start,
                len(batch),
            )
        return vectors

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts or not self.available:
            logger.debug(
                "Embedding request skipped texts=%d provider=%s available=%s",
                len(texts),
                self.config.provider,
                self.available,
            )
            return []
        started = time.perf_counter()
        attempts = max(1, min(self.config.retries + 1, 3))
        for attempt in range(attempts):
            try:
                if self._encoder is not None:
                    vectors = [list(map(float, row)) for row in self._encoder(texts)]
                    logger.debug(
                        "Embedding request complete provider=injected texts=%d dimensions=%d "
                        "attempt=%d elapsed_ms=%.3f",
                        len(texts),
                        len(vectors[0]) if vectors else 0,
                        attempt + 1,
                        (time.perf_counter() - started) * 1000,
                    )
                    return vectors
                provider = self.config.provider.lower()
                if provider == "openai":
                    vectors = self._embed_openai(texts)
                    logger.debug(
                        "Embedding request complete provider=openai model=%s texts=%d dimensions=%d "
                        "attempt=%d elapsed_ms=%.3f",
                        self.config.model,
                        len(texts),
                        len(vectors[0]) if vectors else 0,
                        attempt + 1,
                        (time.perf_counter() - started) * 1000,
                    )
                    return vectors
                from sentence_transformers import (
                    SentenceTransformer,  # type: ignore[import-not-found]
                )

                if self._model is None:
                    self._model = SentenceTransformer(self.config.model or "BAAI/bge-m3")
                vectors = self._model.encode(list(texts), normalize_embeddings=True)
                result = [list(map(float, row)) for row in vectors]
                logger.debug(
                    "Embedding request complete provider=local model=%s texts=%d dimensions=%d "
                    "attempt=%d elapsed_ms=%.3f",
                    self.config.model,
                    len(texts),
                    len(result[0]) if result else 0,
                    attempt + 1,
                    (time.perf_counter() - started) * 1000,
                )
                return result
            except Exception as exc:  # optional enhancement must never break indexing
                if attempt + 1 >= attempts:
                    logger.warning("Embedding unavailable; falling back to keyword retrieval: %s", exc)
                else:
                    delay = _retry_delay_s(attempt)
                    logger.debug(
                        "Embedding attempt %d failed; backing off %.3fs before retry: %s",
                        attempt + 1,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
        return []

    async def embed_async(self, texts: Sequence[str]) -> list[list[float]]:
        """在线程池执行 Embedding，并按实际批次数计算整体超时。"""
        attempts = max(1, min(self.config.retries + 1, 3))
        batch_count = 1
        if self._encoder is None and self.config.provider.lower() == "openai":
            batch_count = max(1, (len(texts) + _OPENAI_MAX_BATCH_SIZE - 1) // _OPENAI_MAX_BATCH_SIZE)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self.embed, texts),
                timeout=self.config.timeout_s * attempts * batch_count,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Embedding timed out; falling back to keyword retrieval")
            return []
