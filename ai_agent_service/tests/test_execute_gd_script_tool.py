from __future__ import annotations

from pathlib import Path

from app.agents.loader import load_agent_file
from app.permissions.engine import make_session_allow_grant
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY


def test_execute_gd_script_is_registered_as_confirmed_process_tool() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["execute_gd_script"]

        assert tool.side == "front"
        assert tool.domain == "program"
        assert tool.executes_process is True
        assert tool.needs_preview is True
        assert tool.render_kind == "run"
        assert tool.mutating is True
        assert tool.read_path_args == ["path"]
        assert tool.schema["parameters"]["required"] == ["path"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_programming_agent_can_execute_gd_scripts() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "programming-agent.md"
    agent = load_agent_file(path)

    assert "execute_gd_script" in agent.tools
    assert "execute_gd_script" in agent.prompt


def test_execute_gd_script_session_grant_is_scoped_to_exact_args() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["execute_gd_script"]
        first = make_session_allow_grant(tool, {"path": "tools/generate_map.gd", "args": ["42"]})
        same = make_session_allow_grant(tool, {"path": "tools/generate_map.gd", "args": ["42"]})
        different = make_session_allow_grant(tool, {"path": "tools/generate_map.gd", "args": ["7"]})

        assert first == same
        assert first != different
        assert first[3]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)
