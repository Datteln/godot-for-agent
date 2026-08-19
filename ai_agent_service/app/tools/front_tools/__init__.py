"""Front-facing Godot tool registry: coordinated registration of all front tools."""

from __future__ import annotations

from app.tools.front_tools.core_tools import register_core_tools
from app.tools.front_tools.program_tools import register_program_tools
from app.tools.front_tools.scene_tools import register_scene_tools
from app.tools.front_tools.project_tools import register_project_tools
from app.tools.front_tools.resource_tools import register_resource_tools
from app.tools.front_tools.map_tools import register_map_tools


def register_front_tools(*, enabled: bool = True) -> None:
    """为显式兼容调用注册旧工具；应用启动始终传入受控特性开关。"""
    if not enabled:
        return
    register_core_tools()
    register_program_tools()
    register_scene_tools()
    register_project_tools()
    register_resource_tools()
    register_map_tools()


__all__ = ["register_front_tools"]
