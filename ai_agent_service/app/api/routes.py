"""FastAPI routes for the local AI agent service."""

from __future__ import annotations

import logging

from fastapi import APIRouter

from app.api.schemas import (
    ChatEventDTO,
    ChatEventsResponse,
    ChatRequest,
    ChatResponse,
    CommandInfo,
    CommandRequest,
    CommandResponse,
    DoctorResponse,
    HealthResponse,
    MemoryRequest,
    MemoryResponse,
    MemoryItemDTO,
    OutputStylesResponse,
    RecoveryPointerDTO,
    RecoveryPointerResponse,
    ResetRequest,
    ResetResponse,
    SkillsResponse,
)
from app.config import AppSettings
from app.doctor.checks import run_doctor
from app.events.store import EventStore
from app.llm.provider import LLMProvider
from app.memory.store import MemoryStore
from app.output_styles.catalog import OutputStyleCatalog
from app.query.engine import QueryEngine
from app.rag.index import CodebaseIndex
from app.recovery.pointer import RecoveryPointerStore
from app.security.settings import SecuritySettings
from app.skills.catalog import SkillCatalog

logger = logging.getLogger(__name__)

COMMANDS: list[CommandInfo] = [
    CommandInfo(
        name="doctor",
        description="返回当前服务自检报告。",
        args_schema={"type": "object", "properties": {}},
    ),
    CommandInfo(
        name="rebuild_index",
        description="重建本地 RAG 检索索引。",
        args_schema={
            "type": "object",
            "properties": {
                "include": {"type": "string", "default": "**/*"},
                "max_files": {"type": "integer", "default": 4000},
            },
        },
    ),
    CommandInfo(
        name="compact",
        description="压缩指定 session 的早期上下文，保留 pending 与 agent_stack。",
        args_schema={"type": "object", "properties": {"session_id": {"type": "string"}}},
    ),
    CommandInfo(
        name="set_effort",
        description="设置当前 session 的 effort 档位。",
        args_schema={
            "type": "object",
            "properties": {
                "effort": {"type": "string", "enum": ["quick", "standard", "deep", "verify", "advisor"]}
            },
            "required": ["effort"],
        },
    ),
    CommandInfo(
        name="set_output_style",
        description="设置当前 session 的 OutputStyle。",
        args_schema={
            "type": "object",
            "properties": {"output_style": {"type": "string"}},
            "required": ["output_style"],
        },
    ),
    CommandInfo(
        name="refresh_extensions",
        description="重新扫描 Skill 与 OutputStyle 目录。",
        args_schema={"type": "object", "properties": {}},
    ),
]


def create_router(
    *,
    settings: AppSettings,
    security: SecuritySettings,
    llm: LLMProvider,
    query_engine: QueryEngine,
    auth_enabled: bool,
    event_store: EventStore,
    recovery_store: RecoveryPointerStore,
    skill_catalog: SkillCatalog,
    output_style_catalog: OutputStyleCatalog,
    memory_store: MemoryStore,
) -> APIRouter:
    """创建 HTTP 路由表。"""
    router = APIRouter()

    @router.post("/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        logger.info(
            "HTTP /chat session=%s has_user=%s tool_results=%d",
            request.session_id,
            request.user_message is not None,
            len(request.tool_results or []),
        )
        return await query_engine.submit_user_turn(request)

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        logger.debug("HTTP /health")
        return HealthResponse(
            ok=True,
            model=settings.llm_model,
            endpoint_reachable=None,
            function_calling_supported=llm.supports_tool_calling,
        )

    @router.post("/reset", response_model=ResetResponse)
    async def reset(request: ResetRequest) -> ResetResponse:
        logger.info("HTTP /reset session=%s", request.session_id)
        query_engine.reset(request.session_id)
        return ResetResponse(ok=True, session_id=request.session_id)

    @router.get("/doctor", response_model=DoctorResponse)
    async def doctor() -> DoctorResponse:
        response = run_doctor(
            settings,
            security,
            llm,
            auth_enabled=auth_enabled,
            skill_catalog=skill_catalog,
            output_style_catalog=output_style_catalog,
            memory_store=memory_store,
        )
        logger.info("HTTP /doctor warnings=%d tools=%d", len(response.warnings), len(response.registered_tools))
        return response

    @router.get("/skills", response_model=SkillsResponse)
    async def skills() -> SkillsResponse:
        summaries = skill_catalog.summaries()
        logger.info("HTTP /skills count=%d", len(summaries))
        return SkillsResponse(skills=[summary.__dict__ for summary in summaries])

    @router.get("/output-styles", response_model=OutputStylesResponse)
    async def output_styles() -> OutputStylesResponse:
        summaries = output_style_catalog.summaries()
        logger.info("HTTP /output-styles count=%d", len(summaries))
        return OutputStylesResponse(
            output_styles=[summary.__dict__ for summary in summaries]
        )

    @router.get("/chat/events", response_model=ChatEventsResponse)
    async def chat_events(session_id: str, after: int = 0) -> ChatEventsResponse:
        events = event_store.list_after(session_id, after)
        logger.debug("HTTP /chat/events session=%s after=%d count=%d", session_id, after, len(events))
        return ChatEventsResponse(
            events=[
                ChatEventDTO(
                    seq=event.seq,
                    session_id=event.session_id,
                    type=event.type,
                    payload=event.payload,
                )
                for event in events
            ]
        )

    @router.get("/recovery-pointer", response_model=RecoveryPointerResponse)
    async def recovery_pointer() -> RecoveryPointerResponse:
        pointer = recovery_store.read()
        if pointer is None:
            logger.debug("HTTP /recovery-pointer missing")
            return RecoveryPointerResponse(exists=False)
        logger.info(
            "HTTP /recovery-pointer exists session=%s pending_turn=%s",
            pointer.session_id,
            pointer.pending_turn_id,
        )
        return RecoveryPointerResponse(
            exists=True,
            pointer=RecoveryPointerDTO(
                session_id=pointer.session_id,
                last_event_seq=pointer.last_event_seq,
                pending_turn_id=pointer.pending_turn_id,
                project_hash=pointer.project_hash,
                updated_at=pointer.updated_at,
            ),
        )

    @router.get("/commands", response_model=list[CommandInfo])
    async def commands() -> list[CommandInfo]:
        logger.debug("HTTP /commands count=%d", len(COMMANDS))
        return COMMANDS

    @router.post("/commands/{name}", response_model=CommandResponse)
    async def run_command(name: str, request: CommandRequest) -> CommandResponse:
        logger.info("HTTP /commands/%s session=%s", name, request.session_id)
        if name == "doctor":
            response = run_doctor(
                settings,
                security,
                llm,
                auth_enabled=auth_enabled,
                skill_catalog=skill_catalog,
                output_style_catalog=output_style_catalog,
                memory_store=memory_store,
            )
            logger.info("Command doctor completed warnings=%d", len(response.warnings))
            return CommandResponse(
                ok=True,
                text="doctor completed",
                result=response.model_dump(),
            )
        if name == "rebuild_index":
            include = request.args.get("include", "**/*")
            max_files = request.args.get("max_files", 4000)
            if not isinstance(include, str) or not include:
                logger.warning("Command rebuild_index rejected: invalid include")
                return CommandResponse(ok=False, text="include 必须是非空字符串")
            if not isinstance(max_files, int) or max_files <= 0:
                logger.warning("Command rebuild_index rejected: invalid max_files")
                return CommandResponse(ok=False, text="max_files 必须是正整数")
            result = CodebaseIndex(
                security,
                settings.resolved_rag_index_path(),
            ).build(include=include, max_files=max_files)
            logger.info(
                "Command rebuild_index completed files=%s chunks=%s truncated=%s",
                result.get("files"),
                result.get("chunks"),
                result.get("truncated_files"),
            )
            return CommandResponse(
                ok=True,
                text="RAG 索引已重建",
                result=result,
            )
        if name == "compact":
            if request.session_id is None:
                logger.warning("Command compact rejected: missing session_id")
                return CommandResponse(ok=False, text="compact 需要 session_id")
            keep_recent = request.args.get("keep_recent", 12)
            if not isinstance(keep_recent, int):
                logger.warning("Command compact rejected: invalid keep_recent")
                return CommandResponse(ok=False, text="keep_recent 必须是整数")
            result = query_engine.compact(request.session_id, keep_recent=keep_recent)
            return CommandResponse(ok=True, text="compact 已完成", result=result)
        if name == "set_effort":
            if request.session_id is None:
                logger.warning("Command set_effort rejected: missing session_id")
                return CommandResponse(ok=False, text="set_effort 需要 session_id")
            effort = str(request.args.get("effort", "standard"))
            if effort not in {"quick", "standard", "deep", "verify", "advisor"}:
                logger.warning("Command set_effort rejected: unknown effort=%s", effort)
                return CommandResponse(ok=False, text=f"未知 effort：{effort}")
            query_engine.set_effort(request.session_id, effort)
            return CommandResponse(ok=True, text=f"effort 已设置为 {effort}")
        if name == "set_output_style":
            if request.session_id is None:
                logger.warning("Command set_output_style rejected: missing session_id")
                return CommandResponse(ok=False, text="set_output_style 需要 session_id")
            output_style = str(request.args.get("output_style", "default"))
            if output_style_catalog.get(output_style) is None:
                logger.warning("Command set_output_style rejected: unknown output_style=%s", output_style)
                return CommandResponse(ok=False, text=f"未知 OutputStyle：{output_style}")
            query_engine.set_output_style(request.session_id, output_style)
            return CommandResponse(ok=True, text=f"OutputStyle 已设置为 {output_style}")
        if name == "refresh_extensions":
            skill_catalog.refresh()
            output_style_catalog.refresh()
            logger.info("Command refresh_extensions completed")
            return CommandResponse(ok=True, text="Skill 与 OutputStyle 已刷新")
        logger.warning("Command rejected: unknown name=%s", name)
        return CommandResponse(ok=False, text=f"未知命令：{name}")

    @router.get("/memory", response_model=MemoryResponse)
    async def memory_list() -> MemoryResponse:
        items = memory_store.list()
        logger.info("HTTP /memory list count=%d", len(items))
        return MemoryResponse(
            items=[MemoryItemDTO(**item.__dict__) for item in items]
        )

    @router.post("/memory", response_model=MemoryResponse)
    async def memory_update(request: MemoryRequest) -> MemoryResponse:
        logger.info("HTTP /memory action=%s", request.action)
        if request.action == "list":
            return await memory_list()
        if request.action == "save":
            if request.text is None:
                logger.warning("Memory save rejected: missing text")
                return MemoryResponse(ok=False, text="save 需要 text")
            item = memory_store.save(request.text, tags=request.tags, scope=request.scope)
            logger.info("Memory saved id=%s scope=%s tags=%d", item.id, item.scope, len(item.tags))
            return MemoryResponse(
                text="memory saved",
                items=[MemoryItemDTO(**item.__dict__)],
            )
        if request.action == "delete":
            if request.id is None:
                logger.warning("Memory delete rejected: missing id")
                return MemoryResponse(ok=False, text="delete 需要 id")
            deleted = memory_store.delete(request.id)
            logger.info("Memory delete requested id=%s deleted=%s", request.id, deleted)
            return MemoryResponse(ok=deleted, text="memory deleted" if deleted else "memory not found")
        if request.action == "clear":
            count = memory_store.clear()
            logger.info("Memory cleared count=%d", count)
            return MemoryResponse(text=f"cleared {count} memory item(s)")
        logger.warning("Memory action rejected: unknown action=%s", request.action)
        return MemoryResponse(ok=False, text=f"未知 memory action：{request.action}")

    return router
