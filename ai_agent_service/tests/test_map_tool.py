from __future__ import annotations

from pathlib import Path

from app.agents.loader import load_agent_file
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY


def test_edit_map_is_registered_as_previewed_map_write() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["edit_map"]

        assert tool.side == "front"
        assert tool.domain == "map"
        assert tool.reads_project is True
        assert tool.writes_project is True
        assert tool.needs_preview is True
        assert tool.render_kind == "map"
        assert tool.schema["parameters"]["required"] == ["operations"]
        actions = tool.schema["parameters"]["properties"]["operations"]["items"]["properties"]["action"]
        assert actions["enum"] == ["fill", "erase", "copy"]
        assert "GridMap" in tool.schema["description"]
        assert "instead of refusing" in tool.schema["description"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_map_agent_is_instructed_and_allowed_to_use_edit_map() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "edit_map" in agent.tools
    assert "不要因为" in agent.prompt
    assert "GridMap" in agent.prompt


def test_coordinator_routes_map_edits_to_native_map_tool() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "coordinator.md"
    agent = load_agent_file(path)

    assert "直接调用 `edit_map`" in agent.prompt
    assert "不得因为 `.tscn`" in agent.prompt
