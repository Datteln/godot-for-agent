"""QueryEngine 门面（§13）：HTTP 层与 query_loop 内核之间的会话协调层。

`QueryEngine` 负责：
- 会话锁与本地持久化；
- 用户消息、前端工具结果与 agent 帧消息的转换；
- `request_id` 幂等缓存；
- 当前请求权限模式覆盖；
- 调用 `orchestrator.agent.run_turn()` 并转换为 HTTP DTO。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition
from app.api.schemas import (
    ChatErrorResponse,
    ChatFinalResponse,
    ChatRequest,
    ChatResponse,
    ChatToolCallsResponse,
    FrontToolCallDTO,
    InterruptResponse,
    SessionHistoryResponse,
    ToolResult,
)
from app.config import AppSettings
from app.events.store import Event, EventStore
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector
from app.llm.provider import LLMProvider
from app.orchestrator.agent import (
    AgentPromptFactory,
    ErrorResult,
    FinalResult,
    StepResult,
    ToolCallsResult,
    run_turn,
)
from app.orchestrator.map_workers import MAP_REVISION_GUARDED_TOOL_NAMES
from app.output_styles.catalog import OutputStyleCatalog
from app.permissions.engine import make_session_allow_grant
from app.prompt.builder import LayeredPrompt, build_system_prompt
from app.prompt.context_builder import ContextBuilder
from app.prompt.project_context import build_project_context
from app.prompt.rag_context import build_rag_context
from app.query.compactor import SessionCompactor
from app.query.helpers import *
from app.rag.asset_llm_client import AssetLLMClient, AssetLLMConfig
from app.rag.factory import create_codebase_index
from app.recovery.pointer import RecoveryPointerStore
from app.security.settings import SecuritySettings, security_settings_from_app
from app.sessions.store import Session, SessionStore
from app.skills.catalog import SkillCatalog
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY
from app.storage.atomic import atomic_write_json
from app.verify.runner import VerifyRunner

logger = logging.getLogger(__name__)
_MODEL_LOG_FIELDS = frozenset({"model", "primary_model", "fallback_model"})
_MAP_ARTIFACT_MAX_FILES_PER_SESSION = 128
_MAP_ARTIFACT_MAX_BYTES_PER_SESSION = 100 * 1024 * 1024


def _normalize_model_override(model: str | None) -> str | None:
    """清理请求级模型覆盖；空白值等同于未指定。"""
    if model is None:
        return None
    normalized = model.strip()
    return normalized or None


def _event_payload_for_log(payload: dict[str, Any]) -> dict[str, Any]:
    """隐藏事件日志中的模型名，不影响发送给 UI 的原始事件。"""
    return {
        key: "<redacted>" if key in _MODEL_LOG_FIELDS else value for key, value in payload.items()
    }


def _response_from_dict(data: dict[str, Any]) -> ChatResponse:
    """把幂等缓存中的响应字典恢复为具体 DTO。"""
    response_type = data.get("type")
    if response_type == "tool_calls":
        return ChatToolCallsResponse.model_validate(data)
    if response_type == "final":
        return ChatFinalResponse.model_validate(data)
    return ChatErrorResponse.model_validate(data)


def _step_to_response(step: StepResult) -> ChatResponse:
    """把编排内核结果转换为 `/chat` 三态响应 DTO。"""
    if isinstance(step, ToolCallsResult):
        return ChatToolCallsResponse(
            turn_id=step.turn_id,
            text=step.text,
            calls=[
                FrontToolCallDTO(
                    id=call.id,
                    name=call.name,
                    input=call.input,
                    needs_confirm=call.needs_confirm,
                    frame_id=call.frame_id,
                    agent=call.agent,
                    render_kind=call.render_kind,
                )
                for call in step.calls
            ],
        )
    if isinstance(step, FinalResult):
        return ChatFinalResponse(text=step.text)
    if isinstance(step, ErrorResult):
        return ChatErrorResponse(text=step.text)
    raise TypeError(f"未知编排结果类型：{type(step)!r}")


from app.query.helpers import (
    _MAP_VALIDATION_TOOL_NAMES,
    _PERSISTED_HISTORY_EVENT_TYPES,
    _append_platform_planning_failure_hint,
    _build_user_content,
    _clear_validation_blockers,
    _defer_map_tool_for_region_read,
    _defer_map_validation_for_state_read,
    _defer_map_write_for_state_read,
    _has_only_map_review_required,
    _has_review_blocker,
    _history_context_used_tokens,
    _history_payload_for_front_tool,
    _json_char_size,
    _map_completion_blocker,
    _map_completion_gate_text,
    _persisted_history_events,
    _region_summary_from_value,
    _remember_latest_map_region_read,
    _remember_latest_map_revision,
    _replace_last_assistant_final,
    _resume_pending_map_tool_after_read,
    _resume_pending_map_validation_after_read,
    _resume_pending_map_write_after_read,
    _review_required_blocker,
    _safe_artifact_name,
    _schedule_map_completion_continuation,
    _schedule_map_reviewer_if_required,
    _schedule_revision_conflict_reader,
    _structured_session_history,
    _tool_history_blocks,
    _tool_message,
    _update_map_context_state,
)
from app.query.history_to_events import blocks_to_pseudo_events


class QueryEngine:
    """会话级 QueryEngine 门面。

    M0 中该对象可作为进程级单例：内部把不同 `session_id` 分发给
    `SessionStore`，并用 per-session lock 串行化同一会话的请求。
    """

    def __init__(
        self,
        settings: AppSettings,
        session_store: SessionStore,
        llm: LLMProvider,
        base_security: SecuritySettings | None = None,
        skill_catalog: SkillCatalog | None = None,
        output_style_catalog: OutputStyleCatalog | None = None,
        event_store: EventStore | None = None,
        recovery_store: RecoveryPointerStore | None = None,
        cache_engine: CacheDecisionEngine | None = None,
        cache_metrics: CacheMetricsCollector | None = None,
    ) -> None:
        """构造 QueryEngine。

        Args:
            settings: 服务配置。
            session_store: 会话持久化存储。
            llm: 大模型 provider。
            base_security: 启动时解析出的安全边界；缺省时从 settings 构造。
            cache_engine: 上下文缓存决策引擎（§16.1）；缺省时构造新实例。
            cache_metrics: 缓存命中率观测聚合器；缺省时构造新实例。
        """
        self._settings = settings
        self._store = session_store
        self._llm = llm
        self._base_security = base_security or security_settings_from_app(settings)
        self._skill_catalog = skill_catalog
        self._output_styles = output_style_catalog
        self._events = event_store
        self._recovery = recovery_store
        self._cache_engine = cache_engine or CacheDecisionEngine()
        self._cache_metrics = cache_metrics or CacheMetricsCollector()
        self._verify_runner = VerifyRunner(
            settings,
            llm,
            self._emit,
            self._model_for_effort,
            self._thinking_budget_for_effort,
        )
        self._compactor = SessionCompactor(
            settings,
            session_store,
            llm,
            self._cache_engine,
            self._emit,
            lambda: self.available_tools,
            self._model_for_effort,
        )
        # session_id -> 该会话当前所有"正在处理 /chat 请求"的任务集合（通常只有
        # 一个，但用户可能在前一个请求仍卡在 per-session 锁等待时就发出下一条
        # 消息/中断，short-lived 地出现多个；用 set 而不是单个槎位，避免新任务
        # 覆盖掉真正持有锁、仍在运行的旧任务引用，导致 interrupt() 取消错对象。
        self._active_tasks: dict[str, set[asyncio.Task]] = {}

    @property
    def available_tools(self) -> set[str]:
        """当前工具注册表里的可见工具名集合。"""
        return set(REGISTRY)

    def _map_artifact_path(self, session_id: str, tool_name: str, result: dict[str, Any]) -> Path:
        """构造地图 raw artifact 的项目内路径。"""
        target = str(result.get("target_path", result.get("target", "map")))
        revision = result.get("map_revision", result.get("actual_revision", "unknown"))
        digest = hashlib.sha256(
            json.dumps(
                {
                    "tool": tool_name,
                    "target": target,
                    "revision": revision,
                    "region": _region_summary_from_value(result),
                    "size": _json_char_size(result),
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:16]
        return (
            self._settings.project_root
            / ".ai_agent_service"
            / "artifacts"
            / _safe_artifact_name(session_id)
            / f"{_safe_artifact_name(tool_name)}-{digest}.json"
        )

    def _store_map_artifact(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        result: Any,
    ) -> str | None:
        """把大型地图工具 raw result 写入本地 artifact，返回相对路径引用。"""
        if tool_name not in {
            "describe_map_region",
            "query_spatial_index",
            "validate_map_region",
            "validate_layer_coverage",
            "validate_object_placements",
        }:
            return None
        if not isinstance(result, dict):
            return None
        if tool_name == "describe_map_region" and not (
            isinstance(result.get("cells"), list) or "atlas_summary" in result
        ):
            return None
        if tool_name == "query_spatial_index" and not isinstance(result.get("matches"), list):
            return None
        if tool_name in _MAP_VALIDATION_TOOL_NAMES and _json_char_size(result) < 8_000:
            return None
        path = self._map_artifact_path(session_id, tool_name, result)
        try:
            atomic_write_json(
                path,
                {
                    "tool": tool_name,
                    "input": tool_args,
                    "result": result,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            self._cleanup_map_artifacts(path.parent)
        except OSError as exc:
            logger.warning(
                "Failed to write map artifact session=%s tool=%s path=%s error=%s",
                session_id,
                tool_name,
                path,
                exc,
            )
            return None
        try:
            return str(path.relative_to(self._settings.project_root)).replace("\\", "/")
        except ValueError:
            return str(path)

    def _cleanup_map_artifacts(self, session_dir: Path) -> None:
        """按 LRU 清理单个 session 的地图 artifact 文件。"""
        try:
            files = [path for path in session_dir.iterdir() if path.is_file()]
        except OSError:
            return
        stats: list[tuple[Path, float, int]] = []
        for path in files:
            try:
                stat = path.stat()
            except OSError:
                continue
            stats.append((path, stat.st_mtime, stat.st_size))
        stats.sort(key=lambda item: item[1], reverse=True)
        total = 0
        for index, (path, _mtime, size) in enumerate(stats):
            total += size
            if (
                index < _MAP_ARTIFACT_MAX_FILES_PER_SESSION
                and total <= _MAP_ARTIFACT_MAX_BYTES_PER_SESSION
            ):
                continue
            try:
                path.unlink()
            except OSError:
                logger.debug("Failed to prune map artifact path=%s", path)

    def session_history(self, session_id: str, limit: int = 200) -> SessionHistoryResponse:
        """Return frontend-renderable history for a persisted session."""
        session = self._store.get_or_create(session_id, self.available_tools)
        events = _persisted_history_events(session)
        if not events and self._events is not None:
            events = self._events.list_after(session_id, 0)
        # 下面的逐 frame/event 转换是 O(frames + events) 的纯 Python 工作；长期
        # 使用的会话（大量 delegate_many 子 agent frame + 持续累积的事件日志）
        # 不加界会让这一步随历史总量无限增长，最终触发前端 30s 看门狗超时、把
        # 本来该串行复用的请求队列卡死。既然最终只展示最近 `limit` 条，这里先
        # 把输入收窄到最近窗口再转换，而不是转换全量历史后再丢弃大半。
        if limit > 0:
            recent_frames = session.agent_stack[-limit:]
            recent_events = events[-(limit * 8) :] if len(events) > limit * 8 else events
        else:
            recent_frames = session.agent_stack
            recent_events = events
        blocks = _structured_session_history(recent_frames, recent_events)
        if limit > 0 and len(blocks) > limit:
            blocks = blocks[-limit:]
        pseudo_events = blocks_to_pseudo_events(blocks)
        logger.info(
            "Session history requested session=%s frames=%d/%d blocks=%d events=%d pending=%s",
            session_id,
            len(recent_frames),
            len(session.agent_stack),
            len(blocks),
            len(pseudo_events),
            session.pending_turn_id is not None,
        )
        return SessionHistoryResponse(
            session_id=session.session_id,
            last_event_seq=self._events.last_seq(session_id) if self._events is not None else 0,
            pending_turn_id=session.pending_turn_id,
            context_used_tokens=_history_context_used_tokens(session, events),
            context_token_limit=self._settings.auto_compact_token_threshold,
            pseudo_events=pseudo_events,
        )

    async def submit_user_turn(self, request: ChatRequest) -> ChatResponse:
        """处理一次 `/chat` 请求。

        `user_message` 发起新用户轮次；`tool_results` 回填上一轮 front 工具结果。
        两者不可同时出现，且会话有 pending 工具结果时拒绝新用户消息。

        本方法把当前 `asyncio.Task` 登记到 `_active_tasks`，使
        `interrupt()` 能在用户点击"停止"时真正取消仍在运行的 agent 循环
        （而不是仅让前端断开 HTTP 连接、后端继续跑完整个 turn）。
        """
        task = asyncio.current_task()
        if task is not None:
            self._active_tasks.setdefault(request.session_id, set()).add(task)
        try:
            async with self._store.lock_for(request.session_id):
                session = self._store.get_or_create(request.session_id, self.available_tools)
                logger.info(
                    "Chat request accepted session=%s request_id=%s has_user=%s tool_results=%d",
                    request.session_id,
                    request.request_id,
                    request.user_message is not None,
                    len(request.tool_results or []),
                )

                if (
                    request.request_id is not None
                    and request.request_id in session.request_id_cache
                ):
                    logger.info(
                        "Chat idempotency hit session=%s request_id=%s",
                        request.session_id,
                        request.request_id,
                    )
                    return _response_from_dict(session.request_id_cache[request.request_id])

                # 取消保护快照：本轮可能在追加 assistant 的 tool_calls 后、写入对应
                # tool result 之前被 interrupt 取消。若让这半截历史留在内存里，下一次
                # 请求发给 OpenAI 兼容端点会因 tool_call 缺少 tool result 而 400。取消
                # 时回滚到本轮开始前的内存快照（本轮尚未 save()，磁盘仍是旧版本）。
                snapshot = copy.deepcopy(session)
                try:
                    response = await self._submit_locked(session, request)
                except asyncio.CancelledError:
                    self._store.replace_in_memory(request.session_id, snapshot)
                    raise

                if request.request_id is not None:
                    session.request_id_cache[request.request_id] = response.model_dump()
                self._store.save(session)
                self._record_recovery(session, response)
                logger.info(
                    "Chat request completed session=%s response_type=%s pending=%s",
                    request.session_id,
                    response.type,
                    session.pending_turn_id is not None,
                )
                logger.debug(
                    "Chat response details session=%s type=%s response=%s",
                    request.session_id,
                    response.type,
                    json.dumps(response.model_dump(), ensure_ascii=False, default=str),
                )
                return response
        finally:
            if task is not None:
                tasks = self._active_tasks.get(request.session_id)
                if tasks is not None:
                    tasks.discard(task)
                    if not tasks:
                        del self._active_tasks[request.session_id]

    async def _submit_locked(self, session: Session, request: ChatRequest) -> ChatResponse:
        """在持有会话锁时执行一次请求。"""
        has_user = request.user_message is not None
        has_results = request.tool_results is not None
        if has_user == has_results:
            logger.warning(
                "Invalid chat request shape session=%s has_user=%s has_results=%s",
                session.session_id,
                has_user,
                has_results,
            )
            return ChatErrorResponse(text="user_message 与 tool_results 必须二选一")

        security = self._security_for_request(request)
        model_override = _normalize_model_override(request.model)

        if request.effort is not None:
            session.effort = request.effort
            logger.info(
                "Session effort overridden session=%s effort=%s", session.session_id, request.effort
            )
        if request.output_style is not None:
            session.output_style = request.output_style
            logger.info(
                "Session output style overridden session=%s output_style=%s",
                session.session_id,
                request.output_style,
            )

        # RAG 段（L3）：用户新提问时刷新检索结果，工具结果回填等同一轮的后续
        # 请求里复用 `session.rag_context`，使该段在整轮 agent 循环内保持稳定、
        # 可被缓存（§16.1 RAG 段缓存）。
        if request.user_message is not None:
            session.rag_context = await self._retrieve_rag_context(security, request.user_message)

        project_context = build_project_context(security.project_root)
        coordinator = get_agent("coordinator", self.available_tools)
        cache_context = ContextBuilder().build(
            stable_prefix=build_system_prompt(
                coordinator,
                self._skill_catalog,
                self._output_styles,
                session.output_style,
            ),
            structure_context=project_context,
            dynamic_context=session.rag_context,
            query=request.user_message or "",
        )
        root_snapshot = session.agent_stack[0].compact_snapshot if session.agent_stack else None
        layered_prompt = LayeredPrompt(
            core=cache_context.stable_prefix,
            structure_context=cache_context.structure_context,
            compact_context=root_snapshot.summary if root_snapshot is not None else "",
            rag_context=cache_context.dynamic_context,
        )
        # `agent.prompt` 保留拼平后的纯文本（供委派子帧继承等需要字符串的场景）；
        # 根帧的 system 消息则写成分层 content-block 数组，使缓存层可为每层（L0
        # 核心 / L2 项目上下文 / L3 RAG）独立标记 `cache_control`，实现多断点缓存
        # （§16.1 / 文档 3.1）。content_blocks 不带 `cache_control`，标记在请求时
        # 由 provider 按 CacheDecisionEngine 的断点注入，不写入会话历史。
        coordinator = replace(coordinator, prompt=layered_prompt.to_text())
        session.ensure_root_frame(coordinator)
        root = session.agent_stack[0]
        root.agent = coordinator
        if root.messages and root.messages[0].get("role") == "system":
            # 只有真正分层（≥2 层）时才写成 content-block 数组以启用多断点；单层
            # （无项目文档/RAG，最常见）保持纯字符串，与改造前完全一致、零行为变化。
            layers = layered_prompt.layers()
            root.messages[0]["content"] = (
                layered_prompt.to_content_blocks() if len(layers) >= 2 else layered_prompt.to_text()
            )

        if has_results:
            self._emit(
                session.session_id,
                "tool_results_received",
                {"count": len(request.tool_results or [])},
            )
            logger.info(
                "Appending front tool results session=%s count=%d pending_turn=%s",
                session.session_id,
                len(request.tool_results or []),
                session.pending_turn_id,
            )
            result_error, verify_candidates = await self._append_tool_results(
                session, request.tool_results or [], security
            )
            if result_error is not None:
                logger.warning(
                    "Front tool result rejected session=%s reason=%s",
                    session.session_id,
                    result_error.text,
                )
                return result_error
            if verify_candidates:
                session.pending_verify_candidates.extend(verify_candidates)
            resumed = self._resume_pending_map_tool_calls(session)
            if resumed is not None:
                return resumed
        else:
            if session.pending_turn_id is not None:
                logger.warning(
                    "User message rejected because tools are pending session=%s pending_turn=%s",
                    session.session_id,
                    session.pending_turn_id,
                )
                return ChatErrorResponse(text="当前会话仍有待回传的工具结果，不能开始新的用户消息")
            frame = session.top_frame()
            if frame is None:
                logger.error(
                    "User message rejected because session has no active frame session=%s",
                    session.session_id,
                )
                return ChatErrorResponse(text="会话没有活跃的 agent 帧")
            frame.messages.append({"role": "user", "content": _build_user_content(request)})
            session.pending_verify_candidates.clear()
            session.map_completion_blockers.clear()
            self._emit(
                session.session_id, "user_submitted", {"has_context": request.context is not None}
            )
            logger.info(
                "User turn appended session=%s has_context=%s language_hint=%s",
                session.session_id,
                request.context is not None,
                request.language_hint,
            )

        # 自动压缩（§16.1 策略 A）：新消息/工具结果已追加完毕、即将驱动 LLM 之前
        # 检查体积——这样下面 run_turn 实际发出的请求已经是压缩后的大小，而不是
        # "先发一次超大请求，下次才生效"。只在体积越界时才触发，不影响正常大小
        # 会话的行为；阈值用粗估 token 数而非精确计费值，足够判断"是否该收紧"。
        if self._settings.auto_compact_enabled and self._needs_auto_compact(session):
            logger.info(
                "Auto-compact triggered session=%s threshold=%d keep_recent=%d",
                session.session_id,
                self._settings.auto_compact_token_threshold,
                self._settings.auto_compact_keep_recent,
            )
            await self._compact_locked_async(
                session.session_id,
                keep_recent=self._settings.auto_compact_keep_recent,
                triggered_by="auto",
                use_llm=request.compact_summary_use_llm,
            )

        defer_verification_until_final = bool(session.pending_verify_candidates)

        def emit_turn_event(event_type: str, payload: dict[str, Any]) -> None:
            if defer_verification_until_final and event_type in {
                "agent_text_delta",
                "agent_reasoning_delta",
            }:
                return
            self._emit(session.session_id, event_type, payload)

        async def build_child_agent_prompt(agent: AgentDefinition, task: str) -> str:
            """为委派子 agent 构造按任务检索的分层 system prompt。"""
            task_rag_context = await self._retrieve_rag_context(security, task)
            child_context = ContextBuilder().build(
                stable_prefix=build_system_prompt(
                    agent,
                    self._skill_catalog,
                    self._output_styles,
                    session.output_style,
                ),
                structure_context=project_context,
                dynamic_context=task_rag_context,
                query=task,
            )
            return cast(
                str,
                LayeredPrompt(
                    core=child_context.stable_prefix,
                    structure_context=child_context.structure_context,
                    rag_context=child_context.dynamic_context,
                ).to_text(),
            )

        def emit_verify_turn_event(event_type: str, payload: dict[str, Any]) -> None:
            self._emit(session.session_id, event_type, payload)

        step = await self._run_agent_turn(
            session,
            security,
            model_override,
            build_child_agent_prompt,
            emit_turn_event,
        )
        response = _step_to_response(step)
        response = self._defer_map_tool_calls_if_needed(session, response)
        if isinstance(response, ChatFinalResponse) and session.pending_verify_candidates:
            final_frame = session.top_frame()
            if final_frame is not None and final_frame.messages:
                last_message = final_frame.messages[-1]
                if last_message.get("role") == "assistant" and not last_message.get("tool_calls"):
                    final_frame.messages.pop()
            latest_by_path: dict[str, dict[str, Any]] = {}
            for candidate in session.pending_verify_candidates:
                path = str(candidate.get("path", ""))
                if path:
                    latest_by_path[path] = candidate
            session.pending_verify_candidates.clear()
            if latest_by_path:
                await self._run_verify(
                    session, security, list(latest_by_path.values()), model_override
                )
                step = await self._run_agent_turn(
                    session,
                    security,
                    model_override,
                    build_child_agent_prompt,
                    emit_verify_turn_event,
                )
                response = _step_to_response(step)
                response = self._defer_map_tool_calls_if_needed(session, response)
        map_gate_continuations = 0
        while (
            isinstance(response, ChatFinalResponse)
            and session.map_completion_blockers
            and map_gate_continuations < 3
        ):
            scheduled = False
            if _has_only_map_review_required(session.map_completion_blockers):
                scheduled = _schedule_map_reviewer_if_required(session)
                if scheduled:
                    logger.info(
                        "Map completion gate scheduled reviewer continuation session=%s",
                        session.session_id,
                    )
            if not scheduled:
                scheduled = _schedule_map_completion_continuation(session)
                if scheduled:
                    logger.info(
                        "Map completion gate scheduled repair continuation session=%s blockers=%d",
                        session.session_id,
                        len(session.map_completion_blockers),
                    )
            if not scheduled:
                break
            map_gate_continuations += 1
            step = await self._run_agent_turn(
                session,
                security,
                model_override,
                build_child_agent_prompt,
                emit_verify_turn_event,
            )
            response = _step_to_response(step)
            response = self._defer_map_tool_calls_if_needed(session, response)
        if isinstance(response, ChatToolCallsResponse):
            self._emit_tool_call_response(
                session,
                response,
                "Chat produced front tool calls session=%s turn_id=%s count=%d",
            )
        elif isinstance(response, ChatFinalResponse):
            if session.map_completion_blockers:
                gated_text = _map_completion_gate_text(session.map_completion_blockers)
                _replace_last_assistant_final(session, gated_text)
                response = ChatFinalResponse(text=gated_text)
            self._emit(session.session_id, "final", {"text_length": len(response.text)})
            logger.info(
                "Chat produced final response session=%s text_length=%d",
                session.session_id,
                len(response.text),
            )
        else:
            self._emit(session.session_id, "error", {"text": response.text})
            logger.warning(
                "Chat produced error response session=%s text=%s", session.session_id, response.text
            )
        return response

    async def _run_agent_turn(
        self,
        session: Session,
        security: SecuritySettings,
        model_override: str | None,
        agent_prompt_factory: AgentPromptFactory,
        event_callback: Callable[[str, dict[str, Any]], None],
    ) -> StepResult:
        """用当前 QueryEngine 依赖运行一轮 agent 编排。"""
        return await run_turn(
            session=session,
            llm=self._llm,
            security=security,
            tool_ctx=ToolContext(
                security=security,
                session_id=session.session_id,
                skill_catalog=self._skill_catalog,
                rag_index_path=self._settings.resolved_rag_index_path(),
            ),
            max_turns=self._settings.max_turns,
            session_allow=session.session_allow,
            agent_prompt_factory=agent_prompt_factory,
            model_selector=self._model_for_effort,
            model_override=model_override,
            thinking_budget_selector=self._thinking_budget_for_effort,
            event_callback=event_callback,
            cache_engine=self._cache_engine,
            cache_metrics=self._cache_metrics,
            context_token_limit=self._settings.auto_compact_token_threshold,
        )

    def _defer_map_tool_calls_if_needed(
        self,
        session: Session,
        response: ChatResponse,
    ) -> ChatResponse:
        """按既有顺序挂起需要先读取地图状态的工具调用。"""
        if not isinstance(response, ChatToolCallsResponse):
            return response
        response = _defer_map_tool_for_region_read(session, response)
        response = _defer_map_write_for_state_read(session, response)
        return _defer_map_validation_for_state_read(session, response)

    def _resume_pending_map_tool_calls(self, session: Session) -> ChatToolCallsResponse | None:
        """按既有优先级恢复自动读取后挂起的地图工具调用。"""
        resume_steps: tuple[
            tuple[Callable[[Session], ChatToolCallsResponse | None], str],
            ...,
        ] = (
            (
                _resume_pending_map_tool_after_read,
                "Resumed pending map tool after region read session=%s turn_id=%s count=%d",
            ),
            (
                _resume_pending_map_write_after_read,
                "Resumed pending map write after state read session=%s turn_id=%s count=%d",
            ),
            (
                _resume_pending_map_validation_after_read,
                "Resumed pending map validation after state read session=%s turn_id=%s count=%d",
            ),
        )
        for resume, log_template in resume_steps:
            response = resume(session)
            if response is None:
                continue
            self._emit_tool_call_response(session, response, log_template)
            return response
        return None

    def _emit_tool_call_response(
        self,
        session: Session,
        response: ChatToolCallsResponse,
        log_template: str,
    ) -> None:
        """发送 tool_calls 事件并写入对应日志。"""
        self._emit(
            session.session_id,
            "tool_calls",
            {
                "turn_id": response.turn_id,
                "text": response.text,
                "calls": [call.model_dump(mode="json") for call in response.calls],
                "count": len(response.calls),
            },
        )
        logger.info(
            log_template,
            session.session_id,
            response.turn_id,
            len(response.calls),
        )

    def _security_for_request(self, request: ChatRequest) -> SecuritySettings:
        """基于启动安全边界叠加单次请求的权限模式覆盖。"""
        if request.permission_mode is None:
            return self._base_security
        logger.info(
            "Permission mode overridden session=%s mode=%s",
            request.session_id,
            request.permission_mode,
        )
        return self._base_security.model_copy(update={"permission_mode": request.permission_mode})

    def _model_for_effort(self, effort: str) -> str | None:
        """Return an optional model override for the current effort."""
        value = {
            "quick": self._settings.llm_quick_model,
            "standard": self._settings.llm_standard_model,
            "deep": self._settings.llm_deep_model,
            "verify": self._settings.llm_verify_model,
            "advisor": self._settings.llm_advisor_model,
        }.get(effort)
        if value is not None and str(value).strip() != "":
            return str(value).strip()
        return self._settings.llm_model.strip() or None

    def _thinking_budget_for_effort(self, effort: str) -> int | None:
        """Return an optional thinking budget override for the current effort."""
        return {
            "quick": self._settings.llm_thinking_budget_quick,
            "standard": self._settings.llm_thinking_budget_standard,
            "deep": self._settings.llm_thinking_budget_deep,
            "verify": self._settings.llm_thinking_budget_verify,
            "advisor": self._settings.llm_thinking_budget_advisor,
        }.get(effort)

    async def _enrich_front_image_result(
        self, tool_name: str, result: dict[str, Any], security: SecuritySettings
    ) -> dict[str, Any]:
        """为前端读图类工具结果补充多模态语义描述。"""
        if tool_name not in {"read_image_metadata", "capture_viewport_screenshot"}:
            return result
        enriched = dict(result)
        client = AssetLLMClient(
            AssetLLMConfig(
                enabled=self._settings.asset_understanding_enabled,
                model=self._settings.asset_understanding_model,
                endpoint=self._settings.asset_understanding_endpoint,
                api_key=self._settings.asset_understanding_api_key.get_secret_value(),
                timeout_s=self._settings.asset_understanding_timeout_s,
                max_tokens=self._settings.asset_understanding_max_tokens,
                concurrency=1,
            )
        )
        semantic: dict[str, Any] = {
            "enabled": client.available,
            "model": self._settings.asset_understanding_model,
        }
        if not client.available:
            semantic["skipped"] = "asset_understanding_not_configured"
            enriched["semantic"] = semantic
            return enriched
        image_path = self._resolve_front_image_path(enriched, security)
        if image_path is None:
            semantic["skipped"] = "image_path_not_readable_by_service"
            enriched["semantic"] = semantic
            return enriched
        description = await asyncio.to_thread(client.describe, image_path, "image")
        semantic["source_path"] = str(image_path)
        semantic["description"] = description
        enriched["semantic"] = semantic
        if description:
            enriched["semantic_description"] = description
        return enriched

    def _resolve_front_image_path(
        self, result: dict[str, Any], security: SecuritySettings
    ) -> Path | None:
        """把前端返回的 res/user 路径解析为服务端可读的本地图片路径。"""
        raw_path = str(result.get("path", "")).strip()
        if raw_path.startswith("res://"):
            rel = raw_path.removeprefix("res://").lstrip("/\\")
            return self._resolve_project_image_path(security.project_root / rel, security)
        if raw_path and not raw_path.startswith("user://") and not Path(raw_path).is_absolute():
            return self._resolve_project_image_path(security.project_root / raw_path, security)
        absolute = str(result.get("absolute_path", "")).strip()
        if raw_path.startswith("user://") and absolute:
            return self._resolve_existing_image_path(Path(absolute))
        return None

    def _resolve_project_image_path(
        self, candidate: Path, security: SecuritySettings
    ) -> Path | None:
        """确认项目内图片路径没有越过安全根目录且真实存在。"""
        try:
            resolved_root = security.project_root.resolve()
            resolved_candidate = candidate.resolve()
            resolved_candidate.relative_to(resolved_root)
        except (OSError, ValueError):
            return None
        return self._resolve_existing_image_path(resolved_candidate)

    def _resolve_existing_image_path(self, candidate: Path) -> Path | None:
        """确认图片候选路径存在且是普通文件。"""
        try:
            if candidate.exists() and candidate.is_file():
                return candidate
        except OSError:
            return None
        return None

    async def _append_tool_results(
        self, session: Session, results: list[ToolResult], security: SecuritySettings
    ) -> tuple[ChatErrorResponse | None, list[dict[str, Any]]]:
        """校验并把前端工具结果追加到对应 agent 帧。

        Returns:
            `(error, verify_candidates)`：`error` 非 None 时本次回传被拒绝，
            `verify_candidates` 此时必为空列表；否则 `verify_candidates` 收集
            本次落地、且命中 `verify_trigger_tools` 的编辑类工具调用，供调用方
            驱动 Verify 两阶段校验（§3.3）。
        """
        if session.pending_turn_id is None:
            logger.warning("Tool results rejected: no pending turn session=%s", session.session_id)
            return ChatErrorResponse(text="当前会话没有等待回传的工具调用"), []
        if not results:
            logger.warning("Tool results rejected: empty results session=%s", session.session_id)
            return ChatErrorResponse(text="tool_results 不能为空"), []

        ids = {result.tool_use_id for result in results}
        if ids != session.pending_tool_call_ids:
            expected = ", ".join(sorted(session.pending_tool_call_ids))
            actual = ", ".join(sorted(ids))
            logger.warning(
                "Tool results rejected: id mismatch session=%s expected=%s actual=%s",
                session.session_id,
                expected,
                actual,
            )
            return (
                ChatErrorResponse(
                    text=f"tool_results 与 pending 工具调用不匹配：expected={expected}; actual={actual}"
                ),
                [],
            )
        if any(result.turn_id != session.pending_turn_id for result in results):
            logger.warning(
                "Tool results rejected: turn mismatch session=%s pending_turn=%s",
                session.session_id,
                session.pending_turn_id,
            )
            return ChatErrorResponse(text="tool_results.turn_id 与当前 pending_turn_id 不匹配"), []

        frames = {frame.id: frame for frame in session.agent_stack}
        verify_candidates: list[dict[str, Any]] = []
        for result in results:
            frame = frames.get(result.frame_id)
            if frame is None:
                logger.warning(
                    "Tool results rejected: unknown frame session=%s frame=%s",
                    session.session_id,
                    result.frame_id,
                )
                return ChatErrorResponse(text=f"未知 frame_id：{result.frame_id}"), []
            is_error = result.status in {"rejected", "error"}
            metadata = session.pending_tool_calls.get(result.tool_use_id, {})
            tool_name = str(metadata.get("name", ""))
            tool_args = metadata.get("input", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            tool = REGISTRY.get(tool_name)
            payload: Any
            map_artifact_ref: str | None = None
            if result.status == "applied":
                applied_result = result.result
                if (
                    tool is not None
                    and tool.enrich is not None
                    and isinstance(applied_result, dict)
                ):
                    applied_result = tool.enrich(tool_args, applied_result)
                if isinstance(applied_result, dict):
                    applied_result = await self._enrich_front_image_result(
                        tool_name, applied_result, security
                    )
                    map_artifact_ref = self._store_map_artifact(
                        session.session_id,
                        tool_name,
                        tool_args,
                        applied_result,
                    )
                    _update_map_context_state(
                        session,
                        tool_name,
                        tool_args,
                        applied_result,
                        map_artifact_ref,
                    )
                if result.grant_session_allow and tool is not None:
                    session.session_allow.add(make_session_allow_grant(tool, tool_args))
                    logger.info(
                        "Session allow grant added session=%s tool=%s frame=%s",
                        session.session_id,
                        tool.name,
                        frame.id,
                    )
                artifact_refs = list(result.artifact_refs)
                if map_artifact_ref is not None:
                    artifact_refs.append(map_artifact_ref)
                payload = {
                    "status": result.status,
                    "result": applied_result,
                    "artifact_refs": artifact_refs,
                    "grant_session_allow": result.grant_session_allow,
                }
                if (
                    self._settings.verify_after_edit
                    and tool_name in self._settings.verify_trigger_tools
                ):
                    path = tool_args.get("path") or tool_args.get("target_path")
                    if isinstance(path, str) and path:
                        verify_candidates.append(
                            {
                                "tool_use_id": result.tool_use_id,
                                "frame_id": frame.id,
                                "tool_name": tool_name,
                                "path": path,
                                "input": tool_args,
                            }
                        )
            else:
                payload = {
                    "status": result.status,
                    "error_code": result.error_code,
                    "result": result.result,
                }
            result_for_gate = payload.get("result") if isinstance(payload, dict) else None
            _remember_latest_map_revision(session, tool_args, result_for_gate)
            if tool_name == "describe_map_region":
                _remember_latest_map_region_read(session, tool_args, result_for_gate)
            blocker = _map_completion_blocker(
                tool_name, result.status, result_for_gate, result.error_code
            )
            if tool_name in _MAP_VALIDATION_TOOL_NAMES and isinstance(result_for_gate, dict):
                if result_for_gate.get("completion_allowed") is True:
                    target = str(result_for_gate.get("target", tool_args.get("target_path", "")))
                    revision = result_for_gate.get("map_revision")
                    revision_value = (
                        revision
                        if isinstance(revision, int) and not isinstance(revision, bool)
                        else None
                    )
                    session.map_completion_blockers = _clear_validation_blockers(
                        session.map_completion_blockers,
                        target,
                        revision_value,
                    )
                    if not _has_review_blocker(
                        session.map_completion_blockers,
                        target,
                        revision_value,
                    ):
                        session.map_completion_blockers.append(
                            _review_required_blocker(tool_name, target, revision_value)
                        )
                elif blocker is not None:
                    session.map_completion_blockers = [blocker]
            elif blocker is not None:
                session.map_completion_blockers = [blocker]
            history_payload = (
                _history_payload_for_front_tool(tool_name, payload, map_artifact_ref)
                if isinstance(payload, dict)
                else payload
            )
            frame.messages.append(
                _tool_message(result.tool_use_id, history_payload, is_error=is_error)
            )
            if isinstance(result_for_gate, dict):
                _append_platform_planning_failure_hint(session, tool_name, result_for_gate)
            if (
                tool_name in MAP_REVISION_GUARDED_TOOL_NAMES
                and str(result.error_code) == "map_revision_conflict"
            ):
                _schedule_revision_conflict_reader(
                    session,
                    frame,
                    tool_name,
                    tool_args,
                    result_for_gate,
                )
            # cell_count_mismatch 时自动注入恢复指引，避免 LLM 盲目重试
            if str(result.error_code) == "cell_count_mismatch":
                actual_cells = None
                if isinstance(result_for_gate, dict):
                    actual_cells = result_for_gate.get("actual_cells")
                hint = (
                    "【cell_count_mismatch 恢复指引】\n"
                    "- 计算公式：x=A..B 的列数 = (B - A + 1)，不是 (B - A)\n"
                    "- 示例：x=64..86 是 23 列，y=21..23 是 3 行，总计 23×3=69 格\n"
                )
                if actual_cells is not None:
                    hint += f"- 重试时必须把 expected_cells 设为 {actual_cells}\n"
                hint += "- 禁止用相同参数重试第 3 次，必须切换策略或提前终止\n"
                frame.messages.append(
                    {"role": "user", "content": hint}
                )
            logger.info(
                "Tool result appended session=%s turn_id=%s tool=%s status=%s frame=%s",
                session.session_id,
                result.turn_id,
                tool_name,
                result.status,
                frame.id,
            )

        session.clear_pending()
        logger.info("Tool results completed session=%s count=%d", session.session_id, len(results))
        return None, verify_candidates

    async def _run_verify(
        self,
        session: Session,
        security: SecuritySettings,
        candidates: list[dict[str, Any]],
        model_override: str | None = None,
    ) -> None:
        """对本轮所有命中校验条件的编辑结果运行 VerifyRunner。"""
        await self._verify_runner.run(session, security, candidates, model_override)

    async def _cancel_active_tasks(self, session_id: str) -> bool:
        """取消并等待该会话仍在运行的 `/chat` 任务，返回是否取消了任何任务。

        会话生命周期操作（reset/interrupt）必须先把仍在 await LLM/工具的旧
        turn 真正取消并 await 到它退出，否则旧 turn 之后的 `save(session)` 会
        把已被重置/中断的会话重新写回，造成"会话复活"（§14.2）。排除当前
        协程自身，避免自取消。
        """
        current = asyncio.current_task()
        tasks = {
            task
            for task in self._active_tasks.get(session_id, set())
            if not task.done() and task is not current
        }
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Cancelled task raised after cancel session=%s", session_id)
        return bool(tasks)

    async def reset(self, session_id: str) -> None:
        """清空指定会话。

        先取消该会话仍在运行的 `/chat` 任务并等待其退出，再在持锁状态下清空
        会话；否则旧 turn 返回后的 `save()` 会把已重置的会话重新写回磁盘。
        """
        await self._cancel_active_tasks(session_id)
        async with self._store.lock_for(session_id):
            self._store.reset(session_id)
            if self._recovery is not None:
                self._recovery.clear(session_id)
            self._emit(session_id, "reset", {})
        logger.info("Session reset through QueryEngine session=%s", session_id)

    async def interrupt(self, session_id: str) -> InterruptResponse:
        """真正中断该会话仍在运行的 `/chat` 请求，并丢弃其后续输出。

        前端"停止"按钮此前只是断开自己的 HTTP 连接：后端的 `run_turn`
        循环（自动执行的静默工具，如 grep/read）会继续跑完整轮，并持续把
        新事件写进 `EventStore`。等用户发出下一条消息时，这些属于已停止
        旧任务的事件会被一起拉取并误渲染成新对话的内容。这里改为取消
        该会话当前登记的 `asyncio.Task`，让 `CancelledError` 在下一个
        await 点（LLM 调用/工具执行）处中断循环，并清理任何尚未回传的
        pending 工具调用占位，使会话立刻能接受新消息。

        `_active_tasks[session_id]` 是一个集合而不是单个任务：如果用户在
        前一个请求仍卡在 per-session 锁等待时就又发了一条消息（或快速点了
        多次"停止"），会话上会短暂同时存在多个 `submit_user_turn` 任务。
        只取消其中一个（尤其是若取了最新、可能只是在排队等锁的那个）会让
        真正持锁运行的旧任务永远不会被取消，导致锁一直被占用，包括这次
        interrupt 自己后面要拿的锁也会卡死。所以这里要把所有未完成的都
        取消掉。
        """
        cancelled = await self._cancel_active_tasks(session_id)

        discarded = 0
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            had_pending_plan = session.pending_plan is not None
            session.pending_plan = None
            if session.pending_turn_id is not None:
                frames = {frame.id: frame for frame in session.agent_stack}
                for tool_use_id in sorted(session.pending_tool_call_ids):
                    metadata = session.pending_tool_calls.get(tool_use_id, {})
                    frame = frames.get(str(metadata.get("frame_id", "")))
                    if frame is None:
                        continue
                    frame.messages.append(
                        _tool_message(
                            tool_use_id, "用户中断了当前请求，该工具调用结果未回传。", is_error=True
                        )
                    )
                    discarded += 1
                session.clear_pending()
                self._store.save(session)
                if self._recovery is not None:
                    self._recovery.clear(session_id)
            elif had_pending_plan:
                self._store.save(session)

        self._emit(
            session_id, "turn_interrupted", {"cancelled": cancelled, "pending_discarded": discarded}
        )
        last_seq = self._events.last_seq(session_id) if self._events is not None else 0
        logger.info(
            "Turn interrupted session=%s cancelled=%s pending_discarded=%d last_seq=%d",
            session_id,
            cancelled,
            discarded,
            last_seq,
        )
        return InterruptResponse(ok=True, cancelled=cancelled, last_event_seq=last_seq)

    async def discard_pending(self, session_id: str) -> ChatResponse:
        """放弃当前会话待回传的前端工具调用，保留其余会话历史。

        为每个待回应的 `tool_use_id` 写入一条"用户放弃"的占位 `tool` 消息，
        然后清空 `pending_turn_id`，使会话恢复到可接受新用户消息的状态。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            if session.pending_turn_id is None:
                return ChatErrorResponse(text="当前会话没有等待回传的工具调用")

            frames = {frame.id: frame for frame in session.agent_stack}
            discarded = 0
            for tool_use_id in sorted(session.pending_tool_call_ids):
                metadata = session.pending_tool_calls.get(tool_use_id, {})
                frame = frames.get(str(metadata.get("frame_id", "")))
                if frame is None:
                    continue
                frame.messages.append(
                    _tool_message(tool_use_id, "用户放弃了该工具调用的结果回传。", is_error=True)
                )
                discarded += 1

            session.clear_pending()
            self._store.save(session)
            response = ChatFinalResponse(
                text=f"已放弃 {discarded} 个待回传的工具调用，可以继续发送新消息。"
            )
            self._record_recovery(session, response)
            self._emit(session_id, "pending_discarded", {"count": discarded})
            logger.info("Pending tool calls discarded session=%s count=%d", session_id, discarded)
            return response

    async def set_effort(self, session_id: str, effort: str) -> None:
        """Set session effort without starting a model turn.

        持锁修改：否则会与正在 await LLM 的活跃 turn 抢同一个 Session，导致
        配置在一轮中途被改、响应与上下文错配（§会话锁边界）。
        """
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            session.effort = effort
            self._store.save(session)
        self._emit(session_id, "config_changed", {"effort": effort})
        logger.info("Session effort changed session=%s effort=%s", session_id, effort)

    async def set_output_style(self, session_id: str, output_style: str) -> None:
        """Set session output style without starting a model turn."""
        async with self._store.lock_for(session_id):
            session = self._store.get_or_create(session_id, self.available_tools)
            session.output_style = output_style
            self._store.save(session)
        self._emit(session_id, "config_changed", {"output_style": output_style})
        logger.info(
            "Session output style changed session=%s output_style=%s", session_id, output_style
        )

    def _needs_auto_compact(self, session: Session) -> bool:
        """判断当前会话是否需要自动压缩。"""
        return self._compactor.needs_auto_compact(session)

    async def compact(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """对指定 session 执行本地 micro/full compact，保留 pending 协议完整性。

        持锁入口：手动 `/compact` 命令经此处，先获取会话锁再压缩，避免与正在
        await LLM 的活跃 turn 同时修改 `frame.messages`（§会话锁边界）。自动
        压缩发生在已持锁的 `_submit_locked` 内，必须直接调用 `_compact_locked`，
        否则同一协程再次获取非重入的 `asyncio.Lock` 会死锁。

        Args:
            session_id: 待压缩的会话 id。
            keep_recent: 每帧保留的最近消息数（不含 system prompt）。
            triggered_by: `"manual"`（`/compact` 命令）或 `"auto"`（§16.1 策略 A
                的自动触发），写入 `compact_boundary` 事件 payload，仅用于
                日志/观测区分来源，不影响压缩逻辑本身。
            use_llm: 本次压缩是否用 LLM 语义压缩摘要的 per-request 覆盖；None 时
                沿用服务端 `compact_summary_use_llm` 配置。
        """
        async with self._store.lock_for(session_id):
            return await self._compact_locked_async(session_id, keep_recent, triggered_by, use_llm)

    def _compact_locked(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """同步兼容入口；异步路径请调用 `_compact_locked_async`。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            effective_use_llm = False if use_llm is None else use_llm
            return asyncio.run(
                self._compact_locked_async(session_id, keep_recent, triggered_by, effective_use_llm)
            )
        raise RuntimeError("_compact_locked() cannot run inside an active event loop")

    async def _compact_locked_async(
        self,
        session_id: str,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, Any]:
        """在已持有会话锁时执行压缩；不要在未持锁路径直接调用。"""
        return await self._compactor.compact_locked(session_id, keep_recent, triggered_by, use_llm)

    async def _retrieve_rag_context(self, security: SecuritySettings, user_message: str) -> str:
        """为当前用户提问检索 RAG 上下文（L3 段），在线程池里执行避免阻塞事件循环。

        Args:
            security: 当前请求的安全边界（限定检索范围与索引路径）。
            user_message: 当前用户提问原文。

        Returns:
            组装好的 L3 RAG 上下文文本；无索引/无结果/出错时为空串。
        """
        index = create_codebase_index(self._settings, security)
        return await asyncio.to_thread(build_rag_context, index, user_message)

    def _emit(self, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """记录内部事件；未配置事件存储时返回 0。"""
        log_payload = _event_payload_for_log(payload)
        logger.debug(
            "Event emitted session=%s type=%s payload=%s",
            session_id,
            event_type,
            json.dumps(log_payload, ensure_ascii=False, default=str),
        )
        if event_type in _PERSISTED_HISTORY_EVENT_TYPES:
            session = self._store.get_or_create(session_id, self.available_tools)
            if event_type == "context_usage":
                try:
                    used_tokens = int(payload.get("used_tokens", 0))
                except (TypeError, ValueError):
                    used_tokens = 0
                if used_tokens > 0:
                    session.latest_context_used_tokens = used_tokens
                    if used_tokens >= self._settings.auto_compact_token_threshold:
                        session.force_compact_next_turn = True
            session.record_history_event(event_type, payload)
        if self._events is None:
            return 0
        event = self._events.append(session_id, event_type, payload)
        logger.debug("Event persisted session=%s seq=%d type=%s", session_id, event.seq, event_type)
        return event.seq

    def _record_recovery(self, session: Session, response: ChatResponse) -> None:
        """根据最新响应写入或清理最小恢复指针。"""
        if self._recovery is None:
            return
        if isinstance(response, ChatToolCallsResponse):
            last_seq = self._events.last_seq(session.session_id) if self._events is not None else 0
            self._recovery.write(
                session_id=session.session_id,
                pending_turn_id=response.turn_id,
                last_event_seq=last_seq,
            )
            logger.info(
                "Recovery pointer written session=%s turn_id=%s last_seq=%d",
                session.session_id,
                response.turn_id,
                last_seq,
            )
        elif isinstance(response, ChatFinalResponse):
            self._recovery.clear(session.session_id)
            logger.debug("Recovery pointer cleared after final session=%s", session.session_id)
