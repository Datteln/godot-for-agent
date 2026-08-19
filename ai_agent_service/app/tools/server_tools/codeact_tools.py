"""将统一 CodeAct 协议注册为服务端工具。"""

from __future__ import annotations

from typing import Any

from app.codeact.contracts import CodeActRequest, CodeActToolName
from app.codeact.identity import codeact_call_id, task_execution_id
from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register


def _schema(
    name: CodeActToolName, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    """构造统一工具 schema，并保留任务执行身份字段。"""
    return {
        "name": name.value,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": {
                **properties,
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 600},
            },
            "required": required,
        },
    }


def _role(context: ToolContext) -> str:
    """从可信运行时角色投影协议角色，而非接受模型提供的角色。"""
    if context.workflow_stage is not None:
        return "map"
    if context.agent_role == "coordinator":
        return "coordinator"
    if context.agent_role == "advisor":
        return "advisor"
    if context.agent_role == "scene":
        return "scene"
    return "programming"


def _handler(tool: CodeActToolName):
    """创建固定工具名的异步 Gateway handler。"""

    async def handler(args: dict[str, Any], context: ToolContext) -> dict[str, Any]:
        """把当前工具调用交给注入的 Execution Gateway。"""
        if context.execution_gateway is None:
            return {
                "error_code": "worker_unavailable",
                "message": "Execution Gateway is not configured",
            }
        payload = dict(args)
        payload.pop("task_execution_id", None)
        execution_id = context.task_execution_id or task_execution_id(
            context.session_id,
            context.session_epoch,
            context.agent_role or "root",
        )
        call_id = codeact_call_id(
            execution_id,
            context.tool_call_id or f"direct:{tool.value}",
        )
        timeout = payload.pop("timeout_seconds", 60)
        request = CodeActRequest(
            task_execution_id=execution_id,
            task_id=context.session_id,
            role=_role(context),
            call_id=call_id,
            tool=tool,
            arguments=payload,
            timeout_seconds=timeout,
        )
        return (await context.execution_gateway.execute(request, context)).model_dump(mode="json")

    return handler


def register_codeact_tools() -> None:
    """注册新协议工具；所有持久写入和命令均标记为 server worker 执行。"""
    entries: tuple[tuple[CodeActToolName, str, dict[str, Any], list[str], bool, bool], ...] = (
        (
            CodeActToolName.PROJECT_READ,
            "Read a file, list files, or page a scoped artifact with kind=file/list/map_artifact/delegate_artifact/planning_snapshot.",
            {
                "kind": {"type": "string"},
                "path": {"type": "string"},
                "pattern": {"type": "string"},
                "artifact_ref": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"},
                "field": {"type": "string"},
                "artifact_turn_id": {"type": "string"},
                "artifact_entry_id": {"type": "string"},
                "artifact_fingerprint": {"type": "string"},
            },
            [],
            False,
            False,
        ),
        (
            CodeActToolName.PROJECT_SEARCH,
            "Search with kind=grep (pattern) or kind=codebase (query).",
            {
                "kind": {"type": "string"},
                "pattern": {"type": "string"},
                "query": {"type": "string"},
                "include": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            [],
            False,
            False,
        ),
        (
            CodeActToolName.PROJECT_EDIT,
            "Apply one small old_text/new_text patch through the isolated worker; full replacement requires an explicit override.",
            {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
                "content": {"type": "string"},
                "allow_full_replace": {"type": "boolean"},
            },
            ["path"],
            True,
            False,
        ),
        (
            CodeActToolName.SHELL_RUN,
            "Run an allowlisted argv command in the isolated worker.",
            {"command": {"type": "array", "items": {"type": "string"}}},
            ["command"],
            True,
            True,
        ),
        (
            CodeActToolName.GODOT_HEADLESS,
            "Run Godot headless argv or one temporary GDScript inside the isolated worker.",
            {
                "command": {"type": "array", "items": {"type": "string"}},
                "temporary_script": {"type": "string"},
                "changed_paths": {"type": "array", "items": {"type": "string"}},
            },
            ["command"],
            True,
            True,
        ),
        (
            CodeActToolName.GIT_STATUS,
            "Read Git status at the repository root.",
            {},
            [],
            False,
            False,
        ),
        (CodeActToolName.GIT_DIFF, "Read Git diff at the repository root.", {}, [], False, False),
        (
            CodeActToolName.SKILL_LOAD,
            "Load an allowed Skill.",
            {"name": {"type": "string"}},
            ["name"],
            False,
            False,
        ),
        (
            CodeActToolName.TOOL_SEARCH,
            "Search currently visible unified tool schemas.",
            {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            ["query"],
            False,
            False,
        ),
        (
            CodeActToolName.EDITOR_STATUS,
            "Read the state of the matching local Editor instance.",
            {},
            [],
            False,
            False,
        ),
        (
            CodeActToolName.EDITOR_RELOAD,
            "Request an approved reload of one open clean file.",
            {"path": {"type": "string"}},
            ["path"],
            False,
            False,
        ),
        (
            CodeActToolName.EDITOR_CAPTURE,
            "Capture the current local Editor viewport as an artifact.",
            {},
            [],
            False,
            False,
        ),
        (
            CodeActToolName.EDITOR_RUNTIME,
            "Read untrusted runtime state from the local Editor.",
            {},
            [],
            False,
            False,
        ),
        (
            CodeActToolName.EDITOR_DEBUGGER,
            "Read untrusted debugger errors from the local Editor.",
            {},
            [],
            False,
            False,
        ),
        (
            CodeActToolName.EDITOR_PROFILER,
            "Read an untrusted profiler snapshot from the local Editor.",
            {},
            [],
            False,
            False,
        ),
    )
    for name, description, properties, required, writes, executes in entries:
        deferred = name.value.startswith("godot.editor.")
        register(
            ToolDef(
                name=name.value,
                domain="project" if name.value.startswith("project.") else "core",
                side="server",
                reads_project=not writes,
                writes_project=writes,
                executes_process=executes,
                is_read_only=not writes and not executes,
                schema=_schema(name, description, properties, required),
                handler=_handler(name),
                path_args=["path"] if "path" in properties else [],
                deferred=deferred,
                search_hint=(
                    f"Godot Editor online observation: {description}" if deferred else None
                ),
            )
        )
