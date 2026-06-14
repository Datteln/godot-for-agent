"""QueryEngine 门面（§13）：HTTP 层与 query_loop 内核之间的会话协调层。

`QueryEngine` 负责：
- 会话锁与本地持久化；
- 用户消息、前端工具结果与 agent 帧消息的转换；
- `request_id` 幂等缓存；
- 当前请求权限模式覆盖；
- 调用 `orchestrator.agent.run_turn()` 并转换为 HTTP DTO。
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition, Frame
from app.api.schemas import (
    ChatErrorResponse,
    ChatFinalResponse,
    ChatRequest,
    ChatResponse,
    ChatToolCallsResponse,
    FrontToolCallDTO,
    ToolResult,
)
from app.config import AppSettings
from app.llm.provider import LLMProvider
from app.orchestrator.agent import ErrorResult, FinalResult, StepResult, ToolCallsResult, run_turn
from app.output_styles.catalog import OutputStyleCatalog
from app.permissions.engine import make_session_allow_grant
from app.prompt.builder import build_system_prompt
from app.security.settings import SecuritySettings, security_settings_from_app
from app.sessions.store import Session, SessionStore
from app.skills.catalog import SkillCatalog
from app.events.store import EventStore
from app.recovery.pointer import RecoveryPointerStore
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY

logger = logging.getLogger(__name__)


def _response_from_dict(data: dict[str, Any]) -> ChatResponse:
    """把幂等缓存中的响应字典恢复为具体 DTO。"""
    response_type = data.get("type")
    if response_type == "tool_calls":
        return ChatToolCallsResponse.model_validate(data)
    if response_type == "final":
        return ChatFinalResponse.model_validate(data)
    return ChatErrorResponse.model_validate(data)


def _response_to_dict(response: ChatResponse) -> dict[str, Any]:
    """把三态响应序列化为幂等缓存可存的 dict。"""
    return response.model_dump()


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


def _tool_message(tool_call_id: str, result: Any, *, is_error: bool = False) -> dict[str, Any]:
    """构造 OpenAI `role=tool` 消息。"""
    body: Any = {"error": result} if is_error else result
    content = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _build_user_content(request: ChatRequest) -> str:
    """把用户消息与前端上下文打包为稳定、可审计的 user message。"""
    assert request.user_message is not None
    context_payload: dict[str, Any] = {}
    if request.context is not None:
        context_payload["context"] = request.context.model_dump(exclude_none=True)
    if request.language_hint is not None:
        context_payload["language_hint"] = request.language_hint
    if request.engine_version is not None:
        context_payload["engine_version"] = request.engine_version
    if request.effort is not None:
        context_payload["effort"] = request.effort
    if request.output_style is not None:
        context_payload["output_style"] = request.output_style

    if not context_payload:
        return request.user_message
    return (
        request.user_message
        + "\n\n[editor_context]\n"
        + json.dumps(context_payload, ensure_ascii=False, sort_keys=True)
    )


def _brief_message(message: dict[str, Any]) -> str:
    """把一条历史 message 压成可读摘要行。"""
    role = str(message.get("role", "unknown"))
    if role == "assistant" and message.get("tool_calls"):
        names: list[str] = []
        for call in message.get("tool_calls", []):
            if isinstance(call, dict):
                function = call.get("function", {})
                if isinstance(function, dict):
                    names.append(str(function.get("name", "unknown")))
        return f"assistant 调用了工具：{', '.join(names) if names else 'unknown'}"
    content = str(message.get("content", ""))
    compact = " ".join(content.split())
    if len(compact) > 360:
        compact = compact[:360] + "..."
    return f"{role}: {compact}"


def _pending_anchor_index(frame: Frame, pending_ids: set[str]) -> int | None:
    """找到包含 pending tool_call 的 assistant 消息位置。"""
    if not pending_ids:
        return None
    for index, message in enumerate(frame.messages):
        calls = message.get("tool_calls", [])
        if not isinstance(calls, list):
            continue
        for call in calls:
            if isinstance(call, dict) and str(call.get("id", "")) in pending_ids:
                return index
    return None


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
    ) -> None:
        """构造 QueryEngine。

        Args:
            settings: 服务配置。
            session_store: 会话持久化存储。
            llm: 大模型 provider。
            base_security: 启动时解析出的安全边界；缺省时从 settings 构造。
        """
        self._settings = settings
        self._store = session_store
        self._llm = llm
        self._base_security = base_security or security_settings_from_app(settings)
        self._skill_catalog = skill_catalog
        self._output_styles = output_style_catalog
        self._events = event_store
        self._recovery = recovery_store

    @property
    def available_tools(self) -> set[str]:
        """当前工具注册表里的可见工具名集合。"""
        return set(REGISTRY)

    async def submit_user_turn(self, request: ChatRequest) -> ChatResponse:
        """处理一次 `/chat` 请求。

        `user_message` 发起新用户轮次；`tool_results` 回填上一轮 front 工具结果。
        两者不可同时出现，且会话有 pending 工具结果时拒绝新用户消息。
        """
        async with self._store.lock_for(request.session_id):
            session = self._store.get_or_create(request.session_id, self.available_tools)
            logger.info(
                "Chat request accepted session=%s request_id=%s has_user=%s tool_results=%d",
                request.session_id,
                request.request_id,
                request.user_message is not None,
                len(request.tool_results or []),
            )

            if request.request_id is not None and request.request_id in session.request_id_cache:
                logger.info(
                    "Chat idempotency hit session=%s request_id=%s",
                    request.session_id,
                    request.request_id,
                )
                return _response_from_dict(session.request_id_cache[request.request_id])

            response = await self._submit_locked(session, request)

            if request.request_id is not None:
                session.request_id_cache[request.request_id] = _response_to_dict(response)
            self._store.save(session)
            self._record_recovery(session, response)
            logger.info(
                "Chat request completed session=%s response_type=%s pending=%s",
                request.session_id,
                response.type,
                session.pending_turn_id is not None,
            )
            return response

    async def _submit_locked(self, session: Session, request: ChatRequest) -> ChatResponse:
        """在持有会话锁时执行一次请求。"""
        has_user = request.user_message is not None
        has_results = bool(request.tool_results)
        if has_user == has_results:
            logger.warning(
                "Invalid chat request shape session=%s has_user=%s has_results=%s",
                session.session_id,
                has_user,
                has_results,
            )
            return ChatErrorResponse(text="user_message 与 tool_results 必须二选一")

        security = self._security_for_request(request)
        if request.effort is not None:
            session.effort = request.effort
            logger.info("Session effort overridden session=%s effort=%s", session.session_id, request.effort)
        if request.output_style is not None:
            session.output_style = request.output_style
            logger.info(
                "Session output style overridden session=%s output_style=%s",
                session.session_id,
                request.output_style,
            )

        coordinator = get_agent("coordinator", self.available_tools)
        prompt = build_system_prompt(
            coordinator,
            self._skill_catalog,
            self._output_styles,
            session.output_style,
        )
        coordinator = replace(coordinator, prompt=prompt)
        session.ensure_root_frame(coordinator)
        root = session.agent_stack[0]
        root.agent = coordinator
        if root.messages and root.messages[0].get("role") == "system":
            root.messages[0]["content"] = prompt

        if has_results:
            self._emit(session.session_id, "tool_results_received", {"count": len(request.tool_results or [])})
            logger.info(
                "Appending front tool results session=%s count=%d pending_turn=%s",
                session.session_id,
                len(request.tool_results or []),
                session.pending_turn_id,
            )
            result_error = self._append_tool_results(session, request.tool_results or [])
            if result_error is not None:
                logger.warning("Front tool result rejected session=%s reason=%s", session.session_id, result_error.text)
                return result_error
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
                logger.error("User message rejected because session has no active frame session=%s", session.session_id)
                return ChatErrorResponse(text="会话没有活跃的 agent 帧")
            frame.messages.append({"role": "user", "content": _build_user_content(request)})
            self._emit(session.session_id, "user_submitted", {"has_context": request.context is not None})
            logger.info(
                "User turn appended session=%s has_context=%s language_hint=%s",
                session.session_id,
                request.context is not None,
                request.language_hint,
            )

        step = await run_turn(
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
            agent_prompt_factory=lambda agent: build_system_prompt(
                agent,
                self._skill_catalog,
                self._output_styles,
                session.output_style,
            ),
        )
        response = _step_to_response(step)
        if isinstance(response, ChatToolCallsResponse):
            self._emit(
                session.session_id,
                "tool_calls",
                {"turn_id": response.turn_id, "count": len(response.calls)},
            )
            logger.info(
                "Chat produced front tool calls session=%s turn_id=%s count=%d",
                session.session_id,
                response.turn_id,
                len(response.calls),
            )
        elif isinstance(response, ChatFinalResponse):
            self._emit(session.session_id, "final", {"text_length": len(response.text)})
            logger.info(
                "Chat produced final response session=%s text_length=%d",
                session.session_id,
                len(response.text),
            )
        else:
            self._emit(session.session_id, "error", {"text": response.text})
            logger.warning("Chat produced error response session=%s text=%s", session.session_id, response.text)
        return response

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

    def _append_tool_results(
        self, session: Session, results: list[ToolResult]
    ) -> ChatErrorResponse | None:
        """校验并把前端工具结果追加到对应 agent 帧。"""
        if session.pending_turn_id is None:
            logger.warning("Tool results rejected: no pending turn session=%s", session.session_id)
            return ChatErrorResponse(text="当前会话没有等待回传的工具调用")
        if not results:
            logger.warning("Tool results rejected: empty results session=%s", session.session_id)
            return ChatErrorResponse(text="tool_results 不能为空")

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
            return ChatErrorResponse(text=f"tool_results 与 pending 工具调用不匹配：expected={expected}; actual={actual}")
        if any(result.turn_id != session.pending_turn_id for result in results):
            logger.warning(
                "Tool results rejected: turn mismatch session=%s pending_turn=%s",
                session.session_id,
                session.pending_turn_id,
            )
            return ChatErrorResponse(text="tool_results.turn_id 与当前 pending_turn_id 不匹配")

        frames = {frame.id: frame for frame in session.agent_stack}
        for result in results:
            frame = frames.get(result.frame_id)
            if frame is None:
                logger.warning(
                    "Tool results rejected: unknown frame session=%s frame=%s",
                    session.session_id,
                    result.frame_id,
                )
                return ChatErrorResponse(text=f"未知 frame_id：{result.frame_id}")
            is_error = result.status in {"rejected", "error"}
            metadata = session.pending_tool_calls.get(result.tool_use_id, {})
            tool_name = str(metadata.get("name", ""))
            tool_args = metadata.get("input", {})
            if not isinstance(tool_args, dict):
                tool_args = {}
            tool = REGISTRY.get(tool_name)
            payload: Any
            if result.status == "applied":
                applied_result = result.result
                if tool is not None and tool.enrich is not None and isinstance(applied_result, dict):
                    applied_result = tool.enrich(tool_args, applied_result)
                if result.grant_session_allow and tool is not None and not tool.executes_process:
                    session.session_allow.add(make_session_allow_grant(tool, tool_args))
                    logger.info(
                        "Session allow grant added session=%s tool=%s frame=%s",
                        session.session_id,
                        tool.name,
                        frame.id,
                    )
                payload = {
                    "status": result.status,
                    "result": applied_result,
                    "artifact_refs": result.artifact_refs,
                    "grant_session_allow": result.grant_session_allow,
                }
            else:
                payload = {
                    "status": result.status,
                    "error_code": result.error_code,
                    "result": result.result,
                }
            frame.messages.append(_tool_message(result.tool_use_id, payload, is_error=is_error))
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
        return None

    def reset(self, session_id: str) -> None:
        """清空指定会话。"""
        self._store.reset(session_id)
        if self._recovery is not None:
            self._recovery.clear()
        self._emit(session_id, "reset", {})
        logger.info("Session reset through QueryEngine session=%s", session_id)

    def set_effort(self, session_id: str, effort: str) -> None:
        """Set session effort without starting a model turn."""
        session = self._store.get_or_create(session_id, self.available_tools)
        session.effort = effort
        self._store.save(session)
        self._emit(session_id, "config_changed", {"effort": effort})
        logger.info("Session effort changed session=%s effort=%s", session_id, effort)

    def set_output_style(self, session_id: str, output_style: str) -> None:
        """Set session output style without starting a model turn."""
        session = self._store.get_or_create(session_id, self.available_tools)
        session.output_style = output_style
        self._store.save(session)
        self._emit(session_id, "config_changed", {"output_style": output_style})
        logger.info("Session output style changed session=%s output_style=%s", session_id, output_style)

    def compact(self, session_id: str, keep_recent: int = 12) -> dict[str, Any]:
        """对指定 session 执行本地 micro/full compact，保留 pending 协议完整性。"""
        session = self._store.get_or_create(session_id, self.available_tools)
        logger.info("Compacting session session=%s keep_recent=%d", session_id, keep_recent)
        compacted_frames = 0
        removed_messages = 0
        keep = max(6, keep_recent)

        for frame in session.agent_stack:
            if len(frame.messages) <= keep + 2:
                continue
            anchor = _pending_anchor_index(frame, session.pending_tool_call_ids)
            default_start = max(1, len(frame.messages) - keep)
            keep_from = min(default_start, anchor) if anchor is not None else default_start
            if keep_from <= 1:
                continue

            old_messages = frame.messages[1:keep_from]
            summary_lines = [_brief_message(message) for message in old_messages]
            summary = (
                "[compact_summary]\n"
                "以下是较早上下文的本地摘要；写文件或执行高风险操作前仍需重新读取事实。\n"
                + "\n".join(f"- {line}" for line in summary_lines)
            )
            frame.messages = [
                frame.messages[0],
                {"role": "system", "content": summary},
                *frame.messages[keep_from:],
            ]
            compacted_frames += 1
            removed_messages += len(old_messages)

        self._store.save(session)
        seq = self._emit(
            session_id,
            "compact_boundary",
            {
                "compacted_frames": compacted_frames,
                "removed_messages": removed_messages,
                "keep_recent": keep,
                "pending_preserved": session.pending_turn_id is not None,
            },
        )
        logger.info(
            "Compacted session session=%s frames=%d removed_messages=%d pending_preserved=%s",
            session_id,
            compacted_frames,
            removed_messages,
            session.pending_turn_id is not None,
        )
        return {
            "session_id": session_id,
            "compacted_frames": compacted_frames,
            "removed_messages": removed_messages,
            "last_event_seq": seq,
            "pending_turn_id": session.pending_turn_id,
        }

    def _emit(self, session_id: str, event_type: str, payload: dict[str, Any]) -> int:
        """记录内部事件；未配置事件存储时返回 0。"""
        if self._events is None:
            return 0
        event = self._events.append(session_id, event_type, payload)
        logger.debug("Event emitted session=%s seq=%d type=%s", session_id, event.seq, event_type)
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
            self._recovery.clear()
            logger.debug("Recovery pointer cleared after final session=%s", session.session_id)
