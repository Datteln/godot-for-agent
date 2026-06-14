"""工具注册表（§7 工具系统与注册表）。

每个工具携带 `side`（前端/服务端执行）与 effect 元数据
（`reads_project`/`writes_project`/`executes_process`/`uses_network`），
权限闸（`app/permissions/engine.py`）据此决策 `allow`/`ask`/`deny`。
新增能力域/工具只需注册一个 `ToolDef` 并归入某 agent 的 `tools` 列表，
编排层、权限闸与入口零改动（NFR-10）。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.tools.context import ToolContext

logger = logging.getLogger(__name__)

# server 工具的实现：接收本次调用入参与执行上下文，返回 JSON 可序列化结果。
ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[dict[str, Any]]]

# front 工具的服务端增强：把前端返回的结构化结果与本次调用入参合并增强。
ToolEnricher = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


@dataclass
class ToolDef:
    """工具元数据：决定执行位置、风险 effect、权限默认值与 schema。

    Attributes:
        name: 工具名，需在 `REGISTRY` 中唯一，且与 OpenAI function schema
            的 `name` 一致。
        domain: 所属能力域（`program`/`map`/`scene`/`resource`/`project`/`core`），
            权限闸用 `enabled_domains` 校验。
        side: `front` 表示由 Godot 前端执行；`server` 表示由本服务执行。
        reads_project: 是否读取工程文件/编辑器状态。
        writes_project: 是否写工程（默认触发预览确认 + 可撤销）。
        executes_process: 是否运行游戏/测试（触发超时/取消/日志/沙箱）。
        uses_network: 是否产生外联（如 embedding 索引）。
        needs_preview: 是否需要前端预览确认；通常由 `mutating` 派生，
            此处允许显式覆盖。
        timeout_ms: 执行型工具的超时（毫秒）。
        is_read_only: 是否只读；默认 False，工具作者必须显式声明。
        is_concurrency_safe: 是否可与其他并发安全工具并发执行；默认 False。
        deferred: True 时不进入常驻 prompt，留待 ToolSearch 按需发现（M2+）。
        search_hint: `deferred=True` 时的检索关键词/摘要。
        render_kind: 前端预览渲染类型（`diff`/`list`/`run`/`log`/`map` 等）。
        path_args: 入参中表示路径的字段名，供 `path_ok`/`all_paths_ok` 校验。
        schema: OpenAI function calling 的 `function` schema。
        handler: `side="server"` 时的实现；`side="front"` 时为 None。
        enrich: `side="front"` 时对前端结果的服务端增强（如合并文档 prose）。
        permission: 默认权限策略键；当前实现下权限闸完全由 effect 元数据
            与权限模式推导，此字段保留以兼容未来的按工具规则覆盖。
    """

    name: str
    domain: str
    side: Literal["front", "server"]
    reads_project: bool = False
    writes_project: bool = False
    executes_process: bool = False
    uses_network: bool = False
    needs_preview: bool = False
    timeout_ms: int | None = None
    is_read_only: bool = False
    is_concurrency_safe: bool = False
    deferred: bool = False
    search_hint: str | None = None
    render_kind: str | None = None
    path_args: list[str] = field(default_factory=list)
    schema: dict[str, Any] = field(default_factory=dict)
    handler: ToolHandler | None = None
    enrich: ToolEnricher | None = None
    permission: str = "auto"

    @property
    def mutating(self) -> bool:
        """是否为"需确认"的改动型工具：写工程或执行进程。

        Returns:
            `writes_project` 或 `executes_process` 任一为真则返回 True。
        """
        return self.writes_project or self.executes_process


REGISTRY: dict[str, ToolDef] = {}


def register(tool: ToolDef) -> None:
    """把一个工具定义注册进全局工具表。

    Args:
        tool: 待注册的工具定义；`tool.name` 作为注册表键，重复注册会覆盖。
    """
    REGISTRY[tool.name] = tool
    logger.debug(
        "Tool registered name=%s side=%s domain=%s mutating=%s deferred=%s",
        tool.name,
        tool.side,
        tool.domain,
        tool.mutating,
        tool.deferred,
    )


def tools_for(
    effective_tools: list[str],
    active_deferred_tools: set[str] | None = None,
) -> list[dict[str, Any]]:
    """根据某个 agent 的可见工具集合，生成稳定排序的 OpenAI tools 列表。

    `deferred=True` 的工具不会进入返回结果（M2+ 由 ToolSearch 按需发现）；
    按工具名排序以利用 prompt 缓存前缀稳定性（§16.1）。

    Args:
        effective_tools: 已裁剪后的工具名列表（通常来自
            `AgentDefinition.effective_tools`）。
        active_deferred_tools: 本帧通过 `search_tools` 激活的 deferred 工具名。

    Returns:
        形如 `[{"type": "function", "function": {...}}, ...]` 的列表，
        可直接作为 `LLMProvider.chat()` 的 `tools` 参数。
    """
    active = active_deferred_tools or set()
    loaded = []
    for name in effective_tools:
        tool = REGISTRY.get(name)
        if tool is None:
            continue
        if tool.deferred and name not in active:
            continue
        loaded.append(tool)
    logger.debug(
        "Resolved tools for prompt requested=%d active_deferred=%d loaded=%d",
        len(effective_tools),
        len(active),
        len(loaded),
    )
    return [
        {"type": "function", "function": tool.schema}
        for tool in sorted(loaded, key=lambda t: t.name)
    ]
