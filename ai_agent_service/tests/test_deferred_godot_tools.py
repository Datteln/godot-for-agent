from __future__ import annotations

from pathlib import Path

from app.agents.loader import load_agent_file
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY


def _agent_defs_dir() -> Path:
    return Path(__file__).parents[1] / "app" / "agents" / "agent_defs"


def test_group_and_current_scene_lookup_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        list_groups = REGISTRY["list_groups"]
        assert list_groups.domain == "scene"
        assert list_groups.is_read_only is True
        assert list_groups.mutating is False

        current_scene = REGISTRY["get_current_scene_path"]
        assert current_scene.domain == "scene"
        assert current_scene.is_read_only is True
        assert current_scene.mutating is False
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_bake_navigation_mesh_is_registered_as_confirmed_mutation() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["bake_navigation_mesh"]

        assert tool.domain == "scene"
        assert tool.writes_project is True
        assert tool.needs_preview is True
        assert tool.mutating is True
        assert tool.schema["parameters"]["required"] == ["path"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_input_map_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        list_actions = REGISTRY["list_input_actions"]
        assert list_actions.domain == "project"
        assert list_actions.is_read_only is True

        for name, required in (
            ("add_input_action", ["action"]),
            ("remove_input_action", ["action"]),
        ):
            tool = REGISTRY[name]
            assert tool.domain == "project"
            assert tool.writes_project is True
            assert tool.needs_preview is True
            assert tool.mutating is True
            assert tool.schema["parameters"]["required"] == required
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_export_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        list_presets = REGISTRY["list_export_presets"]
        assert list_presets.domain == "project"
        assert list_presets.is_read_only is True
        assert list_presets.mutating is False

        export_tool = REGISTRY["export_project"]
        assert export_tool.domain == "program"
        assert export_tool.side == "front"
        assert export_tool.executes_process is True
        assert export_tool.needs_preview is True
        assert export_tool.mutating is True
        assert export_tool.write_path_args == ["output_path"]
        assert export_tool.schema["parameters"]["required"] == ["preset", "output_path"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_animation_and_shader_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        animation_tool = REGISTRY["create_animation_track"]
        assert animation_tool.domain == "resource"
        assert animation_tool.writes_project is True
        assert animation_tool.needs_preview is True
        assert animation_tool.mutating is True
        assert animation_tool.schema["parameters"]["required"] == [
            "player_path",
            "animation",
            "track_path",
            "keyframes",
        ]

        shader_tool = REGISTRY["create_shader_material"]
        assert shader_tool.domain == "resource"
        assert shader_tool.writes_project is True
        assert shader_tool.needs_preview is True
        assert shader_tool.mutating is True
        assert shader_tool.path_args == ["material_path", "shader_path"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_scene_agent_lists_second_batch_tools() -> None:
    agent = load_agent_file(_agent_defs_dir() / "scene-agent.md")
    for name in (
        "list_groups",
        "get_current_scene_path",
        "bake_navigation_mesh",
        "list_input_actions",
        "add_input_action",
        "remove_input_action",
    ):
        assert name in agent.tools


def test_resource_agent_lists_animation_and_shader_tools() -> None:
    agent = load_agent_file(_agent_defs_dir() / "resource-agent.md")
    assert "create_animation_track" in agent.tools
    assert "create_shader_material" in agent.tools
    assert "read_scene_tree" in agent.tools


def test_programming_agent_lists_export_tools() -> None:
    agent = load_agent_file(_agent_defs_dir() / "programming-agent.md")
    assert "list_export_presets" in agent.tools
    assert "export_project" in agent.tools
