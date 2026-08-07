"""Shared model, effort, temperature, and thinking-budget resolution."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from app.agents.types import EFFORT_LEVELS, AgentDefinition, EffortLevel, Frame
from app.sessions.store import Session

EFFORT_TEMPERATURE: dict[EffortLevel, float] = {
    "quick": 0.2,
    "standard": 0.7,
    "deep": 0.7,
    "verify": 0.0,
    "advisor": 0.3,
}
EFFORT_THINKING_BUDGET: dict[EffortLevel, int] = {
    "quick": 4096,
    "standard": 16384,
    "deep": -1,
    "verify": -1,
    "advisor": -1,
}
FIXED_EFFORT_AGENTS: frozenset[str] = frozenset(
    {"advisor", "map-reviewer-agent", "map-validator-agent"}
)


def resolve_model(agent: AgentDefinition) -> str | None:
    """Resolve an explicit agent model; inherit uses the provider default."""
    if agent.model is None or agent.model == "inherit":
        return None
    return agent.model


def resolve_model_for_effort(
    agent: AgentDefinition,
    effort: EffortLevel,
    model_selector: Callable[[EffortLevel], str | None] | None,
) -> str | None:
    """Resolve an explicit agent model before an effort-based selection."""
    agent_model = resolve_model(agent)
    if agent_model is not None:
        return agent_model
    return model_selector(effort) if model_selector is not None else None


def resolve_request_model(
    agent: AgentDefinition,
    effort: EffortLevel,
    model_selector: Callable[[EffortLevel], str | None] | None,
    model_override: str | None,
) -> str | None:
    """Resolve a request override before agent and effort policy."""
    return model_override or resolve_model_for_effort(agent, effort, model_selector)


def resolve_effort(session: Session, frame: Frame) -> EffortLevel:
    """Resolve root, fixed-role, and inherited Session effort deterministically."""
    if frame.parent_id is None and session.effort in EFFORT_LEVELS:
        return cast(EffortLevel, session.effort)
    if frame.agent.name in FIXED_EFFORT_AGENTS:
        return frame.agent.effort
    if session.effort in EFFORT_LEVELS:
        return cast(EffortLevel, session.effort)
    return frame.agent.effort


def resolve_temperature(effort: EffortLevel) -> float:
    """Map an effort level to the shared sampling temperature."""
    return EFFORT_TEMPERATURE[effort]


def resolve_thinking_budget(
    effort: EffortLevel,
    selector: Callable[[EffortLevel], int | None] | None = None,
) -> int:
    """Resolve configured thinking budget before the shared effort default."""
    if selector is not None:
        override = selector(effort)
        if override is not None:
            return override
    return EFFORT_THINKING_BUDGET[effort]
