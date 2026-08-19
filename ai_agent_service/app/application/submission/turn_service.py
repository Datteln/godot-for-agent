"""One locked user/tool-result turn execution service."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

from app.agents.bundled import get_agent
from app.agents.types import AgentDefinition
from app.api.schemas import (
    ChatErrorResponse,
    ChatFinalResponse,
    ChatRequest,
    ChatResponse,
    ChatToolCallsResponse,
)
from app.application.context_service import SessionContextService
from app.application.map_result_projection import _resume_map_batch_queue
from app.application.model_selection import model_for_effort, thinking_budget_for_effort
from app.application.publication import SubmissionPublisher, SubmissionScope
from app.application.request_scope import (
    _activate_user_request_scope,
    _map_completion_candidate_is_current,
)
from app.application.response_mapping import map_turn_outcome
from app.application.response_policy import _apply_verification_policy
from app.application.submission.tool_result_processor import ToolResultProcessor
from app.config import AppSettings
from app.codeact.gateway import ExecutionGateway
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.cache_observability import CacheMetricsCollector
from app.llm.provider import LLMProvider
from app.orchestrator.completion_gate import completion_gate_text, evaluate_map_completion
from app.orchestrator.map_contracts import MAP_WORKER_TO_RUNTIME_STAGE
from app.orchestrator.map_context import build_map_progress_digest

from app.orchestrator.map_turn import AgentPromptFactory, MapTurnPolicy
from app.orchestrator.map_workflow import (
    consume_map_resume_authorization,
    replace_map_state_field,
)
from app.orchestrator.turn.contracts import TurnOutcome
from app.orchestrator.turn.driver import TurnDriver
from app.output_styles.catalog import OutputStyleCatalog
from app.prompt.builder import LayeredPrompt, build_system_prompt
from app.prompt.context_builder import ContextBuilder
from app.prompt.project_context import build_project_context
from app.query.helpers import (
    _bind_map_validation_to_pending_write,
    _build_user_content,
    _defer_map_tool_for_region_read,
    _defer_map_validation_for_state_read,
    _defer_map_write_for_state_read,
    _has_only_map_review_required,
    _replace_last_assistant_final,
    _resume_pending_map_tool_after_read,
    _resume_pending_map_validation_after_read,
    _resume_pending_map_write_after_read,
    _schedule_map_completion_continuation,
    _schedule_map_reviewer_if_required,
)
from app.query.tool_result_submission import ValidatedToolResultBatch
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.skills.catalog import SkillCatalog
from app.tools.context import ToolContext
from app.verify.runner import VerifyRunner

logger = logging.getLogger(__name__)

_MAP_MAX_AUTO_ITERATIONS = 3


def _normalize_model_override(model: str | None) -> str | None:
    if model is None:
        return None
    normalized = model.strip()
    return normalized or None


class TurnExecutionService:
    """Owns request preparation, TurnDriver invocation, and response projection."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        llm: LLMProvider,
        base_security: SecuritySettings,
        skill_catalog: SkillCatalog | None,
        output_styles: OutputStyleCatalog | None,
        cache_engine: CacheDecisionEngine,
        cache_metrics: CacheMetricsCollector,
        publisher: SubmissionPublisher,
        context_service: SessionContextService,
        tool_results: ToolResultProcessor,
        verify_runner: VerifyRunner,
        available_tools: Callable[[], set[str]],
        execution_gateway: ExecutionGateway | None = None,
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._base_security = base_security
        self._skill_catalog = skill_catalog
        self._output_styles = output_styles
        self._cache_engine = cache_engine
        self._cache_metrics = cache_metrics
        self._publisher = publisher
        self._context_service = context_service
        self._tool_results = tool_results
        self._verify_runner = verify_runner
        self._available_tools = available_tools
        self._execution_gateway = execution_gateway or ExecutionGateway(settings)

    @property
    def available_tools(self) -> set[str]:
        return self._available_tools()

    async def _run_agent_turn(
        self,
        session: Session,
        security: SecuritySettings,
        model_override: str | None,
        agent_prompt_factory: AgentPromptFactory,
        event_callback: Callable[[str, dict[str, Any]], None],
        publication_scope: SubmissionScope | None,
    ) -> TurnOutcome:
        """用当前应用依赖运行一轮 TurnDriver 状态机。"""
        return await TurnDriver(MapTurnPolicy()).run(
            session=session,
            llm=self._llm,
            security=security,
            tool_ctx=ToolContext(
                security=security,
                session_id=session.session_id,
                session_epoch=session.session_epoch,
                skill_catalog=self._skill_catalog,
                rag_index_path=self._settings.resolved_rag_index_path(),
                staged_map_artifact_turn=(
                    publication_scope.map_artifact_turn if publication_scope is not None else None
                ),
                execution_gateway=self._execution_gateway,
                map_task_state=session.map_task_state,
                map_request_scope=session.map_request_scope,
            ),
            max_turns=self._settings.max_turns,
            session_allow=session.session_allow,
            agent_prompt_factory=agent_prompt_factory,
            model_selector=lambda effort: model_for_effort(self._settings, effort),
            model_override=model_override,
            thinking_budget_selector=lambda effort: thinking_budget_for_effort(
                self._settings,
                effort,
            ),
            event_callback=event_callback,
            cache_engine=self._cache_engine,
            cache_metrics=self._cache_metrics,
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
        )

    def _defer_map_tool_calls_if_needed(
        self,
        session: Session,
        response: ChatResponse,
    ) -> ChatResponse:
        """按既有顺序挂起需要先读取地图状态的工具调用。"""
        if not isinstance(response, ChatToolCallsResponse):
            return response
        response = _bind_map_validation_to_pending_write(session, response)
        response = _defer_map_write_for_state_read(session, response)
        response = _defer_map_validation_for_state_read(session, response)
        return _defer_map_tool_for_region_read(session, response)

    def _resume_pending_map_tool_calls(
        self,
        session: Session,
        publication_scope: SubmissionScope | None,
    ) -> ChatToolCallsResponse | None:
        """按既有优先级恢复自动读取后挂起的地图工具调用。"""
        resume_steps: tuple[
            tuple[Callable[[Session], ChatToolCallsResponse | None], str],
            ...,
        ] = (
            (
                _resume_map_batch_queue,
                "Resumed deterministic map batch session=%s turn_id=%s count=%d",
            ),
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
            self._emit_tool_call_response(
                session,
                response,
                log_template,
                publication_scope,
            )
            return response
        return None

    def _emit_tool_call_response(
        self,
        session: Session,
        response: ChatToolCallsResponse,
        log_template: str,
        publication_scope: SubmissionScope | None,
    ) -> None:
        """发送 tool_calls 事件并写入对应日志。"""
        self._publisher.emit(
            session.session_id,
            "tool_calls",
            {
                "turn_id": response.turn_id,
                "text": response.text,
                "calls": [call.model_dump(mode="json") for call in response.calls],
                "count": len(response.calls),
            },
            publication_scope,
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

    async def _run_verify(
        self,
        session: Session,
        security: SecuritySettings,
        candidates: list[dict[str, Any]],
        model_override: str | None = None,
    ) -> None:
        """对本轮所有命中校验条件的编辑结果运行 VerifyRunner。"""
        await self._verify_runner.run(session, security, candidates, model_override)

    async def execute(
        self,
        session: Session,
        request: ChatRequest,
        validated_tool_batch: ValidatedToolResultBatch | None = None,
        publication_scope: SubmissionScope | None = None,
    ) -> ChatResponse:
        """Execute one request while its Session lock is held."""
        return await self._execute_locked(
            session,
            request,
            validated_tool_batch,
            publication_scope,
        )

    async def _execute_locked(
        self,
        session: Session,
        request: ChatRequest,
        validated_tool_batch: ValidatedToolResultBatch | None = None,
        publication_scope: SubmissionScope | None = None,
    ) -> ChatResponse:
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
            return ChatErrorResponse(
                text="user_message 与 tool_results 必须二选一",
                error_code="invalid_request_shape",
            )

        dedicated_resume_authorized = False
        if has_user:
            state = session.map_task_state
            task_lineage = session.map_task_lineage
            lineage_id = (
                str(task_lineage.get("lineage_id", ""))
                or str(session.map_request_scope.lineage_id)
                or state.task_id
            )
            dedicated_resume_authorized = consume_map_resume_authorization(
                state,
                task_id=state.task_id,
                lineage_id=lineage_id,
            )

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
            session.rag_context = await self._context_service.retrieve_rag(
                security,
                request.user_message,
            )

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
            dynamic_context=(session.rag_context or "")
            + build_map_progress_digest(session, project_root=security.project_root),
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

        async def build_child_agent_prompt(agent: AgentDefinition, task: str) -> str:
            """为委派或自动恢复子 Agent 构造按任务检索的分层 system prompt。

            在构造 prompt 前先校验 Skill 绑定完整性：若 Agent 声明了所需 skill
            但目录未配置（missing）或版本不兼容（incompatible），立即抛出异常，
            避免子 Agent 在缺少关键能力的情况下被静默启动。
            """
            # Skill 绑定校验：确保子 Agent 所需的 skill 均已正确绑定
            if self._skill_catalog is None:
                if agent.skills:
                    raise ValueError("Skill 绑定失败：当前 AgentApplication 未配置 SkillCatalog")
            else:
                worker_binding_stage = (
                    MAP_WORKER_TO_RUNTIME_STAGE.get(str(agent.map_stage))
                    if agent.pipeline_kind == "map"
                    else None
                )
                binding = self._skill_catalog.binding_status(
                    agent.skills,
                    set(agent.effective_tools),
                    workflow_stage=(
                        worker_binding_stage
                        if worker_binding_stage is not None
                        else (
                            session.map_task_state.stage if agent.pipeline_kind == "map" else None
                        )
                    ),
                    worker_mode=agent.worker_mode,
                    agent_role=agent.role,
                    permitted_tools=set(agent.effective_tools),
                )
                if binding["missing"] or binding["incompatible"]:
                    raise ValueError(
                        "Skill 绑定失败："
                        + json.dumps(
                            {
                                "missing": binding["missing"],
                                "incompatible": binding["incompatible"],
                            },
                            ensure_ascii=False,
                        )
                    )
            # 按任务文本检索 RAG 上下文，与主 Agent 共享同一套检索管线
            task_rag_context = await self._context_service.retrieve_rag(security, task)
            child_context = ContextBuilder().build(
                stable_prefix=build_system_prompt(
                    agent,
                    self._skill_catalog,
                    self._output_styles,
                    session.output_style,
                ),
                structure_context=project_context,
                dynamic_context=(task_rag_context or "") + build_map_progress_digest(session),
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

        if has_results:
            self._publisher.emit(
                session.session_id,
                "tool_results_received",
                {"count": len(request.tool_results or [])},
                publication_scope,
            )
            logger.info(
                "Appending front tool results session=%s count=%d pending_turn=%s",
                session.session_id,
                len(request.tool_results or []),
                session.pending_turn_id,
            )
            result_error, verify_candidates = await self._tool_results.append(
                session,
                request.tool_results or [],
                security,
                build_child_agent_prompt,
                validated_tool_batch,
                publication_scope,
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
            resumed = self._resume_pending_map_tool_calls(session, publication_scope)
            if resumed is not None:
                return resumed
        else:
            if session.pending_turn_id is not None:
                logger.warning(
                    "User message rejected because tools are pending session=%s pending_turn=%s",
                    session.session_id,
                    session.pending_turn_id,
                )
                return ChatErrorResponse(
                    text="当前会话仍有待回传的工具结果，不能开始新的用户消息",
                    error_code="pending_tool_results",
                )
            request_scope, resumed_existing_map_task = _activate_user_request_scope(
                session,
                request,
                dedicated_resume_authorized=dedicated_resume_authorized,
            )
            frame = session.top_frame()
            if frame is None:
                logger.error(
                    "User message rejected because session has no active frame session=%s",
                    session.session_id,
                )
                return ChatErrorResponse(
                    text="会话没有活跃的 agent 帧",
                    error_code="missing_agent_frame",
                )
            frame.messages.append({"role": "user", "content": _build_user_content(request)})
            user_message_index = len(frame.messages) - 1
            session.pending_verify_candidates.clear()
            if (
                # 显式自然语言续接或 resume_map_task 命令后的首次用户消息：
                # 仅属于该 map-edit lineage 的请求才恢复检查点与批次。
                resumed_existing_map_task
                and session.map_task_state.checkpoint is not None
            ):
                frame.messages.append(
                    {
                        "role": "system",
                        "content": (
                            "Resume the explicitly reactivated map task from its checkpoint. "
                            "Reuse cached facts and executed batches."
                        ),
                    }
                )
                resumed_batch = _resume_map_batch_queue(session)
                if resumed_batch is not None:
                    self._emit_tool_call_response(
                        session,
                        resumed_batch,
                        "Resumed map batch from explicit command session=%s turn_id=%s count=%d",
                        publication_scope,
                    )
                    return resumed_batch
            self._publisher.emit(
                session.session_id,
                "user_submitted",
                {
                    "text": request.user_message or "",
                    "frame_id": frame.id,
                    "message_index": user_message_index,
                    "message_id": f"{frame.id}:{user_message_index}",
                    "has_context": request.context is not None,
                    "request_intent": request_scope.intent,
                    "request_lineage_id": request_scope.lineage_id,
                    "map_task_id": request_scope.map_task_id,
                },
                publication_scope,
            )
            logger.info(
                "User turn appended session=%s has_context=%s language_hint=%s",
                session.session_id,
                request.context is not None,
                request.language_hint,
            )

        # 自动压缩（§16.1 策略 A）：新消息/工具结果已追加完毕、即将驱动 LLM 之前
        # 检查体积——这样下面 TurnDriver.run 实际发出的请求已经是压缩后的大小，而不是
        # "先发一次超大请求，下次才生效"。只在体积越界时才触发，不影响正常大小
        # 会话的行为；阈值用粗估 token 数而非精确计费值，足够判断"是否该收紧"。
        if self._settings.auto_compact_enabled and self._context_service.needs_auto_compact(
            session
        ):
            logger.info(
                "Auto-compact triggered session=%s threshold=%d keep_recent=%d",
                session.session_id,
                self._settings.auto_compact_token_threshold,
                self._settings.auto_compact_keep_recent,
            )
            await self._context_service.compact_locked(
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
            self._publisher.emit(session.session_id, event_type, payload, publication_scope)

        def emit_verify_turn_event(event_type: str, payload: dict[str, Any]) -> None:
            self._publisher.emit(session.session_id, event_type, payload, publication_scope)

        step = await self._run_agent_turn(
            session,
            security,
            model_override,
            build_child_agent_prompt,
            emit_turn_event,
            publication_scope,
        )
        response = map_turn_outcome(step)
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
                    publication_scope,
                )
                response = map_turn_outcome(step)
                response = self._defer_map_tool_calls_if_needed(session, response)
        response = _apply_verification_policy(session, response)
        map_gate_continuations = 0
        # ---- 地图完成门控循环 ----
        # 当 agent 产出 final 响应但仍有 completion_blockers 时，
        # 自动调度 reviewer 或 repair continuation，最多迭代 _MAP_MAX_AUTO_ITERATIONS 次
        while (
            isinstance(response, ChatFinalResponse)
            and _map_completion_candidate_is_current(session)
            and session.map_task_state.completion_blockers
            and session.map_task_state.auto_iterations < _MAP_MAX_AUTO_ITERATIONS
        ):
            scheduled = False
            # 优先尝试调度 reviewer 子 Agent（需要 build_child_agent_prompt 构造独立 prompt）
            if _has_only_map_review_required(session.map_task_state.completion_blockers):
                scheduled = await _schedule_map_reviewer_if_required(
                    session,
                    build_child_agent_prompt,
                )
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
                        len(session.map_task_state.completion_blockers),
                    )
            if not scheduled:
                break
            map_gate_continuations += 1
            replace_map_state_field(
                session.map_task_state,
                "auto_iterations",
                session.map_task_state.auto_iterations + 1,
            )
            step = await self._run_agent_turn(
                session,
                security,
                model_override,
                build_child_agent_prompt,
                emit_verify_turn_event,
                publication_scope,
            )
            response = map_turn_outcome(step)
            response = self._defer_map_tool_calls_if_needed(session, response)
        if isinstance(response, ChatToolCallsResponse):
            self._emit_tool_call_response(
                session,
                response,
                "Chat produced front tool calls session=%s turn_id=%s count=%d",
                publication_scope,
            )
        elif isinstance(response, ChatFinalResponse):
            if _map_completion_candidate_is_current(session):
                gate = evaluate_map_completion(session.map_task_state)
                if not gate.allowed:
                    gated_text = completion_gate_text(gate)
                    _replace_last_assistant_final(session, gated_text)
                    response = ChatFinalResponse(text=gated_text)
                elif session.map_task_state.status == "running":
                    session.map_task_state.complete()
            final_frame = session.top_frame()
            final_message_index = (
                len(final_frame.messages) - 1
                if final_frame is not None and final_frame.messages
                else 0
            )
            final_frame_id = final_frame.id if final_frame is not None else "root"
            self._publisher.emit(
                session.session_id,
                "final",
                {
                    "text": response.text,
                    "text_length": len(response.text),
                    "frame_id": final_frame_id,
                    "message_index": final_message_index,
                    "message_id": f"{final_frame_id}:{final_message_index}",
                },
                publication_scope,
            )
            logger.info(
                "Chat produced final response session=%s text_length=%d",
                session.session_id,
                len(response.text),
            )
        else:
            self._publisher.emit(
                session.session_id,
                "error",
                response.model_dump(mode="json"),
                publication_scope,
            )
            logger.warning(
                "Chat produced error response session=%s text=%s", session.session_id, response.text
            )
        if not isinstance(response, ChatToolCallsResponse):
            await self._execution_gateway.finish_session(
                session.session_id,
                session.session_epoch,
                outcome=(
                    "completed" if isinstance(response, ChatFinalResponse) else "terminal_error"
                ),
                summary=response.model_dump(mode="json"),
            )
        return response
