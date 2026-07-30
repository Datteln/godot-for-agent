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
        assert tool.schema["parameters"]["required"] == [
            "operations",
            "expected_revision",
            "target_path",
        ]
        actions = tool.schema["parameters"]["properties"]["operations"]["items"]["properties"]["action"]
        assert actions["enum"] == ["fill", "erase", "copy"]
        assert "GridMap" in tool.schema["description"]
        assert "instead of refusing" in tool.schema["description"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_describe_map_region_is_registered_as_read_only_map_tool() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["describe_map_region"]

        assert tool.side == "front"
        assert tool.domain == "map"
        assert tool.reads_project is True
        assert tool.is_read_only is True
        assert tool.render_kind == "json"
        assert tool.schema["parameters"]["required"] == []
        properties = tool.schema["parameters"]["properties"]
        assert {"target_path", "map_layer", "x", "y", "z", "width", "height", "depth"} <= properties.keys()
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_map_agent_delegates_map_writes_instead_of_editing_directly() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "edit_map" not in agent.tools
    assert {"delegate", "delegate_many", "read_map_artifact"} <= set(agent.tools)
    assert "地图修改只通过 Godot 原生工具" in agent.prompt


def test_map_agent_delegates_real_region_reads() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "describe_map_region" not in agent.tools
    assert "精确格子事实必须委派给兼容的 reader" in agent.prompt
    assert "不代替专职 agent 发明地图事实" in agent.prompt


def test_scene_agent_must_read_map_region_before_aligning_nodes() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "scene-agent.md"
    agent = load_agent_file(path)

    assert "describe_map_region" in agent.tools
    assert "node_position" in agent.prompt


def test_coordinator_routes_map_edits_to_map_agent() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "coordinator.md"
    agent = load_agent_file(path)

    assert "委派给 `map-agent`" in agent.prompt
    assert "coordinator 不直接修改地图内容" in agent.prompt
    assert "不得直接改写 `.tscn`" in agent.prompt


def test_coordinator_plan_for_map_steps_stays_high_level() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "coordinator.md"
    agent = load_agent_file(path)

    assert "不要写具体的 atlas 坐标" in agent.prompt
    assert "高层计划不该预填底层瓦片值" in agent.prompt
    assert "你没有 `describe_map_region` 工具" not in agent.prompt


def test_coordinator_routes_map_analysis_steps_to_map_agent_not_programming_agent() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "coordinator.md"
    agent = load_agent_file(path)

    assert "都必须交给 `map-agent`" in agent.prompt
    assert "即便这一步只是分析或验证" in agent.prompt
    assert "不要把这类步骤分给 `programming-agent` 或 `advisor`" in agent.prompt


def test_map_agent_requires_reader_and_artifact_boundaries() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "地图专业职责委派工作" in agent.prompt
    assert "完整地图上下文" in agent.prompt
    assert "read_map_artifact" in agent.prompt
    assert "两种 schema 不混用" in agent.prompt


def test_describe_map_region_schema_warns_about_legacy_tilemap_layers() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        describe_tool = REGISTRY["describe_map_region"]
        edit_tool = REGISTRY["edit_map"]

        assert "`layers` array" in describe_tool.schema["description"]
        assert "do not assume map_layer 0" in describe_tool.schema["description"]
        map_layer_doc = describe_tool.schema["parameters"]["properties"]["map_layer"]["description"]
        assert "Defaults" in map_layer_doc and "layers" in map_layer_doc

        assert "describe_map_region first" in edit_tool.schema["description"]
        edit_map_layer_doc = edit_tool.schema["parameters"]["properties"]["map_layer"]["description"]
        assert "not always the foreground/collidable layer" in edit_map_layer_doc
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_map_agent_must_not_guess_layer_or_revision() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "不猜测资源、节点、图层或 revision" in agent.prompt
    assert "精确格子事实必须委派给兼容的 reader" in agent.prompt
