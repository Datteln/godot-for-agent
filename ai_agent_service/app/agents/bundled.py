"""内置 Agent 定义（markdown frontmatter + body）。"""

from __future__ import annotations

from pathlib import Path

from app.agents.loader import load_agent_file
from app.agents.types import AgentDefinition, resolve_effective_tools


def _agent_defs_dir() -> Path:
    return Path(__file__).parent / "agent_defs"


def _load_bundled_agents() -> dict[str, AgentDefinition]:
    agents: dict[str, AgentDefinition] = {}
    for path in sorted(_agent_defs_dir().glob("*.md")):
        agent = load_agent_file(path)
        agents[agent.name] = agent
    return agents


AGENTS: dict[str, AgentDefinition] = _load_bundled_agents()


def get_agent(name: str, available_tools: set[str]) -> AgentDefinition:
    """按名称取出内置 agent 定义，并按当前可见工具集合解析 `effective_tools`。

    Args:
        name: agent 名（M0 仅 `"coordinator"` 有效）。
        available_tools: 当前入口/权限模式下实际可见的工具名集合。

    Returns:
        已解析 `effective_tools`/`warnings` 的 `AgentDefinition`。

    Raises:
        KeyError: `name` 不在内置 agent 注册表中。
    """
    base = AGENTS[name]
    return resolve_effective_tools(base, available_tools)
