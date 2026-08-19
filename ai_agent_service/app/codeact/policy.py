"""定义 CodeAct 角色可见工具与高风险策略分类。"""

from __future__ import annotations

from app.codeact.contracts import CodeActRole, CodeActToolName


READ_ONLY_TOOLS = frozenset(
    {
        CodeActToolName.PROJECT_READ,
        CodeActToolName.PROJECT_SEARCH,
        CodeActToolName.GIT_STATUS,
        CodeActToolName.GIT_DIFF,
        CodeActToolName.SKILL_LOAD,
        CodeActToolName.TOOL_SEARCH,
        CodeActToolName.EDITOR_STATUS,
        CodeActToolName.EDITOR_CAPTURE,
        CodeActToolName.EDITOR_RUNTIME,
        CodeActToolName.EDITOR_DEBUGGER,
        CodeActToolName.EDITOR_PROFILER,
    }
)
WRITE_TOOLS = frozenset(
    {
        CodeActToolName.PROJECT_EDIT,
        CodeActToolName.SHELL_RUN,
        CodeActToolName.GODOT_HEADLESS,
    }
)
EDITOR_APPROVAL_TOOLS = frozenset({CodeActToolName.EDITOR_RELOAD})

ROLE_TOOLS: dict[CodeActRole, frozenset[CodeActToolName]] = {
    "programming": frozenset((*READ_ONLY_TOOLS, *WRITE_TOOLS, *EDITOR_APPROVAL_TOOLS)),
    "map": frozenset((*READ_ONLY_TOOLS, *WRITE_TOOLS, *EDITOR_APPROVAL_TOOLS)),
    "scene": frozenset((*READ_ONLY_TOOLS, *WRITE_TOOLS, *EDITOR_APPROVAL_TOOLS)),
    "advisor": READ_ONLY_TOOLS,
    "coordinator": READ_ONLY_TOOLS,
}


def tool_visible_to_role(role: CodeActRole, tool: CodeActToolName) -> bool:
    """判断角色是否具有工具协议层面的可见性。"""
    return tool in ROLE_TOOLS[role]


def policy_decision(tool: CodeActToolName, arguments: dict[str, object]) -> str:
    """对高风险命令与 UI 影响返回 allow、ask 或 deny。"""
    if tool in EDITOR_APPROVAL_TOOLS:
        return "ask"
    command = arguments.get("command")
    if not isinstance(command, list):
        return "allow"
    tokens = {str(item).casefold() for item in command}
    if command and str(command[0]).casefold() == "git":
        return "deny"
    if tokens & {"curl", "wget", "invoke-webrequest", "npm", "pip", "pip3", "apt", "apt-get"}:
        return "deny" if tokens & {"curl", "wget", "invoke-webrequest"} else "ask"
    if tokens & {"rm", "del", "rmdir", "remove-item", "reset", "clean", "checkout"}:
        return "deny"
    return "allow"
