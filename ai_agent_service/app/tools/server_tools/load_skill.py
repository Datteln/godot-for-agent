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

    skill = ctx.skill_catalog.get(name)
    if skill is None:
        raise ValueError(f"未找到 Skill：{name}")
    if not skill.enabled:
        raise ValueError(f"Skill 未启用：{skill.qualified_name}")

    logger.info(
        "load_skill success session=%s qualified_name=%s source=%s warnings=%d",
        ctx.session_id,
        skill.qualified_name,
        skill.source,
        len(skill.warnings),
    )
    return {
        "qualified_name": skill.qualified_name,
        "name": skill.name,
        "source": skill.source,
        "description": skill.description,
        "when_to_use": skill.when_to_use,
        "content": skill.body,
        "effective_tools": skill.effective_tools,
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
