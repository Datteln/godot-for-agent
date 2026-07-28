"""`load_skill`：按需加载 Skill 正文。"""

from __future__ import annotations

import logging
from typing import Any

from app.tools.context import ToolContext
from app.tools.registry import ToolDef, register

logger = logging.getLogger(__name__)

LOAD_SKILL_SCHEMA: dict[str, Any] = {
    "name": "load_skill",
    "description": "按名称加载一个 Skill 的完整正文；支持 'source:name' 规范名或唯一短名。",
    "parameters": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Skill 名称，例如 bundled:godot-code-reading。"}
        },
        "required": ["name"],
    },
}


async def load_skill_handler(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """读取已发现 Skill 的全文。"""
    name = args.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("name 不能为空")
    if ctx.skill_catalog is None:
        raise ValueError("SkillCatalog 未初始化")

    binding = ctx.skill_catalog.resolve_binding(
        name,
        set(ctx.agent_effective_tools or ctx.effective_tools),
        workflow_stage=ctx.workflow_stage,
        worker_mode=ctx.worker_mode,
        agent_role=ctx.agent_role,
        permitted_tools=set(ctx.effective_tools),
    )
    if binding.status != "resolved" or binding.qualified_name is None:
        raise ValueError(
            "Skill 绑定失败："
            f"status={binding.status}; reasons={','.join(binding.reason_codes)}"
        )
    skill = ctx.skill_catalog.get(binding.qualified_name)
    if skill is None:
        raise ValueError(f"Skill 绑定后无法读取：{binding.qualified_name}")
    visible_tools = list(binding.effective_tools)
    unavailable_tools = sorted(
        set(skill.effective_tools) - set(binding.effective_tools)
    )
    logger.info(
        "load_skill success session=%s qualified_name=%s source=%s "
        "tools=%d unavailable=%d warnings=%d",
        ctx.session_id,
        skill.qualified_name,
        skill.source,
        len(visible_tools),
        len(unavailable_tools),
        len(skill.warnings),
    )
    return {
        "qualified_name": skill.qualified_name,
        "name": skill.name,
        "source": skill.source,
        "description": skill.description,
        "when_to_use": skill.when_to_use,
        "content": skill.body,
        "effective_tools": visible_tools,       # 仅返回当前 Agent 可见的工具子集
        "unavailable_tools": unavailable_tools,  # Skill 需要但当前 Agent 不可用的工具
        "workflow_stage": ctx.workflow_stage,    # 当前工作流阶段，供 Skill 正文感知上下文
        "binding": binding.to_dict(),
        "warnings": skill.warnings,
    }


def register_load_skill_tool() -> None:
    """把 `load_skill` 注册进全局工具表。"""
    register(
        ToolDef(
            name="load_skill",
            domain="core",
            side="server",
            reads_project=False,
            is_read_only=True,
            is_concurrency_safe=True,
            search_hint="按需加载 Skill 正文",
            schema=LOAD_SKILL_SCHEMA,
            handler=load_skill_handler,
        )
    )
