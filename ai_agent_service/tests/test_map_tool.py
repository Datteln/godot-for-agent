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


def test_map_agent_is_instructed_and_allowed_to_use_edit_map() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "edit_map" in agent.tools
    assert "不要因为" in agent.prompt
    assert "GridMap" in agent.prompt


def test_map_agent_must_read_real_region_before_blending_terrain() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "describe_map_region" in agent.tools
    assert "必须先用 `describe_map_region`" in agent.prompt
    assert "node_position" in agent.prompt


def test_scene_agent_must_read_map_region_before_aligning_nodes() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "scene-agent.md"
    agent = load_agent_file(path)

    assert "describe_map_region" in agent.tools
    assert "node_position" in agent.prompt


def test_coordinator_routes_map_edits_to_native_map_tool() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "coordinator.md"
    agent = load_agent_file(path)

    assert "直接调用 `edit_map`" in agent.prompt
    assert "不得因为 `.tscn`" in agent.prompt


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


def test_map_agent_batches_follow_read_plan_edit_verify_loop() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "读边界 → 写块计划 → 小批 `edit_map` → 核对结果 → 必要时重读" in agent.prompt
    assert "预期 `cells` 数量" in agent.prompt
    assert "不必每批重读" in agent.prompt
    assert "更新这一块的计划" in agent.prompt


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


def test_map_agent_must_check_real_layers_before_picking_map_layer() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "map-agent.md"
    agent = load_agent_file(path)

    assert "`layers` 字段" in agent.prompt
    assert "不要默认 `map_layer=0`" in agent.prompt
    assert "physics_layer" in agent.prompt
