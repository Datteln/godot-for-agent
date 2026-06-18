"""会话与 agent 帧栈（§6.2 / §14.2 会话持久化）。

`Session` 持有 `agent_stack`（栈顶为当前活跃帧）、待回应的 `pending_*`
字段与 `request_id` 幂等缓存；`SessionStore` 提供按 `session_id` 的
内存态、per-session 锁与本地 JSON 持久化（仅本地、不外传，PRD NFR-12）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition, Frame
from app.permissions.engine import SessionAllowGrant

logger = logging.getLogger(__name__)

_MAX_HISTORY_EVENTS = 500
_COALESCED_HISTORY_EVENT_TYPES = {"agent_text_delta", "agent_reasoning_delta"}


@dataclass
class Session:
    """单个会话的运行态：agent 帧栈 + 待回应工具调用 + 幂等缓存。

    Attributes:
        session_id: 会话 id，来自 `ChatRequest.session_id`。
        agent_stack: agent 帧栈，栈顶为当前活跃帧；根帧（coordinator）常驻，
            `delegate`/`delegate_many` 会压入子 agent 帧，深度受
            `MAX_AGENT_DEPTH` 限制。
        pending_turn_id: 最近一次返回 `tool_calls` 时分配的 `turn_id`；
            为 None 表示当前没有待前端回应的工具调用。
        pending_tool_call_ids: `pending_turn_id` 对应的待回应 `tool_use_id` 集合。
        turn_counter: `turn_id` 生成计数器。
        frame_counter: `frame_id` 生成计数器。
        request_id_cache: `request_id` → 上次响应体，用于用户消息级幂等（§14.1）。
        pending_tool_calls: pending tool_call_id → tool metadata，用于工具结果回填、
            enrich 与会话级 allow 授权。
        session_allow: 本会话内"总是允许"的授权集合，不跨会话持久化到项目配置。
        effort: 当前会话 effort 档位。
        output_style: 当前会话 OutputStyle id。
        delegate_groups: `delegate_many` 的挂起组状态；仅保存 JSON 原生值。
        pending_plan: 当前正在执行的 `create_plan` 计划状态（概述、步骤、进度指针），
            不存在活跃计划时为 None。
        verify_retry_count: 文件路径 → 该文件已触发的"校验失败-修复"重试次数，
            用于防止 Verify 与 LLM 修复之间死循环。
    """

    session_id: str
    agent_stack: list[Frame] = field(default_factory=list)
    pending_turn_id: str | None = None
    pending_tool_call_ids: set[str] = field(default_factory=set)
    turn_counter: int = 0
    frame_counter: int = 0
    request_id_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_allow: set[SessionAllowGrant] = field(default_factory=set)
    effort: str = "standard"
    output_style: str = "default"
    delegate_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_plan: dict[str, Any] | None = None
    verify_retry_count: dict[str, int] = field(default_factory=dict)
    history_event_counter: int = 0
    history_events: list[dict[str, Any]] = field(default_factory=list)

    def top_frame(self) -> Frame | None:
        """返回当前活跃帧（栈顶），栈为空时返回 None。

        Returns:
            栈顶 `Frame`，若 `agent_stack` 为空则返回 None。
        """
        return self.agent_stack[-1] if self.agent_stack else None

    def new_frame_id(self) -> str:
        """生成下一个帧 id（形如 `f1`、`f2`）。

        Returns:
            会话内唯一递增的帧 id 字符串。
        """
        self.frame_counter += 1
        return f"f{self.frame_counter}"

    def new_turn_id(self) -> str:
        """生成下一个 `turn_id`（形如 `t1`、`t2`）。

        Returns:
            会话内唯一递增的 turn id 字符串。
        """
        self.turn_counter += 1
        return f"t{self.turn_counter}"

    def ensure_root_frame(self, agent: AgentDefinition) -> Frame:
        """确保会话至少有一个根帧（coordinator），不存在时创建并压栈。

        根帧的初始 `messages` 只包含一条以 `agent.prompt` 为内容的
        `system` 消息，作为该 agent 的 system prompt（M1 起由
        `PromptBuilder` 接管分层组装）。

        Args:
            agent: 根帧应绑定的 agent 定义（已解析 `effective_tools`）。

        Returns:
            已存在或新创建的根帧。
        """
        if self.agent_stack:
            return self.agent_stack[0]
        frame = Frame(
            id=self.new_frame_id(),
            agent=agent,
            messages=[{"role": "system", "content": agent.prompt}],
        )
        self.agent_stack.append(frame)
        return frame

    def set_pending(
        self,
        turn_id: str,
        tool_call_ids: list[str],
        metadata: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """记录一批待前端回应的工具调用（§14.1 幂等与并发控制）。

        Args:
            turn_id: 本轮分配的 `turn_id`。
            tool_call_ids: 本轮所有 front 工具调用的 `tool_use_id` 列表。
        """
        self.pending_turn_id = turn_id
        self.pending_tool_call_ids = set(tool_call_ids)
        self.pending_tool_calls = metadata or {}

    def clear_pending(self) -> None:
        """清空待回应记录（前端结果已全部校验通过并 append 后调用）。"""
        self.pending_turn_id = None
        self.pending_tool_call_ids = set()
        self.pending_tool_calls = {}

    def record_history_event(self, event_type: str, payload: dict[str, Any]) -> int:
        """Record a bounded, coalesced event timeline used for history replay."""
        self.history_event_counter += 1
        record = {
            "seq": self.history_event_counter,
            "type": event_type,
            "payload": dict(payload),
        }
        if event_type in _COALESCED_HISTORY_EVENT_TYPES and self.history_events:
            previous = self.history_events[-1]
            previous_payload = previous.get("payload", {})
            if not isinstance(previous_payload, dict):
                previous_payload = {}
            previous_key = (
                str(previous.get("type", "")),
                str(previous_payload.get("frame_id", "")),
                str(previous_payload.get("loop", "")),
            )
            current_key = (
                event_type,
                str(payload.get("frame_id", "")),
                str(payload.get("loop", "")),
            )
            if previous_key == current_key:
                self.history_events[-1] = record
                return self.history_event_counter
        self.history_events.append(record)
        if len(self.history_events) > _MAX_HISTORY_EVENTS:
            del self.history_events[: len(self.history_events) - _MAX_HISTORY_EVENTS]
        return self.history_event_counter


def _frame_to_dict(frame: Frame) -> dict[str, Any]:
    """把 `Frame` 序列化为可写入 JSON 的字典。

    Args:
        frame: 待序列化的帧。

    Returns:
        仅含 JSON 原生类型的字典；`agent` 只保留 `agent_name`，恢复时
        重新从内置 agent 注册表解析，避免持久化大段 prompt 文本。
    """
    return {
        "id": frame.id,
        "agent_name": frame.agent.name,
        "messages": frame.messages,
        "parent_id": frame.parent_id,
        "pending_delegate_call_id": frame.pending_delegate_call_id,
        "pending_delegate_group_id": frame.pending_delegate_group_id,
        "status": frame.status,
        "depth": frame.depth,
        "active_deferred_tools": sorted(frame.active_deferred_tools),
        "history_anchor_frame_id": frame.history_anchor_frame_id,
        "history_anchor_message_index": frame.history_anchor_message_index,
    }


def _frame_from_dict(data: dict[str, Any], available_tools: set[str]) -> Frame:
    """从持久化字典恢复 `Frame`。

    Args:
        data: `_frame_to_dict` 产出的字典。
        available_tools: 当前入口/权限模式下可见的工具名集合，用于重新
            解析 `agent.effective_tools`。

    Returns:
        恢复后的 `Frame`。
    """
    agent = get_agent(data["agent_name"], available_tools)
    status = data.get("status", "running")
    return Frame(
        id=data["id"],
        agent=agent,
        messages=data["messages"],
        parent_id=data.get("parent_id"),
        pending_delegate_call_id=data.get("pending_delegate_call_id"),
        pending_delegate_group_id=data.get("pending_delegate_group_id"),
        status=status,
        depth=data.get("depth", 0),
        active_deferred_tools=set(data.get("active_deferred_tools", [])),
        history_anchor_frame_id=data.get("history_anchor_frame_id"),
        history_anchor_message_index=data.get("history_anchor_message_index"),
    )


def session_to_dict(session: Session) -> dict[str, Any]:
    """把 `Session` 序列化为可写入 JSON 的字典。

    Args:
        session: 待序列化的会话。

    Returns:
        仅含 JSON 原生类型的字典。
    """
    return {
        "session_id": session.session_id,
        "agent_stack": [_frame_to_dict(f) for f in session.agent_stack],
        "pending_turn_id": session.pending_turn_id,
        "pending_tool_call_ids": sorted(session.pending_tool_call_ids),
        "turn_counter": session.turn_counter,
        "frame_counter": session.frame_counter,
        "request_id_cache": session.request_id_cache,
        "pending_tool_calls": session.pending_tool_calls,
        "session_allow": [list(grant) for grant in sorted(session.session_allow)],
        "effort": session.effort,
        "output_style": session.output_style,
        "delegate_groups": session.delegate_groups,
        "pending_plan": session.pending_plan,
        "verify_retry_count": session.verify_retry_count,
        "history_event_counter": session.history_event_counter,
        "history_events": session.history_events,
    }


def session_from_dict(data: dict[str, Any], available_tools: set[str]) -> Session:
    """从持久化字典恢复 `Session`。

    Args:
        data: `session_to_dict` 产出的字典。
        available_tools: 当前入口/权限模式下可见的工具名集合。

    Returns:
        恢复后的 `Session`。
    """
    raw_history_events = data.get("history_events", [])
    history_events = (
        [event for event in raw_history_events if isinstance(event, dict)]
        if isinstance(raw_history_events, list)
        else []
    )
    restored_event_counter = 0
    for event in history_events:
        try:
            restored_event_counter = max(restored_event_counter, int(event.get("seq", 0)))
        except (TypeError, ValueError):
            continue
    try:
        stored_event_counter = int(data.get("history_event_counter", 0))
    except (TypeError, ValueError):
        stored_event_counter = 0
    history_event_counter = max(stored_event_counter, restored_event_counter)
    return Session(
        session_id=data["session_id"],
        agent_stack=[_frame_from_dict(f, available_tools) for f in data.get("agent_stack", [])],
        pending_turn_id=data.get("pending_turn_id"),
        pending_tool_call_ids=set(data.get("pending_tool_call_ids", [])),
        turn_counter=data.get("turn_counter", 0),
        frame_counter=data.get("frame_counter", 0),
        request_id_cache=data.get("request_id_cache", {}),
        pending_tool_calls=data.get("pending_tool_calls", {}),
        session_allow={
            (str(item[0]), str(item[1]), str(item[2]))
            for item in data.get("session_allow", [])
            if isinstance(item, list) and len(item) == 3
        },
        effort=str(data.get("effort", "standard")),
        output_style=str(data.get("output_style", "default")),
        delegate_groups=data.get("delegate_groups", {}),
        pending_plan=data.get("pending_plan"),
        verify_retry_count=data.get("verify_retry_count", {}),
        history_event_counter=history_event_counter,
        history_events=history_events,
    )


def _safe_filename(session_id: str) -> str:
    """把会话 id 转换为安全的文件名，避免路径穿越。

    Args:
        session_id: 客户端提供的会话 id。

    Returns:
        仅包含字母数字、`-`、`_` 的文件名（不含扩展名）；若 `session_id`
        不含任何合法字符，回退为 `"_"`。
    """
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_")
    return safe or "_"


class SessionStore:
    """会话存储：内存态 + per-session 锁 + 本地 JSON 持久化。

    持久化目录仅保存 `session_id`、`agent_stack`（含消息历史）、
    `pending_*` 与 `request_id_cache`；不包含鉴权 token 或 API key。
    """

    def __init__(self, storage_dir: Path) -> None:
        """初始化会话存储。

        Args:
            storage_dir: 会话 JSON 文件的存放目录，按需创建。
        """
        self._storage_dir = storage_dir
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        """返回（必要时创建）某会话的 per-session 锁。

        Args:
            session_id: 会话 id。

        Returns:
            与该会话绑定的 `asyncio.Lock`，用于串行化同一会话的请求。
        """
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
            logger.debug("Session lock created session=%s", session_id)
        return lock

    def get_or_create(self, session_id: str, available_tools: set[str]) -> Session:
        """获取内存中的会话，不存在则尝试从磁盘恢复或新建。

        Args:
            session_id: 会话 id。
            available_tools: 当前入口/权限模式下可见的工具名集合，用于
                恢复 `agent_stack` 时重新解析 `effective_tools`。

        Returns:
            内存中的会话实例（已加入内存表）。
        """
        existing = self._sessions.get(session_id)
        if existing is not None:
            logger.debug("Session cache hit session=%s frames=%d", session_id, len(existing.agent_stack))
            return existing
        restored = self._load(session_id, available_tools)
        session = restored if restored is not None else Session(session_id=session_id)
        self._sessions[session_id] = session
        if restored is None:
            logger.info("Session created session=%s", session_id)
        else:
            logger.info(
                "Session restored session=%s frames=%d pending=%s",
                session_id,
                len(session.agent_stack),
                session.pending_turn_id is not None,
            )
        return session

    def save(self, session: Session) -> None:
        """把会话写入内存表并持久化到本地 JSON 文件。

        Args:
            session: 待保存的会话。
        """
        self._sessions[session.session_id] = session
        path = self._path_for(session.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(session_to_dict(session), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug(
            "Session saved session=%s frames=%d pending=%s cache_entries=%d path=%s",
            session.session_id,
            len(session.agent_stack),
            session.pending_turn_id is not None,
            len(session.request_id_cache),
            path,
        )

    def reset(self, session_id: str) -> None:
        """清空指定会话（内存与本地持久化文件）。

        Args:
            session_id: 待清空的会话 id。
        """
        self._sessions.pop(session_id, None)
        path = self._path_for(session_id)
        if path.exists():
            path.unlink()
            logger.info("Session reset removed persisted file session=%s path=%s", session_id, path)
        else:
            logger.info("Session reset session=%s no persisted file", session_id)

    def _path_for(self, session_id: str) -> Path:
        """返回某会话对应的本地 JSON 文件路径。

        Args:
            session_id: 会话 id。

        Returns:
            `storage_dir` 下以安全文件名命名的 `.json` 路径。
        """
        return self._storage_dir / f"{_safe_filename(session_id)}.json"

    def _load(self, session_id: str, available_tools: set[str]) -> Session | None:
        """尝试从磁盘恢复会话；文件不存在或内容不合法则返回 None。

        Args:
            session_id: 会话 id。
            available_tools: 当前入口/权限模式下可见的工具名集合。

        Returns:
            恢复成功的会话，或 None（视为新会话）。
        """
        path = self._path_for(session_id)
        if not path.exists():
            logger.debug("Session load skipped missing file session=%s path=%s", session_id, path)
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            session = session_from_dict(data, available_tools)
            logger.debug("Session loaded from disk session=%s path=%s", session_id, path)
            return session
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("Session load failed session=%s path=%s error=%s", session_id, path, exc)
            return None
