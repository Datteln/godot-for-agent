from __future__ import annotations

from pathlib import Path

from app.agents.loader import load_agent_file
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY


def _agent_defs_dir() -> Path:
    return Path(__file__).parents[1] / "app" / "agents" / "agent_defs"


def test_scene_graph_tools_are_registered_as_confirmed_mutations() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        for name, required in (
            ("instance_scene", ["scene_path"]),
            ("duplicate_node", ["path"]),
            ("connect_signal", ["path", "signal", "target_path", "method"]),
            ("disconnect_signal", ["path", "signal", "target_path", "method"]),
            ("add_to_group", ["path", "group"]),
            ("remove_from_group", ["path", "group"]),
            ("save_scene", []),
        ):
            tool = REGISTRY[name]
            assert tool.side == "front"
            assert tool.domain == "scene"
            assert tool.writes_project is True
            assert tool.needs_preview is True
            assert tool.mutating is True
            assert tool.schema["parameters"]["required"] == required
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_scene_graph_read_only_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        for name in ("list_node_groups", "list_node_signals", "list_node_methods", "list_open_scenes"):
            tool = REGISTRY[name]
            assert tool.side == "front"
            assert tool.domain == "scene"
            assert tool.is_read_only is True
            assert tool.mutating is False

        screenshot = REGISTRY["capture_viewport_screenshot"]
        assert screenshot.domain == "scene"
        assert screenshot.is_read_only is True
        assert screenshot.mutating is False
        assert screenshot.write_path_args == ["output_path"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_project_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        read_setting = REGISTRY["read_project_setting"]
        assert read_setting.domain == "project"
        assert read_setting.is_read_only is True

        autoloads = REGISTRY["list_autoloads"]
        assert autoloads.domain == "project"
        assert autoloads.is_read_only is True

        for name, required in (
            ("add_autoload", ["name", "path"]),
            ("remove_autoload", ["name"]),
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


def test_resource_read_write_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        read_resource = REGISTRY["read_resource"]
        assert read_resource.domain == "resource"
        assert read_resource.is_read_only is True
        assert read_resource.mutating is False

        set_resource_property = REGISTRY["set_resource_property"]
        assert set_resource_property.domain == "resource"
        assert set_resource_property.writes_project is True
        assert set_resource_property.needs_preview is True
        assert set_resource_property.mutating is True
        assert set_resource_property.schema["parameters"]["required"] == ["path", "property", "value"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_git_read_tools_are_registered_as_non_mutating() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        for name in ("git_status", "git_diff"):
            tool = REGISTRY[name]
            assert tool.side == "front"
            assert tool.domain == "program"
            assert tool.is_read_only is True
            assert tool.mutating is False
            assert tool.render_kind == "run"
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_scene_agent_lists_new_scene_and_project_tools() -> None:
    agent = load_agent_file(_agent_defs_dir() / "scene-agent.md")
    for name in (
        "instance_scene",
        "duplicate_node",
        "connect_signal",
        "disconnect_signal",
        "add_to_group",
        "remove_from_group",
        "list_node_groups",
        "list_node_signals",
        "list_node_methods",
        "save_scene",
        "list_open_scenes",
        "capture_viewport_screenshot",
        "read_project_setting",
        "list_autoloads",
        "add_autoload",
        "remove_autoload",
    ):
        assert name in agent.tools


def test_resource_agent_lists_read_resource_tools() -> None:
    agent = load_agent_file(_agent_defs_dir() / "resource-agent.md")
    assert "read_resource" in agent.tools
    assert "set_resource_property" in agent.tools


def test_programming_agent_lists_git_read_tools() -> None:
    agent = load_agent_file(_agent_defs_dir() / "programming-agent.md")
    assert "git_status" in agent.tools
    assert "git_diff" in agent.tools


def test_map_agent_lists_screenshot_tool() -> None:
    agent = load_agent_file(_agent_defs_dir() / "map-agent.md")
    assert "capture_viewport_screenshot" in agent.tools
