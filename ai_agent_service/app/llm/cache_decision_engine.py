"""上下文缓存的策略与断点决策（§16.1）。

把"用哪种缓存策略"、"标记哪些断点"、"这次前缀相对上一轮是否稳定"集中到这里：
`provider.py` 只负责按给定断点改写消息，`message_transformer.py` 只负责断点定位
算法，`cache_manager.py` 只负责指纹计算，本模块组合三者产出单一决策，供
`orchestrator/agent.py::run_turn` 直接消费。

策略采用三级降级（文档 3.10），把 token 阈值从"核心判断"降级为"兜底保护"：
- 前缀稳定（本帧上一轮见过同一缓存键）→ 显式缓存（注入 `cache_control` 断点），
  因为这段前缀确定会被复用，值得承担一次显式创建成本；
- 前缀不稳定但预估 token 数达到兜底阈值（如长 system prompt 的首轮）→ 显式缓存，
  作为"够大、大概率后续复用"的兜底；
- 前缀不稳定且未达兜底阈值、但达到隐式缓存最小长度 → 隐式缓存（不注入标记，
  依赖端点自动命中公共前缀，零创建成本）；
- 更短 → 不缓存。
"""

from __future__ import annotations

import asyncio
import enum
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.llm.cache_manager import (
    build_cache_key,
    compute_project_id,
    compute_rag_fingerprint,
    compute_repo_fingerprint,
    compute_system_core_hash,
    compute_tool_schema_version,
    read_graph_versions,
)
from app.llm.message_transformer import (
    CacheBreakpoint,
    build_stable_prefix,
    estimate_message_tokens,
)

logger = logging.getLogger(__name__)

# 显式缓存的"兜底"token 阈值（文档 3.5 fallback protection）：前缀不稳定但预估
# token 数达到该值时，仍按显式缓存处理。显式缓存创建按 125% 计价，太短的前缀
# 创建了也难有后续轮次复用，故兜底阈值取得相对高一些。
EXPLICIT_CACHE_FALLBACK_TOKENS = 1024

# 隐式缓存的最小前缀长度：低于此值时端点也不会自动缓存（百炼阿里云自部署模型
# 约 256 token），直接判为不缓存。token 在这里只是"够不够端点缓存"的下限保护，
# 不再是是否缓存的核心依据。
IMPLICIT_CACHE_MIN_TOKENS = 256

# 同时跟踪的 (session_id, frame_id) 上限；超出后按插入顺序淘汰最旧的一条，
# 避免长期运行的服务进程内存无界增长（同构于 `events/store.py` 的裁剪策略）。
_MAX_TRACKED_FRAMES = 4096


class CacheStrategy(enum.Enum):
    """本次请求采用的缓存策略（文档 3.10 三级降级）。"""

    EXPLICIT = "explicit"
    IMPLICIT = "implicit"
    NONE = "none"


@dataclass(frozen=True)
class CacheDecision:
    """`CacheDecisionEngine.decide` 的输出。

    Attributes:
        strategy: 三级降级后的缓存策略。
        breakpoints: `strategy=EXPLICIT` 时建议标记的断点；其余策略为空。
        cache_key: 本次请求的内部缓存指纹，不发给 LLM 端点，只用于跨轮次比较。
        prefix_stable: `cache_key` 与该帧上一轮记录的 `cache_key` 是否相同——
            相同意味着 system prompt/工具 schema/工程状态/RAG 都没变化，缓存
            大概率会被复用；不同则大概率本轮要新建缓存（百炼按 125% 计费）。
        stable_prefix_end_index: 稳定前缀结束的消息下标（见 `build_stable_prefix`）。
        segments_used: 与 `breakpoints` 等长的语义分段名，供日志/观测使用。
        repo_fingerprint: 本次决策使用的工程状态指纹，供观测层记录。
        tool_schema_version: 本次决策使用的工具 schema 哈希，供观测层记录。
        estimated_tokens: 本次前缀的预估 token 数。
    """

    strategy: CacheStrategy
    breakpoints: list[CacheBreakpoint] = field(default_factory=list)
    cache_key: str = ""
    prefix_stable: bool = False
    stable_prefix_end_index: int = 0
    segments_used: list[str] = field(default_factory=list)
    repo_fingerprint: str = ""
    tool_schema_version: str = ""
    estimated_tokens: int = 0

    @property
    def enabled(self) -> bool:
        """是否需要在请求体里注入显式缓存断点。"""
        return self.strategy is CacheStrategy.EXPLICIT and bool(self.breakpoints)


class CacheDecisionEngine:
    """跨轮次跟踪每个 agent 帧的缓存指纹，产出本次请求的缓存决策。"""

    def __init__(self) -> None:
        self._last_cache_key: dict[tuple[str, str], str] = {}

    async def decide(
        self,
        *,
        session_id: str,
        frame_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        project_root: Path,
        rag_index_path: Path | None = None,
        compact_digest: str = "",
    ) -> CacheDecision:
        """为一次 `LLMProvider.chat()` 调用产出缓存决策。

        Args:
            session_id: 当前会话 id。
            frame_id: 当前活跃帧 id；与 `session_id` 一起作为跟踪键，避免不同
                会话间偶然重用的帧 id 互相污染稳定性判断。
            messages: 即将发送的完整消息列表。
            tools: 当前帧可见的工具 schema 列表。
            project_root: 当前安全边界的工程根目录，用于计算 repo 指纹/project_id。
            rag_index_path: 本地 RAG 索引路径，用于计算 rag_fingerprint。
            compact_digest: 当前帧持久化压缩快照的内容指纹；未压缩时为空。

        Returns:
            本次请求的 `CacheDecision`；前缀过短（连隐式缓存下限都不到）时直接
            返回 `strategy=NONE`，不计算指纹（避免短对话也付出 git 调用开销）。
        """
        tokens = estimate_message_tokens(messages)
        if tokens < IMPLICIT_CACHE_MIN_TOKENS:
            return CacheDecision(strategy=CacheStrategy.NONE, estimated_tokens=tokens)

        tool_schema_version = compute_tool_schema_version(tools)
        system_core_hash = compute_system_core_hash(messages)
        repo_fingerprint = await asyncio.to_thread(compute_repo_fingerprint, project_root)
        project_id = compute_project_id(project_root)
        query = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                content = message.get("content", "")
                query = content if isinstance(content, str) else str(content)
                break
        rag_fingerprint = compute_rag_fingerprint(rag_index_path, query)
        scene_graph_version, asset_graph_version = read_graph_versions(rag_index_path)
        cache_key = build_cache_key(
            system_core_hash=system_core_hash,
            tool_schema_version=tool_schema_version,
            repo_fingerprint=repo_fingerprint,
            project_id=project_id,
            compact_digest=compact_digest,
            rag_fingerprint=rag_fingerprint,
            scene_graph_version=scene_graph_version,
            asset_graph_version=asset_graph_version,
        )

        track_key = (session_id, frame_id)
        previous = self._last_cache_key.get(track_key)
        prefix_stable = previous == cache_key
        self._last_cache_key[track_key] = cache_key
        if len(self._last_cache_key) > _MAX_TRACKED_FRAMES:
            oldest = next(iter(self._last_cache_key))
            del self._last_cache_key[oldest]

        plan = build_stable_prefix(messages)
        # 三级降级：稳定前缀或够大的前缀 → 显式；否则够长 → 隐式；再短不会到这里。
        if plan.breakpoints and (prefix_stable or tokens >= EXPLICIT_CACHE_FALLBACK_TOKENS):
            strategy = CacheStrategy.EXPLICIT
            breakpoints = plan.breakpoints
        else:
            strategy = CacheStrategy.IMPLICIT
            breakpoints = []

        logger.debug(
            "Cache decision session=%s frame=%s tokens=%d stable=%s strategy=%s breakpoints=%d",
            session_id,
            frame_id,
            tokens,
            prefix_stable,
            strategy.value,
            len(breakpoints),
        )
        return CacheDecision(
            strategy=strategy,
            breakpoints=breakpoints,
            cache_key=cache_key,
            prefix_stable=prefix_stable,
            stable_prefix_end_index=plan.stable_prefix_end_index,
            segments_used=[bp.segment for bp in breakpoints],
            repo_fingerprint=repo_fingerprint,
            tool_schema_version=tool_schema_version,
            estimated_tokens=tokens,
        )

    def invalidate(self, session_id: str, frame_ids: list[str] | None = None) -> None:
        """使会话帧的本地缓存稳定性记录失效。

        Args:
            session_id: 需要失效的会话 id。
            frame_ids: 指定帧 id；为 None 时失效该会话的全部已跟踪帧。
        """
        selected = set(frame_ids) if frame_ids is not None else None
        stale_keys = [
            key
            for key in self._last_cache_key
            if key[0] == session_id and (selected is None or key[1] in selected)
        ]
        for key in stale_keys:
            del self._last_cache_key[key]
