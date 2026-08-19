"""定义版本化、可验证的 CodeAct 工具协议。"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


PROTOCOL_VERSION = "codeact.v1"
DEFAULT_TIMEOUT_SECONDS = 60


class CodeActToolName(StrEnum):
    """声明网关可调度的稳定工具名称。"""

    PROJECT_READ = "project.read"
    PROJECT_SEARCH = "project.search"
    PROJECT_EDIT = "project.edit"
    SHELL_RUN = "shell.run"
    GODOT_HEADLESS = "godot.headless"
    GIT_STATUS = "git.status"
    GIT_DIFF = "git.diff"
    SKILL_LOAD = "skill.load"
    TOOL_SEARCH = "tool.search"
    EDITOR_STATUS = "godot.editor.status"
    EDITOR_RELOAD = "godot.editor.reload_for_validation"
    EDITOR_CAPTURE = "godot.editor.viewport_capture"
    EDITOR_RUNTIME = "godot.editor.runtime_state"
    EDITOR_DEBUGGER = "godot.editor.debugger_errors"
    EDITOR_PROFILER = "godot.editor.profiler_snapshot"


class CodeActErrorCode(StrEnum):
    """声明可供 agent 稳定处理的执行错误代码。"""

    AUTHORIZATION_DENIED = "authorization_denied"
    INVALID_REQUEST = "invalid_request"
    PATH_REJECTED = "path_rejected"
    WORKER_UNAVAILABLE = "worker_unavailable"
    WORKSPACE_CONFLICT = "workspace_conflict"
    EDITOR_OPEN_CONFLICT = "editor_open_conflict"
    EDITOR_DIRTY_CONFLICT = "editor_dirty_conflict"
    EDITOR_UNAVAILABLE = "editor_unavailable"
    EDITOR_BUSY = "editor_busy"
    EDITOR_CANCELLED = "editor_cancelled"
    PROJECT_MISMATCH = "project_mismatch"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    POLICY_APPROVAL_REQUIRED = "policy_approval_required"
    VALIDATION_UNAVAILABLE = "validation_unavailable"
    FAILED_VALIDATION = "failed_validation"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"


CodeActRole = Literal["programming", "map", "scene", "advisor", "coordinator"]


class CodeActRequest(BaseModel):
    """描述一次绑定任务身份的统一工具调用。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["codeact.v1"] = PROTOCOL_VERSION
    task_execution_id: str = Field(min_length=1, max_length=200)
    task_id: str = Field(min_length=1, max_length=200)
    role: CodeActRole
    call_id: str = Field(min_length=1, max_length=200)
    tool: CodeActToolName
    arguments: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1, le=600)

    @field_validator("task_execution_id", "task_id", "call_id")
    @classmethod
    def reject_blank_identifiers(cls, value: str) -> str:
        """拒绝只含空白字符的任务与调用身份。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier must not be blank")
        return normalized


class CodeActResult(BaseModel):
    """返回给来源 agent 的类型化执行结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["codeact.v1"] = PROTOCOL_VERSION
    task_execution_id: str
    call_id: str
    tool: CodeActToolName
    status: Literal["ok", "error", "unavailable", "cancelled", "approval_required"]
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: CodeActErrorCode | None = None
    message: str | None = None
    artifacts: tuple[str, ...] = ()
    retryable: bool = False

    @classmethod
    def success(
        cls,
        request: CodeActRequest,
        data: dict[str, Any],
        *,
        artifacts: tuple[str, ...] = (),
    ) -> CodeActResult:
        """根据请求构造成功结果。"""
        return cls(
            task_execution_id=request.task_execution_id,
            call_id=request.call_id,
            tool=request.tool,
            status="ok",
            data=data,
            artifacts=artifacts,
        )

    @classmethod
    def failure(
        cls,
        request: CodeActRequest,
        code: CodeActErrorCode,
        message: str,
        *,
        status: Literal["error", "unavailable", "cancelled", "approval_required"] = "error",
        retryable: bool = False,
        data: dict[str, Any] | None = None,
    ) -> CodeActResult:
        """根据请求构造不会抛异常的结构化失败结果。"""
        return cls(
            task_execution_id=request.task_execution_id,
            call_id=request.call_id,
            tool=request.tool,
            status=status,
            data=data or {},
            error_code=code,
            message=message,
            retryable=retryable,
        )
