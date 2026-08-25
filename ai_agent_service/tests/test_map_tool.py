from __future__ import annotations

import json
from pathlib import Path

from app.agents.loader import load_agent_file
from app.agents.types import AgentDefinition
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY
from app.transcript.writer import _bounded_args, approval_operation_summary


def _agent(name: str) -> AgentDefinition:
    """加载指定内置 agent 定义。"""
    return load_agent_file(Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / name)


def test_legacy_map_mutation_names_cannot_register() -> None:
    """验证已删除的地图写工具不再出现在前端注册表。"""
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        for name in ("edit_map", "fill_rect", "paint_from_image_grid"):
            assert name not in REGISTRY
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_class_docs_schema_and_visible_summary_are_bounded() -> None:
    """验证 ClassDB 查询必须显式有界且不会进入可见转录。"""
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        parameters = REGISTRY["read_class_docs"].schema["parameters"]
        assert parameters["properties"]["mode"]["enum"] == [
            "overview",
            "search",
            "members",
            "constants",
        ]
        assert parameters["properties"]["limit"]["maximum"] == 12
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)

def test_legacy_map_mutation_names_cannot_route_or_dispatch() -> None:
    """验证旧工具名既不在提示路由中，也不在前端 dispatch 中。"""
    root = Path(__file__).parents[2]
    coordinator = (root / "ai_agent_service" / "app" / "agents" / "agent_defs" / "coordinator.md").read_text(encoding="utf-8")
    executor = (root / "ai_agent_frontend" / "addons" / "ai_agent" / "tools" / "tool_executor.gd").read_text(encoding="utf-8")
    map_tools = (root / "ai_agent_frontend" / "addons" / "ai_agent" / "tools" / "map_tools.gd").read_text(encoding="utf-8")
    for name in ("edit_map", "fill_rect", "paint_from_image_grid"):
        assert name not in coordinator
        assert '"%s":' % name not in executor
        assert "func %s(" % name not in map_tools


def test_map_observation_tools_remain_read_only_and_reload_is_bounded() -> None:
    """验证保留的地图事实工具和受限 reload 工具的契约。"""
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        for name in ("describe_tilemap_selection", "describe_map_region"):
            tool = REGISTRY[name]
            assert tool.domain == "map"
            assert tool.is_read_only is True
            assert tool.writes_project is False

        reload_tool = REGISTRY["reload_map_targets"]
        assert reload_tool.domain == "map"
        assert reload_tool.is_read_only is True
        assert reload_tool.schema["parameters"]["required"] == ["targets", "approved_paths", "reload_mode"]
        assert reload_tool.schema["parameters"]["properties"]["targets"]["maxItems"] == 8
        assert reload_tool.schema["parameters"]["properties"]["reload_mode"]["enum"] == [
            "editor_visible",
            "resource_only",
            "runtime_only",
        ]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_map_agent_bootstraps_code_driven_authoring_without_map_mutation() -> None:
    """验证地图 agent 可建立 @tool 作者入口并保留通用编辑边界。"""
    agent = _agent("map-agent.md")
    for name in (
        "describe_tilemap_selection",
        "describe_map_region",
        "read_file",
        "apply_text_edit",
        "propose_script_edit",
        "propose_content_file",
        "reload_map_targets",
    ):
        assert name in agent.tools
    for legacy_name in ("edit_map", "fill_rect", "paint_from_image_grid"):
        assert legacy_name not in agent.tools
        assert legacy_name not in agent.prompt
    assert "godot-map-authoring" in agent.skills
    assert "@tool" in agent.prompt
    assert "generated-only" in agent.prompt
    assert "read_class_docs" in agent.prompt
    assert "unsupported_map_authoring_target" not in agent.prompt
    assert "advisory visual evidence" in agent.prompt


def test_map_authoring_skill_contains_safe_tool_bootstrap_instructions() -> None:
    """验证预加载技能明确给出 @tool 骨架和文档优先的 API 规则。"""
    root = Path(__file__).parents[1]
    skill = root / "app" / "skills" / "bundled" / "godot-map-authoring" / "SKILL.md"
    content = skill.read_text(encoding="utf-8")

    assert "@tool" in content
    assert "generated-only" in content
    assert "read_class_docs" in content
    assert "不要根据模型记忆" in content


def test_all_authorized_agents_receive_general_map_observation_tools() -> None:
    """验证可使用地图事实的 agent 均没有地图写入权限。"""
    for name in ("programming-agent.md", "scene-agent.md", "advisor.md", "resource-agent.md"):
        agent = _agent(name)
        assert "describe_tilemap_selection" in agent.tools
        assert "describe_map_region" in agent.tools
        assert "edit_map" not in agent.tools


def test_coordinator_routes_map_requests_to_code_driven_agent_workflow() -> None:
    """验证 coordinator 将地图写入明确路由至地图 agent。"""
    agent = _agent("coordinator.md")
    assert "委派给 `map-agent`" in agent.prompt
    assert "代码驱动地图工作流" in agent.prompt
    assert "通用 programming workflow" in agent.prompt
    assert "`edit_map`" not in agent.prompt


def test_map_approval_identifies_code_driven_batch_without_content() -> None:
    """验证审批摘要使用地图批次标签且不依赖文件内容。"""
    summary = approval_operation_summary(
        "apply_text_edit",
        {"workflow": "code_driven_map", "path": "maps/generator.gd", "content": "secret"},
    )
    assert summary == "代码驱动地图批次：修改"
    persisted = _bounded_args(
        {"workflow": "code_driven_map", "path": "maps/generator.gd", "new_string": "sensitive source"}
    )
    assert persisted["path"] == "maps/generator.gd"
    assert persisted["new_string"].startswith("<地图代码内容已脱敏；")
    assert "sensitive source" not in persisted["new_string"]


def test_map_workflow_end_to_end_fixtures_describe_honest_visual_outcomes() -> None:
    """验证 editor-visible 与 runtime-only fixture 的证据语义。"""
    fixture_dir = Path(__file__).parent / "fixtures" / "map_workflow"
    editor_visible = json.loads((fixture_dir / "editor_visible.json").read_text(encoding="utf-8"))
    runtime_only = json.loads((fixture_dir / "runtime_only.json").read_text(encoding="utf-8"))

    assert editor_visible["approved_edit"]["workflow"] == "code_driven_map"
    assert editor_visible["reload_request"]["targets"] == editor_visible["reload_request"]["approved_paths"]
    assert editor_visible["expected"] == {
        "reload_status": "reloaded",
        "visual_evidence": "captured",
        "semantic_verification": "not_established",
    }
    assert runtime_only["reload_request"]["reload_mode"] == "runtime_only"
    assert runtime_only["expected"]["reload_status"] == "unavailable"
    assert runtime_only["expected"]["reason"] == "runtime_only_generator_not_executed"
