"""真实 Frame 工厂上的 owner/worker 路由守卫矩阵。"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

from app.agents.types import AgentDefinition, Frame
from app.orchestrator.turn.contracts import ErrorTurnOutcome
from app.orchestrator.map_turn import MapTurnPolicy, _planner_route_guard
from app.orchestrator.turn.driver import TurnDriver
from app.orchestrator.frame_factory import create_child_frame
from app.orchestrator.map_contracts import MAP_WORKER_RESULT_SCHEMA
from app.orchestrator.map_context import record_map_owner_link
from app.orchestrator.map_state import MapTaskState

from app.recovery.supervisor import FAILURE_POLICIES
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext


def _agent(*, role: str, stage: str | None, skills: list[str] | None = None) -> AgentDefinition:
    return AgentDefinition(
        name=f"test-{role}-{stage}",
        source="bundled",
        description="route matrix agent",
        prompt="perform only the contracted role",
        pipeline_kind="map" if stage is not None else "general",
        role=role,
        map_stage=stage,
        skills=list(skills or []),
        can_delegate=role == "map_orchestrator",
    )


def _snapshot() -> dict[str, Any]:
    return {
        "artifact_ref": "map-artifact://snapshot-1",
        "snapshot_id": "snapshot-1",
        "digest": "sha256:snapshot-1",
        "target_path": "Map/Main",
        "map_layer": 0,
        "map_revision": 7,
        "execution_eligible": True,
        "cells": [{"cell": [1, 2], "occupied": True}],
        "occupancy": {"1,2": True},
    }


def _factory_graph() -> tuple[Session, Frame, Frame]:
    root = Frame(
        id="f1",
        agent=_agent(role="coordinator", stage=None),
        messages=[],
        map_request_lineage_id="lineage-1",
        map_task_id="task-1",
    )
    snapshot = _snapshot()
    session = Session(
        session_id="session-1",
        session_epoch="epoch-1",
        agent_stack=[root],
        frame_counter=1,
        map_task_state=MapTaskState(
            task_id="task-1",
            task_lineage_id="lineage-1",
            authoritative_snapshots={"Map/Main::map_layer=0": snapshot},
            latest_revisions={"Map/Main": 7},
            latest_layers={"Map/Main": 0},
        ),
    )
    owner = create_child_frame(
        session=session,
        parent=root,
        agent=_agent(role="map_orchestrator", stage="orchestrator"),
        task_text="own this map outcome",
        depth=1,
    )
    session.agent_stack.append(owner)
    record_map_owner_link(
        session.map_task_state,
        macro_step_id="map-step",
        owner_frame_id=owner.id,
        domain_task_id="map-step:epoch-1",
        target="Map/Main",
        revision=7,
    )
    planner = create_child_frame(
        session=session,
        parent=owner,
        agent=_agent(
            role="map_planner",
            stage="planner",
            skills=["godot-code-reading"],
        ),
        task_text="plan a route",
        depth=2,
        map_stage_contract={
            "stage": "planner",
            "target_path": "Map/Main",
            "map_revision": 7,
            "authoritative_snapshot": snapshot,
        },
    )
    return session, owner, planner


class _ForbiddenProvider:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("route mismatch must fail before provider invocation")


class TestPlannerRouteGuard:
    def test_valid_owner_from_factory_reaches_first_turn(self) -> None:
        session, owner, _ = _factory_graph()
        assert owner.domain_owner_contract["contract_kind"] == "domain_owner_v1"
        assert owner.map_stage_contract == {}
        assert owner.result_schema is None
        assert owner.worker_instance_id is None
        assert _planner_route_guard(owner, session) is None

    def test_owner_with_planner_contract_is_rejected(self) -> None:
        session, owner, planner = _factory_graph()
        malformed = create_child_frame(
            session=session,
            parent=session.agent_stack[0],
            agent=owner.agent,
            task_text="malformed owner",
            depth=1,
            map_stage_contract={
                "stage": "planner",
                "target_path": "Map/Main",
                "map_revision": 7,
                "authoritative_snapshot": _snapshot(),
            },
        )
        assert planner.result_schema == MAP_WORKER_RESULT_SCHEMA
        result = _planner_route_guard(malformed, session)
        assert result is not None
        assert result.error_code == "map_route_contract_violation"

    def test_valid_planner_child_is_allowed(self) -> None:
        session, _, planner = _factory_graph()
        session.agent_stack.append(planner)
        assert planner.map_stage_contract["contract_kind"] == "map_worker_stage_v1"
        assert planner.result_schema == MAP_WORKER_RESULT_SCHEMA
        assert _planner_route_guard(planner, session) is None

    def test_mismatched_worker_role_or_stage_is_rejected(self) -> None:
        session, _, planner = _factory_graph()
        session.agent_stack.append(planner)
        planner.agent = replace(planner.agent, map_stage="reader")
        assert _planner_route_guard(planner, session) is not None

    def test_stale_lineage_is_rejected(self) -> None:
        session, _, planner = _factory_graph()
        session.agent_stack.append(planner)
        planner.map_request_lineage_id = "stale-lineage"
        assert _planner_route_guard(planner, session) is not None

    def test_planner_missing_snapshot_is_rejected(self) -> None:
        session, owner, _ = _factory_graph()
        planner = create_child_frame(
            session=session,
            parent=owner,
            agent=_agent(
                role="map_planner",
                stage="planner",
                skills=["godot-code-reading"],
            ),
            task_text="plan without snapshot",
            depth=2,
            map_stage_contract={
                "stage": "planner",
                "target_path": "Map/Main",
                "map_revision": 7,
            },
        )
        session.agent_stack.append(planner)
        assert _planner_route_guard(planner, session) is not None

    def test_generic_node_cannot_reuse_worker_fields(self) -> None:
        session, _, planner = _factory_graph()
        generic = deepcopy(planner)
        generic.agent = _agent(role="specialist", stage=None)
        assert _planner_route_guard(generic, session) is not None

    def test_actual_mismatch_makes_zero_provider_calls(self) -> None:
        session, owner, _ = _factory_graph()
        owner.result_schema = MAP_WORKER_RESULT_SCHEMA
        provider = _ForbiddenProvider()
        security = SecuritySettings(project_root=Path.cwd())
        result = asyncio.run(
            TurnDriver(MapTurnPolicy()).run(
                session=session,
                llm=provider,
                security=security,
                tool_ctx=ToolContext(
                    security=security,
                    session_id=session.session_id,
                    session_epoch=session.session_epoch,
                ),
                max_turns=1,
            )
        )
        assert isinstance(result, ErrorTurnOutcome)
        assert result.error_code == "map_route_contract_violation"
        assert provider.calls == 0


def test_route_violation_recovery_is_backend_owned() -> None:
    policy = FAILURE_POLICIES["map_route_contract_violation"]
    assert policy.disposition == "retry_new_attempt"
    assert policy.retryable is True
    assert policy.side_effect_state == "none"
    assert policy.retry_owner == "backend"
    assert "user" not in policy.terminal_condition
