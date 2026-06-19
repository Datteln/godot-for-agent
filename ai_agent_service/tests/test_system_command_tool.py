from __future__ import annotations

from pathlib import Path

from app.agents.loader import load_agent_file
from app.permissions.engine import make_session_allow_grant
from app.tools.front_tools import register_front_tools
from app.tools.registry import REGISTRY


def test_system_command_is_registered_as_confirmed_process_tool() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["run_system_command"]

        assert tool.side == "front"
        assert tool.domain == "program"
        assert tool.executes_process is True
        assert tool.needs_preview is True
        assert tool.render_kind == "run"
        assert tool.mutating is True
        assert tool.schema["parameters"]["required"] == ["command"]
        shells = tool.schema["parameters"]["properties"]["shell"]["enum"]
        assert shells == ["auto", "powershell", "pwsh", "cmd", "sh", "bash", "zsh"]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)


def test_programming_agent_can_use_system_commands() -> None:
    path = Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "programming-agent.md"
    agent = load_agent_file(path)

    assert "run_system_command" in agent.tools
    assert "每次都必须由用户确认" in agent.prompt


def test_system_command_session_grant_is_scoped_to_exact_command() -> None:
    previous = REGISTRY.copy()
    try:
        REGISTRY.clear()
        register_front_tools()
        tool = REGISTRY["run_system_command"]
        first = make_session_allow_grant(
            tool,
            {"command": "git status", "shell": "powershell", "working_directory": "res://"},
        )
        same = make_session_allow_grant(
            tool,
            {"command": "git status", "shell": "powershell", "working_directory": "res://"},
        )
        different = make_session_allow_grant(
            tool,
            {"command": "git push", "shell": "powershell", "working_directory": "res://"},
        )

        assert first == same
        assert first != different
        assert first[3]
    finally:
        REGISTRY.clear()
        REGISTRY.update(previous)
