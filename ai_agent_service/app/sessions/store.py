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
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition, CompactSnapshot, Frame
from app.orchestrator.map_progress import MapTaskState
from app.orchestrator.map_request_scope import MapRequestScope
from app.orchestrator.map_workers import restore_project_agent
from app.permissions.engine import SessionAllowGrant
from app.sessions.schema import (
    SESSION_SCHEMA_VERSION,
    migrate_session_payload,
    session_payload_version,
)
from app.storage.atomic import atomic_write_json
from app.sessions.resource_registry import RESET_FAILPOINTS, ResetFailureInjector

logger = logging.getLogger(__name__)

_COALESCED_HISTORY_EVENT_TYPES = {"agent_text_delta", "agent_reasoning_delta", "context_usage"}
_STREAM_HISTORY_EVENT_TYPES = {"agent_text_delta", "agent_reasoning_delta"}


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
        request_id_cache: `request_id` → 上次响应体，用于请求级幂等（§14.1）。
        completed_tool_turn_cache: 已处理工具结果的 turn id → 结果指纹与响应体；
            客户端更换 request_id 重试同一批结果时仍返回第一次响应。
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
        map_task_state: 地图任务的唯一运行时状态，包括 revision、校验合同、缓存和完成门。
        map_request_scope: 当前用户请求的结构化 intent、lineage 与 completion candidate；
            普通对话默认不绑定地图任务。
        map_task_lineage: 当前地图任务独立于普通 request scope 持久化的 origin、
            lineage 与 completion-candidate 状态，供显式恢复继承。
        pending_map_write_after_read: 因缺少 map state 而挂起的一次地图写调用；
            自动读到 `map_layer`/`map_revision` 后恢复下发。
        pending_map_validation_after_read: 因缺少 `map_layer` 而挂起的一次地图校验调用；
            自动读到图层后恢复下发。
        pending_map_tool_after_read: 因缺少真实地图区域上下文而挂起的一次地图工具调用；
            自动读完同一区域后恢复下发，避免 LLM 读完后凭记忆重试。
        latest_context_used_tokens: provider 最近一次返回的真实上下文 token 用量；
            自动压缩用它和本地估算取大，避免低估。
        force_compact_next_turn: 最近一次 provider 用量超过阈值后置位；下一轮 LLM 前
            强制 compact 一次。
        rag_context: 当前用户提问检索到的 RAG 上下文（分层 prompt 的 L3 段），
            在新用户消息到达时刷新、在工具结果回填等同一轮的后续请求里复用，
            使该段在整轮 agent 循环内保持稳定、可被缓存（§16.1 RAG 段缓存）。
    """

    session_id: str
    session_epoch: str = ""
    agent_stack: list[Frame] = field(default_factory=list)
    pending_turn_id: str | None = None
    pending_tool_call_ids: set[str] = field(default_factory=set)
    turn_counter: int = 0
    frame_counter: int = 0
    request_id_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 已处理工具结果的 turn id → 结果指纹与响应体；
    # 客户端更换 request_id 重试同一批工具结果时，直接返回首次响应而不重复执行
    completed_tool_turn_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_tool_calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    session_allow: set[SessionAllowGrant] = field(default_factory=set)
    effort: str = "standard"
    output_style: str = "default"
    delegate_groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_plan: dict[str, Any] | None = None
    verify_retry_count: dict[str, int] = field(default_factory=dict)
    pending_verify_candidates: list[dict[str, Any]] = field(default_factory=list)
    pending_map_write_after_read: dict[str, Any] | None = None
    pending_map_validation_after_read: dict[str, Any] | None = None
    pending_map_tool_after_read: dict[str, Any] | None = None
    latest_context_used_tokens: int = 0
    force_compact_next_turn: bool = False
    map_task_state: MapTaskState = field(default_factory=MapTaskState)
    map_request_scope: MapRequestScope = field(default_factory=MapRequestScope)
    map_task_lineage: dict[str, Any] = field(default_factory=dict)
    history_event_counter: int = 0
    history_events: list[dict[str, Any]] = field(default_factory=list)
    rag_context: str = ""
    task_run: dict[str, Any] | None = None
    _turn_counter_reserver: Callable[[str, int], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

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
        if self._turn_counter_reserver is not None:
            self._turn_counter_reserver(self.session_id, self.turn_counter)
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
        enriched_metadata: dict[str, dict[str, Any]] = {}
        frames = {frame.id: frame for frame in self.agent_stack}
        for tool_call_id in tool_call_ids:
            item = dict((metadata or {}).get(tool_call_id, {}))
            frame = frames.get(str(item.get("frame_id", "")))
            lineage_id = (
                frame.map_request_lineage_id
                if frame is not None
                else self.map_request_scope.lineage_id
            )
            map_task_id = (
                frame.map_task_id if frame is not None else self.map_request_scope.map_task_id
            )
            item["request_lineage_id"] = lineage_id or ""
            item["map_task_id"] = map_task_id or ""
            enriched_metadata[tool_call_id] = item
        self.pending_turn_id = turn_id
        self.pending_tool_call_ids = set(tool_call_ids)
        self.pending_tool_calls = enriched_metadata

    def clear_pending(self) -> None:
        """清空待回应记录（前端结果已全部校验通过并 append 后调用）。"""
        self.pending_turn_id = None
        self.pending_tool_call_ids = set()
        self.pending_tool_calls = {}

    def record_history_event(self, event_type: str, payload: dict[str, Any]) -> int:
        """记录用于历史回放的事件，并合并同一流片段的增量文本。"""
        self.history_event_counter += 1
        record = {
            "seq": self.history_event_counter,
            "type": event_type,
            "payload": dict(payload),
        }
        if event_type in _STREAM_HISTORY_EVENT_TYPES and bool(payload.get("append_delta", False)):
            current_key = (
                event_type,
                str(payload.get("frame_id", "")),
                str(payload.get("loop", "")),
                str(payload.get("timeline_frame_id", "")),
                str(payload.get("timeline_message_index", payload.get("message_index", ""))),
                str(payload.get("stream_segment", "")),
            )
            for index in range(len(self.history_events) - 1, -1, -1):
                previous = self.history_events[index]
                if str(previous.get("type", "")) not in _STREAM_HISTORY_EVENT_TYPES:
                    break
                previous_payload = previous.get("payload", {})
                if not isinstance(previous_payload, dict):
                    continue
                previous_key = (
                    str(previous.get("type", "")),
                    str(previous_payload.get("frame_id", "")),
                    str(previous_payload.get("loop", "")),
                    str(previous_payload.get("timeline_frame_id", "")),
                    str(
                        previous_payload.get(
                            "timeline_message_index",
                            previous_payload.get("message_index", ""),
                        )
                    ),
                    str(previous_payload.get("stream_segment", "")),
                )
                if previous_key != current_key:
                    continue
                merged_payload = dict(payload)
                merged_payload["text"] = str(previous_payload.get("text", "")) + str(
                    payload.get("text", "")
                )
                merged_payload["append_delta"] = False
                self.history_events[index] = {
                    "seq": previous.get("seq", self.history_event_counter),
                    "type": event_type,
                    "payload": merged_payload,
                }
                return self.history_event_counter
        if (
            event_type in _COALESCED_HISTORY_EVENT_TYPES
            and not bool(payload.get("append_delta", False))
            and self.history_events
        ):
            previous = self.history_events[-1]
            previous_payload = previous.get("payload", {})
            if not isinstance(previous_payload, dict):
                previous_payload = {}
            previous_snapshot_key = (
                str(previous.get("type", "")),
                str(previous_payload.get("frame_id", "")),
                str(previous_payload.get("loop", "")),
            )
            current_snapshot_key = (
                event_type,
                str(payload.get("frame_id", "")),
                str(payload.get("loop", "")),
            )
            if previous_snapshot_key == current_snapshot_key:
                self.history_events[-1] = record
                return self.history_event_counter
        self.history_events.append(record)
        return self.history_event_counter


def _frame_to_dict(frame: Frame) -> dict[str, Any]:
    """把 `Frame` 序列化为可写入 JSON 的字典。

    Args:
        frame: 待序列化的帧。

    Returns:
        仅含 JSON 原生类型的字典；内置 agent 只保留 `agent_name`，恢复时
        重新从注册表解析。一次性 project agent 额外保存必要定义。
    """
    return {
        "id": frame.id,
        "agent_name": frame.agent.name,
        "project_agent": (
            {
                "name": frame.agent.name,
                "description": frame.agent.description,
                "prompt": frame.agent.prompt,
                "tools": frame.agent.tools or [],
                "skills": frame.agent.skills,
                "workflow_operations": frame.agent.workflow_operations,
                "workflow_constraints": frame.agent.workflow_constraints,
                # 持久化稳定的编排元数据：权限/预算只从这些字段推断
                "pipeline_kind": frame.agent.pipeline_kind,
                "role": frame.agent.role,
                "map_stage": frame.agent.map_stage,
                "worker_mode": frame.agent.worker_mode,
                "model": frame.agent.model,
                "effort": frame.agent.effort,
                "max_turns": frame.agent.max_turns,
                "edit_map_max_turns": frame.agent.edit_map_max_turns,
            }
            if frame.agent.source == "project"
            else None
        ),
        "messages": frame.messages,
        "parent_id": frame.parent_id,
        "pending_delegate_call_id": frame.pending_delegate_call_id,
        "pending_delegate_group_id": frame.pending_delegate_group_id,
        "status": frame.status,
        "depth": frame.depth,
        "active_deferred_tools": sorted(frame.active_deferred_tools),
        "search_tools_noop_count": frame.search_tools_noop_count,
        "history_anchor_frame_id": frame.history_anchor_frame_id,
        "history_anchor_message_index": frame.history_anchor_message_index,
        "persistent_turn_count": frame.persistent_turn_count,
        "persistent_edit_map_turn_count": frame.persistent_edit_map_turn_count,
        "map_progress_revision": frame.map_progress_revision,
        "forced_completion_text": frame.forced_completion_text,
        "force_text_only": frame.force_text_only,
        "map_reader_detailed_region_ready": frame.map_reader_detailed_region_ready,
        # 地图子 Frame 的可信阶段合同与运行时证据，用于跨轮次恢复时保持流水线状态一致
        "map_stage_contract": frame.map_stage_contract,
        "map_request_lineage_id": frame.map_request_lineage_id,
        "map_task_id": frame.map_task_id,
        "contract_id": frame.contract_id,
        "worker_instance_id": frame.worker_instance_id,
        "result_schema": frame.result_schema,
        "allowed_next_stages": list(frame.allowed_next_stages),
        "map_evidence": frame.map_evidence,
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
    project_agent = data.get("project_agent")
    agent = (
        restore_project_agent(project_agent, available_tools)
        if isinstance(project_agent, dict)
        else get_agent(data["agent_name"], available_tools)
    )
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
        search_tools_noop_count=_as_int(data.get("search_tools_noop_count")),
        history_anchor_frame_id=data.get("history_anchor_frame_id"),
        history_anchor_message_index=data.get("history_anchor_message_index"),
        persistent_turn_count=_as_int(data.get("persistent_turn_count")),
        persistent_edit_map_turn_count=_as_int(data.get("persistent_edit_map_turn_count")),
        map_progress_revision=(
            data.get("map_progress_revision")
            if isinstance(data.get("map_progress_revision"), int)
            and not isinstance(data.get("map_progress_revision"), bool)
            else None
        ),
        forced_completion_text=(
            data.get("forced_completion_text")
            if isinstance(data.get("forced_completion_text"), str)
            else None
        ),
        force_text_only=data.get("force_text_only") is True,
        map_reader_detailed_region_ready=data.get("map_reader_detailed_region_ready") is True,
        map_stage_contract=_as_dict(data.get("map_stage_contract")),
        map_request_lineage_id=(
            str(data["map_request_lineage_id"])
            if data.get("map_request_lineage_id") is not None
            else None
        ),
        map_task_id=(str(data["map_task_id"]) if data.get("map_task_id") is not None else None),
        contract_id=(str(data["contract_id"]) if data.get("contract_id") is not None else None),
        worker_instance_id=(
            str(data["worker_instance_id"]) if data.get("worker_instance_id") is not None else None
        ),
        result_schema=(
            str(data["result_schema"]) if data.get("result_schema") is not None else None
        ),
        allowed_next_stages=tuple(
            str(item) for item in _as_list(data.get("allowed_next_stages")) if isinstance(item, str)
        ),
        map_evidence=[
            dict(item) for item in data.get("map_evidence", []) if isinstance(item, dict)
        ],
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
        # 持久化 schema 版本：供 migrate_session_payload 判断是否需要升级
        "schema_version": SESSION_SCHEMA_VERSION,
        "session_id": session.session_id,
        "session_epoch": session.session_epoch,
        "agent_stack": [_frame_to_dict(f) for f in session.agent_stack],
        "pending_turn_id": session.pending_turn_id,
        "pending_tool_call_ids": sorted(session.pending_tool_call_ids),
        "turn_counter": session.turn_counter,
        "frame_counter": session.frame_counter,
        "request_id_cache": session.request_id_cache,
        "completed_tool_turn_cache": session.completed_tool_turn_cache,
        "pending_tool_calls": session.pending_tool_calls,
        "session_allow": [list(grant) for grant in sorted(session.session_allow)],
        "effort": session.effort,
        "output_style": session.output_style,
        "delegate_groups": session.delegate_groups,
        "pending_plan": session.pending_plan,
        "verify_retry_count": session.verify_retry_count,
        "pending_verify_candidates": session.pending_verify_candidates,
        "pending_map_write_after_read": session.pending_map_write_after_read,
        "pending_map_validation_after_read": session.pending_map_validation_after_read,
        "pending_map_tool_after_read": session.pending_map_tool_after_read,
        "latest_context_used_tokens": session.latest_context_used_tokens,
        "force_compact_next_turn": session.force_compact_next_turn,
        "map_task_state": session.map_task_state.to_dict(),
        "map_request_scope": session.map_request_scope.to_dict(),
        "map_task_lineage": session.map_task_lineage,
        "history_event_counter": session.history_event_counter,
        "history_events": session.history_events,
        "rag_context": session.rag_context,
        "task_run": session.task_run,
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
    # 反序列化前先执行 schema 迁移：把旧版字段映射到当前结构，
    # 迁移后 data 已经是最新 schema，后续代码无需再处理兼容
    data, _ = migrate_session_payload(data)
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
    map_task_state = MapTaskState.from_dict(data.get("map_task_state"))
    return Session(
        session_id=str(data["session_id"]),
        session_epoch=str(data.get("session_epoch", "")),
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
        completed_tool_turn_cache=_as_dict(data.get("completed_tool_turn_cache")),
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
        pending_map_write_after_read=(
            data.get("pending_map_write_after_read")
            if isinstance(data.get("pending_map_write_after_read"), dict)
            else None
        ),
        pending_map_validation_after_read=(
            data.get("pending_map_validation_after_read")
            if isinstance(data.get("pending_map_validation_after_read"), dict)
            else None
        ),
        pending_map_tool_after_read=(
            data.get("pending_map_tool_after_read")
            if isinstance(data.get("pending_map_tool_after_read"), dict)
            else None
        ),
        latest_context_used_tokens=_as_int(data.get("latest_context_used_tokens")),
        force_compact_next_turn=bool(data.get("force_compact_next_turn", False)),
        map_task_state=map_task_state,
        map_request_scope=MapRequestScope.from_dict(data.get("map_request_scope")),
        map_task_lineage=_as_dict(data.get("map_task_lineage")),
        history_event_counter=history_event_counter,
        history_events=history_events,
        rag_context=str(data.get("rag_context", "")),
        task_run=(dict(data["task_run"]) if isinstance(data.get("task_run"), dict) else None),
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

    def __init__(
        self,
        storage_dir: Path,
        *,
        project_root: Path | None = None,
        reset_failure_injector: ResetFailureInjector | None = None,
    ) -> None:
        """初始化会话存储。

        Args:
            storage_dir: 会话 JSON 文件的存放目录，按需创建。
            project_root: 工程根目录；提供后会用已占用 artifact turn 修正
                重启时的单调计数器。
        """
        self._storage_dir = storage_dir
        self._project_root = project_root
        self._reset_failure_injector = reset_failure_injector
        self._sessions: dict[str, Session] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def hit_reset_failpoint(self, name: str) -> None:
        """仅在构造时注入测试依赖后触发命名 reset 故障。"""
        if name not in RESET_FAILPOINTS:
            raise ValueError(f"unknown reset failpoint: {name}")
        if self._reset_failure_injector is not None:
            self._reset_failure_injector.hit(name)

    @staticmethod
    def _new_epoch() -> str:
        """生成不可预测、可安全写入 JSON/路径摘要的会话 epoch。"""
        return secrets.token_urlsafe(24)

    def _epoch_path_for(self, session_id: str) -> Path:
        """返回独立于 Session 正文的 epoch barrier 路径。"""
        return self._storage_dir / "_epochs" / f"{_safe_filename(session_id)}.json"

    def _reset_path_for(self, session_id: str) -> Path:
        """返回幂等 reset 记录路径。"""
        return self._storage_dir / "_resets" / f"{_safe_filename(session_id)}.json"

    def _task_run_path_for(self, session_id: str) -> Path:
        """返回独立 Attempt journal 路径，避免破坏 Session 原子提交边界。"""
        return self._storage_dir / "_attempts" / f"{_safe_filename(session_id)}.json"

    def current_epoch(self, session_id: str, *, create: bool = True) -> str | None:
        """读取当前持久化 epoch；首次会话可按需原子创建。"""
        path = self._epoch_path_for(session_id)
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                epoch = payload.get("session_epoch") if isinstance(payload, dict) else None
                if isinstance(epoch, str) and epoch:
                    return epoch
            except (OSError, UnicodeError, json.JSONDecodeError):
                logger.exception(
                    "Session epoch barrier is unreadable session=%s path=%s", session_id, path
                )
                raise
            raise ValueError(f"invalid session epoch barrier: {path}")
        if not create:
            return None
        epoch = self._new_epoch()
        atomic_write_json(
            path,
            {
                "version": 1,
                "session_id": session_id,
                "session_epoch": epoch,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return epoch

    def begin_reset(
        self,
        session_id: str,
        *,
        last_event_seq: int = 0,
    ) -> dict[str, Any]:
        """持久化 reset 记录并先切换 epoch，形成不可回退的逻辑隔离屏障。"""
        old_epoch = self.current_epoch(session_id)
        new_epoch = self._new_epoch()
        reset_id = secrets.token_urlsafe(18)
        record: dict[str, Any] = {
            "version": 1,
            "reset_id": reset_id,
            "session_id": session_id,
            "old_epoch": old_epoch,
            "new_epoch": new_epoch,
            "last_event_seq": max(0, last_event_seq),
            "state": "prepared",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        reset_path = self._reset_path_for(session_id)
        atomic_write_json(reset_path, record)
        try:
            self.hit_reset_failpoint("reset_record_after_prepare")
            self.hit_reset_failpoint("epoch_barrier_before_write")
            atomic_write_json(
                self._epoch_path_for(session_id),
                {
                    "version": 1,
                    "session_id": session_id,
                    "session_epoch": new_epoch,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except (OSError, TypeError, ValueError):
            if self.current_epoch(session_id, create=False) == old_epoch:
                reset_path.unlink(missing_ok=True)
            raise
        try:
            self.hit_reset_failpoint("epoch_barrier_after_write")
        except (OSError, TypeError, ValueError):
            logger.exception(
                "Reset failpoint fired after durable epoch barrier session=%s",
                session_id,
            )
        record["state"] = "epoch_switched"
        record["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            atomic_write_json(reset_path, record)
            self.hit_reset_failpoint("reset_record_after_epoch_switch")
        except (OSError, TypeError, ValueError):
            logger.exception(
                "Reset checkpoint failed after durable epoch barrier session=%s",
                session_id,
            )
        self._sessions.pop(session_id, None)
        return record

    def pending_reset_records(self) -> list[dict[str, Any]]:
        """枚举需在启动时继续清理的 reset 记录。"""
        reset_dir = self._storage_dir / "_resets"
        if not reset_dir.exists():
            return []
        records: list[dict[str, Any]] = []
        for path in reset_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                logger.warning("Ignoring unreadable reset record path=%s", path)
                continue
            if (
                isinstance(payload, dict)
                and payload.get("state") not in {"cleaned", "aborted"}
                and isinstance(payload.get("session_id"), str)
            ):
                records.append(payload)
        return records

    def abandon_reset(self, record: dict[str, Any], reason: str) -> None:
        """在 epoch 从未切换或记录已失去所有权时终止 reset 记录。"""
        abandoned = dict(record)
        abandoned["state"] = "aborted"
        abandoned["abort_reason"] = reason
        abandoned["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(
            self._reset_path_for(str(record["session_id"])),
            abandoned,
        )

    def remove_session_payload(self, session_id: str) -> None:
        """删除精确 Session 正文；epoch barrier 与 reset 记录不在此处删除。"""
        self._sessions.pop(session_id, None)
        self._path_for(session_id).unlink(missing_ok=True)
        self._task_run_path_for(session_id).unlink(missing_ok=True)

    def finish_reset(self, record: dict[str, Any]) -> None:
        """把 reset 记录推进为 cleaned；重复调用保持幂等。"""
        session_id = str(record["session_id"])
        current_epoch = self.current_epoch(session_id, create=False)
        if current_epoch != record.get("new_epoch"):
            raise ValueError("reset record no longer owns the current session epoch")
        completed = dict(record)
        completed["state"] = "cleaned"
        completed["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.hit_reset_failpoint("reset_record_before_cleaned")
        atomic_write_json(self._reset_path_for(session_id), completed)
        try:
            self.hit_reset_failpoint("reset_record_after_cleaned")
        except (OSError, TypeError, ValueError):
            logger.exception(
                "Reset failpoint fired after durable cleaned marker session=%s",
                session_id,
            )

    def reset_step_completed(self, record: dict[str, Any], resource_id: str) -> bool:
        """返回 reset 记录是否已持久完成指定资源清理。"""
        completed = record.get("completed_resources", [])
        return isinstance(completed, list) and resource_id in completed

    def complete_reset_step(
        self,
        record: dict[str, Any],
        resource_id: str,
    ) -> None:
        """幂等记录一个资源清理步骤，支持崩溃后从精确边界继续。"""
        completed_raw = record.get("completed_resources", [])
        completed = [str(item) for item in completed_raw] if isinstance(completed_raw, list) else []
        if resource_id not in completed:
            completed.append(resource_id)
        record["completed_resources"] = completed
        self.checkpoint_reset(record, f"cleaning:{resource_id}")

    def checkpoint_reset(self, record: dict[str, Any], state: str) -> None:
        """持久化 reset 清理进度，供崩溃后幂等续跑。"""
        checkpoint = dict(record)
        checkpoint["state"] = state
        checkpoint["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(
            self._reset_path_for(str(checkpoint["session_id"])),
            checkpoint,
        )
        record.update(checkpoint)

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
        epoch = self.current_epoch(session_id)
        session = (
            restored
            if restored is not None
            else Session(session_id=session_id, session_epoch=epoch or "")
        )
        if not session.session_epoch:
            session.session_epoch = epoch or ""
        elif session.session_epoch != epoch:
            logger.warning(
                "Ignoring stale Session payload session=%s stored_epoch=%s current_epoch=%s",
                session_id,
                session.session_epoch,
                epoch,
            )
            session = Session(session_id=session_id, session_epoch=epoch or "")
        task_run = self._load_task_run(session_id, session.session_epoch)
        if task_run is not None:
            session.task_run = task_run
        session._turn_counter_reserver = self._reserve_turn_counter
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
        current_epoch = self.current_epoch(session.session_id)
        if not session.session_epoch:
            if self._project_root is not None:
                from app.orchestrator.map_artifacts import adopt_legacy_artifact_epoch

                adopt_legacy_artifact_epoch(
                    self._project_root,
                    session.session_id,
                    current_epoch or "",
                )
            session.session_epoch = current_epoch or ""
        if session.session_epoch != current_epoch:
            raise ValueError(
                "refusing to persist stale Session epoch "
                f"session={session.session_id} stored={session.session_epoch!r} "
                f"current={current_epoch!r}"
            )
        path = self._path_for(session.session_id)
        persisted = self._read_persisted_payload(path)
        if persisted.get("session_epoch") == session.session_epoch:
            session.turn_counter = max(
                session.turn_counter,
                _as_int(persisted.get("turn_counter")),
            )
            session.history_event_counter = max(
                session.history_event_counter,
                _as_int(persisted.get("history_event_counter")),
            )
        session._turn_counter_reserver = self._reserve_turn_counter
        atomic_write_json(path, session_to_dict(session))
        self._sessions[session.session_id] = session
        logger.debug(
            "Session saved session=%s frames=%d pending=%s cache_entries=%d path=%s",
            session.session_id,
            len(session.agent_stack),
            session.pending_turn_id is not None,
            len(session.request_id_cache),
            path,
        )

    def save_task_run(self, session: Session) -> None:
        """独立持久化 TaskRun/Attempt，不提前发布 Session 业务状态。"""
        current_epoch = self.current_epoch(session.session_id)
        if session.session_epoch != current_epoch:
            raise ValueError("refusing to persist TaskRun for a stale session epoch")
        if session.task_run is None:
            self._task_run_path_for(session.session_id).unlink(missing_ok=True)
            return
        atomic_write_json(
            self._task_run_path_for(session.session_id),
            {
                "version": 1,
                "session_id": session.session_id,
                "session_epoch": session.session_epoch,
                "task_run": session.task_run,
            },
        )

    def _load_task_run(
        self,
        session_id: str,
        session_epoch: str,
    ) -> dict[str, Any] | None:
        """读取当前 epoch 的独立 Attempt journal；旧 epoch 一律忽略。"""
        path = self._task_run_path_for(session_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("TaskRun journal is unreadable session=%s path=%s", session_id, path)
            return None
        if (
            not isinstance(payload, dict)
            or payload.get("session_id") != session_id
            or payload.get("session_epoch") != session_epoch
            or not isinstance(payload.get("task_run"), dict)
        ):
            return None
        return dict(payload["task_run"])

    def task_run_session_ids(self) -> list[str]:
        """枚举拥有独立 TaskRun journal 的会话 id。"""
        task_dir = self._storage_dir / "_attempts"
        if not task_dir.exists():
            return []
        session_ids: list[str] = []
        for path in task_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                logger.warning("Ignoring unreadable TaskRun journal path=%s", path)
                continue
            session_id = payload.get("session_id") if isinstance(payload, dict) else None
            if isinstance(session_id, str) and session_id:
                session_ids.append(session_id)
        return sorted(set(session_ids))

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
        current = self._sessions.get(session_id)
        if current is not None:
            session.turn_counter = max(
                session.turn_counter,
                current.turn_counter,
            )
        session._turn_counter_reserver = self._reserve_turn_counter
        self._sessions[session_id] = session

    def _read_persisted_payload(self, path: Path) -> dict[str, Any]:
        """Read the persisted object used to merge monotonic counters."""
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _reserve_turn_counter(self, session_id: str, value: int) -> None:
        """Durably reserve a turn number before exposing it to a caller."""
        path = self._path_for(session_id)
        payload = self._read_persisted_payload(path)
        current_epoch = self.current_epoch(session_id)
        if payload and payload.get("session_epoch") not in {None, "", current_epoch}:
            payload = {}
        if value <= _as_int(payload.get("turn_counter")):
            return
        if not payload:
            payload = {
                "schema_version": SESSION_SCHEMA_VERSION,
                "session_id": session_id,
                "session_epoch": current_epoch,
            }
        payload["session_epoch"] = current_epoch
        payload["turn_counter"] = value
        atomic_write_json(path, payload)

    def reset(self, session_id: str) -> None:
        """清空指定会话（内存与本地持久化文件）。

        Args:
            session_id: 待清空的会话 id。
        """
        record = self.begin_reset(session_id)
        self.remove_session_payload(session_id)
        self.finish_reset(record)
        logger.info(
            "Session reset completed session=%s epoch=%s",
            session_id,
            record["new_epoch"],
        )

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
            # 加载时先检测持久化文件的 schema 版本，执行迁移后再反序列化，
            # 使旧文件能透明升级到当前 SESSION_SCHEMA_VERSION
            if not isinstance(data, dict):
                raise ValueError("session payload must be an object")
            source_version = session_payload_version(data)
            migrated_data, migrated = migrate_session_payload(data)
            session = session_from_dict(migrated_data, available_tools)
            current_epoch = self.current_epoch(session_id)
            if not session.session_epoch:
                session.session_epoch = current_epoch or ""
                migrated = True
            elif session.session_epoch != current_epoch:
                logger.info(
                    "Stale Session payload isolated session=%s stored_epoch=%s current_epoch=%s",
                    session_id,
                    session.session_epoch,
                    current_epoch,
                )
                return None
            if self._project_root is not None:
                from app.orchestrator.map_artifacts import MapArtifactStore

                session.turn_counter = max(
                    session.turn_counter,
                    MapArtifactStore(
                        self._project_root,
                        session_id,
                        session_epoch=session.session_epoch,
                    ).max_reserved_turn_counter(),
                )
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
        # 若发生了 schema 迁移，立即将升级后的会话回写磁盘，
        # 避免每次加载都重复执行迁移逻辑
        if migrated:
            atomic_write_json(path, session_to_dict(session))
            logger.info(
                "Session payload migrated session=%s from_version=%d to_version=%d path=%s",
                session_id,
                source_version,
                SESSION_SCHEMA_VERSION,
                path,
            )
        logger.debug("Session loaded from disk session=%s path=%s", session_id, path)
        return session
