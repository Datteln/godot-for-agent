"""Canonical Session history query use case."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.api.schemas import SessionHistoryResponse
from app.config import AppSettings
from app.events.store import EventStore
from app.query.helpers import (
    _history_context_used_tokens,
    _persisted_history_events,
    _structured_session_history,
)
from app.query.history_to_events import blocks_to_timeline_events
from app.sessions.store import SessionStore

logger = logging.getLogger(__name__)


class HistoryQueryService:
    """Owns history projection and pagination without submission mutation."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        events: EventStore | None,
        cache: dict[tuple[str, str], tuple[tuple[int, int, int], list[Any]]],
        available_tools: Callable[[], set[str]],
    ) -> None:
        self._settings = settings
        self._store = store
        self._events = events
        self._history_blocks_cache = cache
        self._available_tools = available_tools

    @property
    def available_tools(self) -> set[str]:
        return self._available_tools()

    def session_history(
        self, session_id: str, limit: int = 200, before: int = 0
    ) -> SessionHistoryResponse:
        """Return frontend-renderable history for a persisted session."""
        session = self._store.get_or_create(session_id, self.available_tools)
        if self._events is not None:
            self._events.ensure_sequence(
                session_id,
                session.history_event_counter,
                session_epoch=session.session_epoch,
            )
        events = _persisted_history_events(session)
        if not events and self._events is not None:
            events = self._events.list_after(session_id, 0)
        # 下面的逐 frame/event 转换是 O(frames + events) 的纯 Python 工作；长期
        # 使用的会话（大量 delegate_many 子 agent frame + 持续累积的事件日志）
        # 不加界会让这一步随历史总量无限增长，最终触发前端 30s 看门狗超时、把
        # 本来该串行复用的请求队列卡死。既然最终只展示最近 `limit` 条，这里先
        # 把输入收窄到最近窗口再转换，而不是转换全量历史后再丢弃大半。
        omitted_inputs = False
        if limit > 0 and before <= 0:
            target_blocks = limit + max(before, 0)
            input_window = max(target_blocks, 1)
            while True:
                recent_frames = session.agent_stack[-input_window:]
                recent_events = events[-(input_window * 8) :]
                omitted_inputs = len(recent_frames) < len(session.agent_stack) or len(
                    recent_events
                ) < len(events)
                blocks = _structured_session_history(recent_frames, recent_events)
                if not omitted_inputs or len(blocks) >= target_blocks:
                    break
                input_window *= 2
        else:
            # 局部窗口会把较早的 frame 误判成历史末尾，提前返回
            # history_has_more=false。仅在真正向上翻页时构建完整时间线，
            # 并缓存结果，避免每页重复做 O(frames + events) 的转换。
            recent_frames = session.agent_stack
            recent_events = events
            cache_key = (
                len(recent_frames),
                len(recent_events),
                recent_events[-1].seq if recent_events else 0,
            )
            history_cache_key = (session.session_id, session.session_epoch)
            cached = self._history_blocks_cache.get(history_cache_key)
            if cached is not None and cached[0] == cache_key:
                blocks = cached[1]
            else:
                blocks = _structured_session_history(recent_frames, recent_events)
                self._history_blocks_cache[history_cache_key] = (cache_key, blocks)
        offset = min(max(before, 0), len(blocks))
        end = len(blocks) - offset
        start = max(0, end - limit) if limit > 0 else 0
        page = blocks[start:end]
        page_events = blocks_to_timeline_events(
            page,
            session_epoch=session.session_epoch,
            start_index=start,
        )
        logger.info(
            "Session history requested session=%s frames=%d/%d blocks=%d events=%d pending=%s",
            session_id,
            len(recent_frames),
            len(session.agent_stack),
            len(page),
            len(page_events),
            session.pending_turn_id is not None,
        )
        return SessionHistoryResponse(
            session_id=session.session_id,
            session_epoch=session.session_epoch,
            last_event_seq=self._events.last_seq(session_id) if self._events is not None else 0,
            pending_turn_id=session.pending_turn_id,
            context_used_tokens=_history_context_used_tokens(session, events),
            context_token_limit=self._settings.auto_compact_token_threshold,
            map_worker_structured_output_enabled=(
                self._settings.map_worker_structured_output_enabled
            ),
            map_worker_response_contract_mode=(self._settings.map_worker_response_contract_mode),
            map_worker_structured_correction_limit=(
                self._settings.map_worker_structured_correction_limit
            ),
            map_worker_structured_thinking_budget=(
                self._settings.map_worker_structured_thinking_budget
            ),
            history_before=offset + len(page),
            history_has_more=start > 0 or omitted_inputs,
            events=page_events,
        )
