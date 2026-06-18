"""HTTP DTO（§14 HTTP 接口规格）。

`/chat` 的请求体 `ChatRequest` 与三态响应
(`ChatToolCallsResponse`/`ChatFinalResponse`/`ChatErrorResponse`)
均为结构化 Pydantic 模型，与前端 GDScript 插件的协议契约保持一致。
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

PermissionMode = Literal["default", "plan", "auto_approve", "read_only"]
Effort = Literal["quick", "standard", "deep", "verify", "advisor"]


class Context(BaseModel):
    """前端提供的结构化编辑器上下文（§14）。

    Attributes:
        selection: 当前选中节点/脚本信息。
        scene_tree: 当前场景结构（局部或全量，由前端裁剪）。
        tile_catalog: 合法瓦片清单（map 域工具据此校验）。
        project_files: 与本轮相关的文件清单。
        debugger_errors: 运行时报错列表。
        dotnet_enabled: 前端是否检测到 `.csproj`，决定 C# 工具是否暴露（PRD D2）。
    """

    selection: dict[str, Any] | None = None
    scene_tree: dict[str, Any] | None = None
    tile_catalog: list[Any] | None = None
    project_files: list[Any] | None = None
    debugger_errors: list[Any] | None = None
    diagnostics: list[Any] | None = None
    dotnet_enabled: bool = False


class ToolResult(BaseModel):
    """前端回传的一个工具执行结果（§14）。

    Attributes:
        tool_use_id: 对应的工具调用 id。
        frame_id: 来源帧 id，服务端据此路由回对应 agent 帧。
        turn_id: 本次回传所属的 `turn_id`，用于幂等校验（§14.1）。
        status: 执行结果状态：已落地/被用户拒绝/执行出错。
        result: JSON 值（dict/list/str/...），非裸字符串。
        error_code: 错误码（`status="error"` 时使用）。
        artifact_refs: 落地产物引用（如 `res://` 路径）。
        grant_session_allow: 用户是否选择"总是允许"以升级会话级授权
            （粒度 = tool + domain + path + effect；高风险工具忽略）。
    """

    tool_use_id: str
    frame_id: str
    turn_id: str
    status: Literal["applied", "rejected", "error"]
    result: Any | None = None
    error_code: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    grant_session_allow: bool = False


class VerifyIssue(BaseModel):
    """代码编辑后自动校验发现的一个问题（内部模型，不出现在 HTTP 协议里）。

    Attributes:
        severity: 严重程度。
        file_path: 问题所在文件（相对工程根目录）。
        line: 行号；无法定位时为 None。
        message: 问题描述。
    """

    severity: Literal["error", "warning", "info"]
    file_path: str
    line: int | None = None
    message: str


class VerifyResultDTO(BaseModel):
    """一次校验（Phase 1 语法快检或 Phase 2 语义校验）的结构化结果（内部模型）。

    Attributes:
        passed: 是否通过校验。
        issues: 发现的问题列表；通过时为空。
        summary: 一句话总结，供事件 payload 与 system 消息展示。
    """

    passed: bool
    issues: list[VerifyIssue] = Field(default_factory=list)
    summary: str


class ChatRequest(BaseModel):
    """`POST /chat` 请求体（§14）。

    `user_message` 与 `tool_results` 二选一：前者发起新一轮用户消息，
    后者回传上一轮 front 工具的执行结果。
    """

    session_id: str
    request_id: str | None = None
    user_message: str | None = None
    context: Context | None = None
    language_hint: str | None = None
    engine_version: str | None = None
    permission_mode: PermissionMode | None = None
    effort: Effort | None = None
    output_style: str | None = None
    tool_results: list[ToolResult] | None = None


class FrontToolCallDTO(BaseModel):
    """`tool_calls` 响应中的一项：需前端执行/确认的工具调用（§14）。"""

    id: str
    name: str
    input: dict[str, Any]
    needs_confirm: bool
    frame_id: str
    agent: str
    render_kind: str | None = None


class ChatToolCallsResponse(BaseModel):
    """`/chat` 响应三态之一：本轮产出了需前端执行/确认的工具调用。"""

    type: Literal["tool_calls"] = "tool_calls"
    turn_id: str
    text: str | None = None
    calls: list[FrontToolCallDTO]


class ChatFinalResponse(BaseModel):
    """`/chat` 响应三态之一：本轮已得到最终回复。"""

    type: Literal["final"] = "final"
    text: str


class ChatErrorResponse(BaseModel):
    """`/chat` 响应三态之一：本轮因端点/鉴权/限流等原因失败（§17）。"""

    type: Literal["error"] = "error"
    text: str


ChatResponse = Annotated[
    ChatToolCallsResponse | ChatFinalResponse | ChatErrorResponse,
    Field(discriminator="type"),
]


class ResetRequest(BaseModel):
    """`POST /reset` 请求体（§14）。"""

    session_id: str


class InterruptResponse(BaseModel):
    """`POST /chat/interrupt` 响应：是否真正取消了正在执行的请求，以及
    该会话此刻的最新事件序号，供前端跳过中断前后产生的过期事件。
    """

    ok: bool = True
    cancelled: bool
    last_event_seq: int


class ResetResponse(BaseModel):
    """`POST /reset` 响应：确认会话已清空。"""

    ok: bool
    session_id: str


class HealthResponse(BaseModel):
    """`GET /health` 响应（§14）。"""

    ok: bool
    model: str
    endpoint_reachable: bool | None = None
    function_calling_supported: bool | None = None


class DoctorResponse(BaseModel):
    """`GET /doctor` 响应（M0：基础自检，§18.3）。

    Attributes:
        python_version: 运行该服务的 Python 版本。
        auth_enabled: 是否启用了一次性 token 鉴权（§9.0）。
        project_root: 当前工程根目录（绝对路径字符串）。
        permission_mode: 服务启动时的默认权限模式。
        trusted_project: 工程是否被标记为受信任。
        enabled_domains: 当前启用的工具域。
        registered_tools: 已注册的工具名列表（按名称排序）。
        llm_base_url_configured: 是否已配置非默认的 LLM 端点
            （不返回具体 URL/key，避免泄露）。
        llm_model: 默认模型名。
        session_store_dir: 会话本地持久化目录。
        warnings: 自检过程中产生的告警（如配置缺失）。
    """

    python_version: str
    auth_enabled: bool
    project_root: str
    permission_mode: PermissionMode
    trusted_project: bool
    enabled_domains: list[str]
    registered_tools: list[str]
    skills: list[dict[str, Any]] = Field(default_factory=list)
    output_styles: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    llm_base_url_configured: bool
    llm_model: str
    session_store_dir: str
    warnings: list[str] = Field(default_factory=list)


class ChatEventDTO(BaseModel):
    """`GET /chat/events` 返回的一条事件。"""

    seq: int
    session_id: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatEventsResponse(BaseModel):
    """`GET /chat/events` 响应。"""

    events: list[ChatEventDTO]


class SessionHistoryItemDTO(BaseModel):
    """A frontend-renderable session history item."""

    role: Literal["user", "assistant", "system", "error"]
    text: str
    frame_id: str | None = None
    agent: str | None = None


class HistoryBlockBase(BaseModel):
    """Structured, frontend-renderable session history block."""

    frame_id: str | None = None
    agent: str | None = None


class UserHistoryBlock(HistoryBlockBase):
    type: Literal["user"] = "user"
    text: str


class ErrorHistoryBlock(HistoryBlockBase):
    type: Literal["error"] = "error"
    text: str


class LogTextHistoryBlock(HistoryBlockBase):
    type: Literal["log_text"] = "log_text"
    text: str
    marker: bool = False
    indent: bool = False


class LogReadHistoryBlock(HistoryBlockBase):
    type: Literal["log_read"] = "log_read"
    path: str
    line_start: int = 1
    line_end: int = 1


class GrepMatchDTO(BaseModel):
    path: str
    line: int | None = None
    text: str = ""


class LogGrepHistoryBlock(HistoryBlockBase):
    type: Literal["log_grep"] = "log_grep"
    pattern: str
    include: str = "project"
    match_count: int = 0
    results: list[GrepMatchDTO] = Field(default_factory=list)
    truncated: bool = False


class LogEditHistoryBlock(HistoryBlockBase):
    type: Literal["log_edit"] = "log_edit"
    path: str
    added: int = 0
    removed: int = 0


class ThoughtHistoryBlock(HistoryBlockBase):
    type: Literal["thought"] = "thought"
    header: str = "Thought"
    detail: str = ""


class PlanStepDTO(BaseModel):
    index: int
    title: str = ""
    agent: str = ""
    task: str = ""


class PlanCreatedHistoryBlock(HistoryBlockBase):
    type: Literal["plan_created"] = "plan_created"
    summary: str = ""
    steps: list[PlanStepDTO] = Field(default_factory=list)


class StepStartedHistoryBlock(HistoryBlockBase):
    type: Literal["step_started"] = "step_started"
    index: int
    total: int
    title: str = ""


class StepCompletedHistoryBlock(HistoryBlockBase):
    type: Literal["step_completed"] = "step_completed"
    index: int
    total: int
    summary: str = ""


class VerifyStartedHistoryBlock(HistoryBlockBase):
    type: Literal["verify_started"] = "verify_started"
    file_path: str
    phase: str = ""


class VerifyPassedHistoryBlock(HistoryBlockBase):
    type: Literal["verify_passed"] = "verify_passed"
    file_path: str
    summary: str = ""


class VerifyFailedHistoryBlock(HistoryBlockBase):
    type: Literal["verify_failed"] = "verify_failed"
    file_path: str
    issues_count: int = 0
    summary: str = ""


class DelegateResultDTO(BaseModel):
    agent: str = ""
    summary: str = ""


class DelegateResultsHistoryBlock(HistoryBlockBase):
    type: Literal["delegate_results"] = "delegate_results"
    results: list[DelegateResultDTO] = Field(default_factory=list)


class DelegateResultHistoryBlock(HistoryBlockBase):
    type: Literal["delegate_result"] = "delegate_result"
    summary: str = ""


class SystemTextHistoryBlock(HistoryBlockBase):
    type: Literal["system_text"] = "system_text"
    text: str


SessionHistoryBlock = Annotated[
    UserHistoryBlock
    | ErrorHistoryBlock
    | LogTextHistoryBlock
    | LogReadHistoryBlock
    | LogGrepHistoryBlock
    | LogEditHistoryBlock
    | ThoughtHistoryBlock
    | PlanCreatedHistoryBlock
    | StepStartedHistoryBlock
    | StepCompletedHistoryBlock
    | VerifyStartedHistoryBlock
    | VerifyPassedHistoryBlock
    | VerifyFailedHistoryBlock
    | DelegateResultsHistoryBlock
    | DelegateResultHistoryBlock
    | SystemTextHistoryBlock,
    Field(discriminator="type"),
]


class SessionHistoryResponse(BaseModel):
    """`GET /sessions/{session_id}/history` response."""

    ok: bool = True
    session_id: str
    pending_turn_id: str | None = None
    items: list[SessionHistoryItemDTO] = Field(default_factory=list)
    blocks: list[SessionHistoryBlock] = Field(default_factory=list)


class RecoveryPointerDTO(BaseModel):
    """最小恢复指针（§14.3），不包含 token/API key/完整消息。"""

    session_id: str
    last_event_seq: int
    pending_turn_id: str | None = None
    project_hash: str
    updated_at: str


class RecoveryPointerResponse(BaseModel):
    """`GET /recovery-pointer` 响应。"""

    exists: bool
    pointer: RecoveryPointerDTO | None = None


class CommandInfo(BaseModel):
    """命令面板可展示的服务端命令摘要。"""

    name: str
    description: str
    args_schema: dict[str, Any] = Field(default_factory=dict)


class CommandRequest(BaseModel):
    """`POST /commands/{name}` 请求体。"""

    session_id: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class CommandResponse(BaseModel):
    """`POST /commands/{name}` 响应。"""

    ok: bool
    text: str
    result: Any | None = None


class SkillsResponse(BaseModel):
    """`GET /skills` response."""

    skills: list[dict[str, Any]] = Field(default_factory=list)


class OutputStylesResponse(BaseModel):
    """`GET /output-styles` response."""

    output_styles: list[dict[str, Any]] = Field(default_factory=list)


class MemoryItemDTO(BaseModel):
    """Memory item DTO."""

    id: str
    text: str
    scope: str
    tags: list[str] = Field(default_factory=list)
    created_at: float
    updated_at: float


class MemoryResponse(BaseModel):
    """`GET/POST /memory` response."""

    ok: bool = True
    items: list[MemoryItemDTO] = Field(default_factory=list)
    text: str | None = None


class MemoryRequest(BaseModel):
    """`POST /memory` request."""

    action: Literal["save", "delete", "clear", "list"]
    text: str | None = None
    id: str | None = None
    tags: list[str] = Field(default_factory=list)
    scope: str = "project"
