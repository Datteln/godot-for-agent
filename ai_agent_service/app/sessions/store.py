"""会话与 agent 帧栈（§6.2 / §14.2 会话持久化）。

`Session` 持有 `agent_stack`（栈顶为当前活跃帧）、待回应的 `pending_*`
字段与 `request_id` 幂等缓存；`SessionStore` 提供按 `session_id` 的
内存态、per-session 锁与本地 JSON 持久化（仅本地、不外传，PRD NFR-12）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition, CompactSnapshot, Frame
from app.permissions.engine import SessionAllowGrant
from app.storage.atomic import atomic_write_json

logger = logging.getLogger(__name__)

_MAX_HISTORY_EVENTS = 500
_COALESCED_HISTORY_EVENT_TYPES = {"agent_text_delta", "agent_reasoning_delta", "context_usage"}


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
        map_completion_blockers: 地图编辑任务的阻断完成原因；前端地图工具回传
            `blocking_completion` 或尚未通过路线校验时写入，最终回复前清空或拦截。
        rag_context: 当前用户提问检索到的 RAG 上下文（分层 prompt 的 L3 段），
            在新用户消息到达时刷新、在工具结果回填等同一轮的后续请求里复用，
            使该段在整轮 agent 循环内保持稳定、可被缓存（§16.1 RAG 段缓存）。
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
    pending_verify_candidates: list[dict[str, Any]] = field(default_factory=list)
    map_completion_blockers: list[dict[str, Any]] = field(default_factory=list)
    history_event_counter: int = 0
    history_events: list[dict[str, Any]] = field(default_factory=list)
    rag_context: str = ""

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
        if (
            event_type in _COALESCED_HISTORY_EVENT_TYPES
            and not bool(payload.get("append_delta", False))
            and self.history_events
        ):
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
        "compact_snapshot": (
            {
                "revision": frame.compact_snapshot.revision,
                "digest": frame.compact_snapshot.digest,
                "summary": frame.compact_snapshot.summary,
                "created_at": frame.compact_snapshot.created_at,
                "source_message_count": frame.compact_snapshot.source_message_count,
                "removed_message_count": frame.compact_snapshot.removed_message_count,
                "keep_recent": frame.compact_snapshot.keep_recent,
                "estimated_tokens_before": frame.compact_snapshot.estimated_tokens_before,
                "estimated_tokens_after": frame.compact_snapshot.estimated_tokens_after,
                "triggered_by": frame.compact_snapshot.triggered_by,
            }
            if frame.compact_snapshot is not None
            else None
        ),
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
    raw_snapshot = data.get("compact_snapshot")
    compact_snapshot: CompactSnapshot | None = None
    if isinstance(raw_snapshot, dict):
        triggered_by: Literal["manual", "auto"] = (
            "auto" if raw_snapshot.get("triggered_by") == "auto" else "manual"
        )
        compact_snapshot = CompactSnapshot(
            revision=_as_int(raw_snapshot.get("revision"), 1),
            digest=str(raw_snapshot.get("digest", "")),
            summary=str(raw_snapshot.get("summary", "")),
            created_at=str(raw_snapshot.get("created_at", "")),
            source_message_count=_as_int(raw_snapshot.get("source_message_count")),
            removed_message_count=_as_int(raw_snapshot.get("removed_message_count")),
            keep_recent=_as_int(raw_snapshot.get("keep_recent"), 12),
            estimated_tokens_before=_as_int(raw_snapshot.get("estimated_tokens_before")),
            estimated_tokens_after=_as_int(raw_snapshot.get("estimated_tokens_after")),
            triggered_by=triggered_by,
        )
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
        compact_snapshot=compact_snapshot,
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
        "pending_verify_candidates": session.pending_verify_candidates,
        "map_completion_blockers": session.map_completion_blockers,
        "history_event_counter": session.history_event_counter,
        "history_events": session.history_events,
        "rag_context": session.rag_context,
    }


def _as_dict(value: Any) -> dict[str, Any]:
    """把外部 JSON 值规整为 dict；非 dict（含 None）一律视作空 dict。"""
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    """把外部 JSON 值规整为 list；非 list（含 None）一律视作空 list。"""
    return value if isinstance(value, list) else []


def _as_int(value: Any, default: int = 0) -> int:
    """把外部 JSON 值规整为 int；非整数/None 回退为 `default`。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return default


def session_from_dict(data: dict[str, Any], available_tools: set[str]) -> Session:
    """从持久化字典恢复 `Session`。

    持久化文件可能是合法 JSON 但字段类型错误（例如 `{"items": null}`、
    `pending_verify_candidates: null`）。这里对每个集合/字典字段先判型再使用，
    避免对 None 做迭代/解包而抛出未捕获的 `TypeError`、进而让接口持续 500
    （§14.2）。

    Args:
        data: `session_to_dict` 产出的字典。
        available_tools: 当前入口/权限模式下可见的工具名集合。

    Returns:
        恢复后的 `Session`。

    Raises:
        ValueError: 顶层不是对象，或缺少必需的 `session_id` 字段。
    """
    if not isinstance(data, dict):
        raise ValueError("session payload must be an object")
    if not isinstance(data.get("session_id"), str) or not data["session_id"]:
        raise ValueError("session payload missing string session_id")
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
    pending_plan = data.get("pending_plan")
    return Session(
        session_id=str(data["session_id"]),
        agent_stack=[
            _frame_from_dict(f, available_tools)
            for f in _as_list(data.get("agent_stack"))
            if isinstance(f, dict)
        ],
        pending_turn_id=data.get("pending_turn_id"),
        pending_tool_call_ids={str(item) for item in _as_list(data.get("pending_tool_call_ids"))},
        turn_counter=_as_int(data.get("turn_counter")),
        frame_counter=_as_int(data.get("frame_counter")),
        request_id_cache=_as_dict(data.get("request_id_cache")),
        pending_tool_calls=_as_dict(data.get("pending_tool_calls")),
        session_allow={
            (str(item[0]), str(item[1]), str(item[2]), str(item[3]) if len(item) >= 4 else "")
            for item in _as_list(data.get("session_allow"))
            if isinstance(item, list) and len(item) in {3, 4}
        },
        effort=str(data.get("effort", "standard")),
        output_style=str(data.get("output_style", "default")),
        delegate_groups=_as_dict(data.get("delegate_groups")),
        pending_plan=pending_plan if isinstance(pending_plan, dict) else None,
        verify_retry_count=_as_dict(data.get("verify_retry_count")),
        pending_verify_candidates=[
            item
            for item in _as_list(data.get("pending_verify_candidates"))
            if isinstance(item, dict)
        ],
        map_completion_blockers=[
            item
            for item in _as_list(data.get("map_completion_blockers"))
            if isinstance(item, dict)
        ],
        history_event_counter=history_event_counter,
        history_events=history_events,
        rag_context=str(data.get("rag_context", "")),
    )


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _safe_filename(session_id: str) -> str:
    """把会话 id 转换为无碰撞的安全文件名，避免路径穿越与串读。

    旧实现把 `session_id` 里的非法字符直接剔除，于是 `a/bc` 与 `ab/c`
    会被清洗成同一个 `abc`，导致两个不同会话共用一个文件、互相覆盖/串读
    （§14.2）。这里改为：先用白名单正则拒绝非法 id，再用 `session_id` 的
    SHA-256 摘要作为文件名——摘要是单射且与原文一一对应，永不碰撞。

    Args:
        session_id: 客户端提供的会话 id。

    Returns:
        `session_id` 的 SHA-256 十六进制摘要（不含扩展名）。

    Raises:
        ValueError: `session_id` 不满足白名单格式。
    """
    if not _SESSION_ID_RE.fullmatch(session_id):
        raise ValueError(f"invalid session_id: {session_id!r}")
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


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
            logger.debug(
                "Session cache hit session=%s frames=%d", session_id, len(existing.agent_stack)
            )
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
        atomic_write_json(path, session_to_dict(session))
        logger.debug(
            "Session saved session=%s frames=%d pending=%s cache_entries=%d path=%s",
            session.session_id,
            len(session.agent_stack),
            session.pending_turn_id is not None,
            len(session.request_id_cache),
            path,
        )

    def replace_in_memory(self, session_id: str, session: Session) -> None:
        """仅替换内存态会话，不触碰磁盘。

        用于请求被取消时回滚到 turn 开始前的内存快照：此时本轮可能已向
        `frame.messages` 追加了 assistant 的 tool_calls 却来不及写入对应的
        tool result，若让这半截历史留在内存里，下一次请求发给 LLM 会因
        "tool_call 无对应 tool result" 而协议报错（§agent.py 中断回滚）。
        因为本轮尚未 `save()`，磁盘仍是旧版本，所以只需还原内存。

        Args:
            session_id: 会话 id。
            session: 回滚目标快照。
        """
        self._sessions[session_id] = session

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
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("Session load failed session=%s path=%s error=%s", session_id, path, exc)
            return None
        # 文件名是 session_id 的哈希；正常情况下不会串读，但仍校验磁盘中记录的
        # session_id 与请求值一致，防止历史遗留文件或人为改名导致的串读。
        if session.session_id != session_id:
            logger.warning(
                "Session id mismatch on load requested=%s stored=%s path=%s; treating as new",
                session_id,
                session.session_id,
                path,
            )
            return None
        logger.debug("Session loaded from disk session=%s path=%s", session_id, path)
        return session
