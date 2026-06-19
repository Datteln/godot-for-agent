from __future__ import annotations

from pathlib import Path

from app.agents.loader import load_agent_file
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY


def test_node_crud_tools_are_registered() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()

        for name, required in (
            ("delete_node", ["path"]),
            ("reparent_node", ["path", "new_parent_path"]),
            ("rename_node", ["path", "name"]),
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


def test_open_scene_is_registered_as_confirmed_mutating_tool() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["open_scene"]

        assert tool.side == "front"
        assert tool.domain == "scene"
        assert tool.reads_project is True
        assert tool.writes_project is True
        assert tool.needs_preview is True
        assert tool.mutating is True
        assert tool.read_path_args == ["path"]
        assert tool.schema["parameters"]["required"] == ["path"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_set_project_setting_is_registered_in_project_domain() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["set_project_setting"]

        assert tool.side == "front"
        assert tool.domain == "project"
        assert tool.writes_project is True
        assert tool.needs_preview is True
        assert tool.mutating is True
        assert tool.schema["parameters"]["required"] == ["key", "value"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_scene_agent_can_use_new_scene_tools() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "scene-agent.md"
    agent = load_agent_file(path)

    for name in ["delete_node", "reparent_node", "rename_node", "open_scene", "set_project_setting"]:
        assert name in agent.tools
