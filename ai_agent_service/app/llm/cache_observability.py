"""上下文缓存命中率的进程内观测（§16.1 非功能需求）。

`CacheMetricsCollector` 只在内存里聚合命中率并写日志，不持久化到会话存储——
符合"缓存命中率统计仅用于日志/监控，不写入会话持久化存储"的约束；进程重启
后聚合归零是可接受的，因为它不参与任何业务决策，纯粹用于观测。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CacheMetricsSnapshot:
    """单次 LLM 请求的缓存观测快照。

    Attributes:
        cache_key: 本次请求的内部缓存指纹（见 `cache_manager.build_cache_key`），
            不会发给 LLM 端点。
        repo_fingerprint: 当前工程根目录的状态指纹。
        tool_schema_version: 当前可见工具 schema 的哈希。
        cached_tokens: 本次命中缓存的 token 数。
        total_tokens: 本次请求的总输入 token 数。
        hit_ratio: `cached_tokens / total_tokens`；`total_tokens<=0` 时为 0。
        prefix_segments_used: 本次实际标记 `cache_control` 的语义分段名。
        cache_enabled: 本次是否启用了显式缓存标记（未达 token 阈值时为 False）。
    """

    cache_key: str
    repo_fingerprint: str
    tool_schema_version: str
    cached_tokens: int
    total_tokens: int
    hit_ratio: float
    prefix_segments_used: list[str] = field(default_factory=list)
    cache_enabled: bool = False


class CacheMetricsCollector:
    """进程内聚合缓存命中率，仅供日志/监控，不跨进程持久化。"""

    def __init__(self) -> None:
        self._total_cached = 0
        self._total_tokens = 0
        self._requests = 0

    def record(self, snapshot: CacheMetricsSnapshot) -> None:
        """记录一次快照并写入 INFO 级日志，同时累加进程级聚合命中率。"""
        self._requests += 1
        self._total_cached += max(snapshot.cached_tokens, 0)
        self._total_tokens += max(snapshot.total_tokens, 0)
        logger.info(
            "Cache metrics cache_key=%s repo_fingerprint=%s tool_version=%s "
            "cached_tokens=%d total_tokens=%d hit_ratio=%.4f segments=%s enabled=%s "
            "aggregate_hit_ratio=%.4f requests=%d",
            snapshot.cache_key,
            snapshot.repo_fingerprint,
            snapshot.tool_schema_version,
            snapshot.cached_tokens,
            snapshot.total_tokens,
            snapshot.hit_ratio,
            ",".join(snapshot.prefix_segments_used),
            snapshot.cache_enabled,
            self.aggregate_hit_ratio,
            self._requests,
        )

    @property
    def aggregate_hit_ratio(self) -> float:
        """进程启动以来的累计命中率；尚无请求时为 0。"""
        if self._total_tokens <= 0:
            return 0.0
        return self._total_cached / self._total_tokens
