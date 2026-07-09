"""Session compact implementation split out of QueryEngine."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Literal

from app.agents.types import CompactSnapshot
from app.config import AppSettings
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.message_transformer import estimate_message_tokens
from app.llm.provider import LLMError, LLMProvider
from app.query.helpers import (
    _compact_digest,
    _compact_summary_text,
    _inject_compact_snapshot,
    _mechanical_summary_body,
    _pending_anchor_index,
    _truncate_oversized_message,
    _wrap_compact_summary,
)
from app.sessions.store import Session, SessionStore

logger = logging.getLogger(__name__)


class SessionCompactor:
    """压缩会话历史并维护 compact snapshot/cache 状态。"""

    def __init__(
        self,
        settings: AppSettings,
        store: SessionStore,
        llm: LLMProvider,
        cache_engine: CacheDecisionEngine,
        emit: Callable[[str, str, dict[str, Any]], int],
        available_tools: Callable[[], set[str]],
        model_for_effort: Callable[[str], str | None],
    ) -> None:
        """保存 compact 所需依赖。"""
        self._settings = settings
        self._store = store
        self._llm = llm
        self._cache_engine = cache_engine
        self._emit = emit
        self._available_tools = available_tools
        self._model_for_effort = model_for_effort

    def needs_auto_compact(self, session: Session) -> bool:
        """判断当前会话是否超过自动压缩阈值。"""
        threshold = self._settings.auto_compact_token_threshold
        local_estimate = max(
            (estimate_message_tokens(frame.messages) for frame in session.agent_stack),
            default=0,
        )
        effective_tokens = max(local_estimate, session.latest_context_used_tokens)
        return session.force_compact_next_turn or effective_tokens > threshold

    async def compact_locked(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """在调用方已持有会话锁时执行压缩。"""
        session = self._store.get_or_create(session_id, self._available_tools())
        trigger: Literal["manual", "auto"] = "auto" if triggered_by == "auto" else "manual"
        summary_use_llm = self._settings.compact_summary_use_llm if use_llm is None else use_llm
        logger.info(
            "Compacting session session=%s keep_recent=%d triggered_by=%s",
            session_id,
            keep_recent,
            trigger,
        )
        compacted_frames = 0
        removed_messages = 0
        truncated_messages = 0
        keep = max(6, keep_recent)
        modified_frame_ids: list[str] = []
        snapshot_payloads: list[dict[str, Any]] = []
        estimated_tokens_before = 0
        estimated_tokens_after = 0
        backups = [
            (frame, copy.deepcopy(frame.messages), copy.deepcopy(frame.compact_snapshot))
            for frame in session.agent_stack
        ]

        self._emit(
            session_id,
            "compact_started",
            {
                "keep_recent": keep,
                "triggered_by": trigger,
                "frame_ids": [frame.id for frame in session.agent_stack],
            },
        )

        for frame in session.agent_stack:
            frame_tokens_before = estimate_message_tokens(frame.messages)
            estimated_tokens_before += frame_tokens_before
            frame_changed = False
            anchor = _pending_anchor_index(frame, session.pending_tool_call_ids)

            scan_end = len(frame.messages) - 1
            if anchor is not None:
                scan_end = min(scan_end, anchor)
            for index in range(1, scan_end):
                replacement = _truncate_oversized_message(frame.messages[index])
                if replacement is not None:
                    frame.messages[index] = replacement
                    truncated_messages += 1
                    frame_changed = True

            if len(frame.messages) <= keep + 1:
                estimated_tokens_after += estimate_message_tokens(frame.messages)
                if frame_changed:
                    modified_frame_ids.append(frame.id)
                continue
            default_start = max(1, len(frame.messages) - keep)
            keep_from = min(default_start, anchor) if anchor is not None else default_start
            if keep_from <= 1:
                estimated_tokens_after += estimate_message_tokens(frame.messages)
                if frame_changed:
                    modified_frame_ids.append(frame.id)
                continue

            old_messages = frame.messages[1:keep_from]
            if (
                trigger == "auto"
                and len(old_messages) < self._settings.auto_compact_min_new_messages
            ):
                estimated_tokens_after += estimate_message_tokens(frame.messages)
                if frame_changed:
                    modified_frame_ids.append(frame.id)
                continue
            previous = frame.compact_snapshot
            summary = await self._build_compact_summary(
                previous, old_messages, use_llm=summary_use_llm
            )
            digest = _compact_digest(summary)
            revision = (
                previous.revision
                if previous is not None and previous.digest == digest
                else previous.revision + 1 if previous is not None else 1
            )
            frame.messages = [frame.messages[0], *frame.messages[keep_from:]]
            snapshot = CompactSnapshot(
                revision=revision,
                digest=digest,
                summary=summary,
                created_at=datetime.now(timezone.utc).isoformat(),
                source_message_count=(previous.source_message_count if previous is not None else 0)
                + len(old_messages),
                removed_message_count=len(old_messages),
                keep_recent=keep,
                estimated_tokens_before=frame_tokens_before,
                estimated_tokens_after=0,
                triggered_by=trigger,
            )
            frame.compact_snapshot = snapshot
            _inject_compact_snapshot(
                frame,
                has_rag_context=(
                    frame is session.agent_stack[0] and bool(session.rag_context.strip())
                ),
            )
            frame_tokens_after = estimate_message_tokens(frame.messages)
            snapshot = replace(snapshot, estimated_tokens_after=frame_tokens_after)
            frame.compact_snapshot = snapshot
            estimated_tokens_after += frame_tokens_after
            frame_changed = True
            compacted_frames += 1
            removed_messages += len(old_messages)
            modified_frame_ids.append(frame.id)
            snapshot_payloads.append(
                {
                    "frame_id": frame.id,
                    "revision": revision,
                    "digest": digest,
                    "source_message_count": snapshot.source_message_count,
                    "removed_message_count": len(old_messages),
                    "estimated_tokens_before": frame_tokens_before,
                    "estimated_tokens_after": frame_tokens_after,
                }
            )

        session.latest_context_used_tokens = estimated_tokens_after
        session.force_compact_next_turn = False
        try:
            self._store.save(session)
        except Exception:
            for frame, messages, backup_snapshot in backups:
                frame.messages = messages
                frame.compact_snapshot = backup_snapshot
            raise
        if modified_frame_ids:
            self._cache_engine.invalidate(session_id, modified_frame_ids)
        seq = self._emit(
            session_id,
            "compact_boundary",
            {
                "compacted_frames": compacted_frames,
                "removed_messages": removed_messages,
                "truncated_messages": truncated_messages,
                "keep_recent": keep,
                "pending_preserved": session.pending_turn_id is not None,
                "triggered_by": trigger,
                "estimated_tokens_before": estimated_tokens_before,
                "estimated_tokens_after": estimated_tokens_after,
                "snapshots": snapshot_payloads,
            },
        )
        logger.info(
            "Compacted session session=%s frames=%d removed_messages=%d truncated_messages=%d "
            "pending_preserved=%s triggered_by=%s",
            session_id,
            compacted_frames,
            removed_messages,
            truncated_messages,
            session.pending_turn_id is not None,
            trigger,
        )
        return {
            "session_id": session_id,
            "compacted_frames": compacted_frames,
            "removed_messages": removed_messages,
            "truncated_messages": truncated_messages,
            "estimated_tokens_before": estimated_tokens_before,
            "estimated_tokens_after": estimated_tokens_after,
            "snapshots": snapshot_payloads,
            "last_event_seq": seq,
            "pending_turn_id": session.pending_turn_id,
        }

    async def _build_compact_summary(
        self,
        previous: CompactSnapshot | None,
        old_messages: list[dict[str, Any]],
        *,
        use_llm: bool,
    ) -> str:
        """生成最终压缩摘要，失败时回退机械摘要。"""
        if use_llm and old_messages:
            body = await self._summarize_via_llm(previous, old_messages)
            if body:
                return _wrap_compact_summary(body)
        return _compact_summary_text(previous, old_messages)

    async def _summarize_via_llm(
        self, previous: CompactSnapshot | None, old_messages: list[dict[str, Any]]
    ) -> str | None:
        """调用 LLM 把旧摘要与本次移除消息融合为摘要正文。"""
        source = _mechanical_summary_body(previous, old_messages)
        if not source.strip():
            return None
        instructions = (
            "你是会话历史压缩器。请把下面这段较早的对话上下文压缩成简洁、忠实的中文摘要，"
            "保留关键决策、结论、涉及的文件路径与符号、以及尚未完成的事项；不要编造，不要补充原文没有的信息。"
            "若其中已包含『较早压缩快照』，请把它与新内容融合成单一连贯摘要，不要罗列多份摘要。"
            "只输出摘要正文，不要添加任何前后缀或标记。"
        )
        model = self._settings.compact_summary_model or self._model_for_effort("quick")
        try:
            turn = await self._llm.chat(
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": source},
                ],
                tools=[],
                model=model,
                temperature=0.0,
                thinking_budget=0,
            )
        except LLMError as exc:
            logger.warning("Compact LLM summarize failed, falling back to mechanical: %s", exc)
            return None
        text = (turn.content or "").strip()
        if not text:
            logger.warning(
                "Compact LLM summarize returned empty content, falling back to mechanical"
            )
            return None
        return text
