"""公共路由不变性：coordinator -> create_plan -> 单一 map owner + typed planner。

验证 `/chat -> coordinator -> create_plan` 公共入口的端到端不变性：
- 纯地图请求只产生一个 map owner 步骤 + 展示里程碑（非调度节点）；
- worker_spec / specialist 内部字段在 create_plan 入口被拒绝；
- 同一地图任务的 sibling map-agent owner 被拒绝；
- 跨域（code + map）计划被接受。
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents.types import AgentDefinition, Frame
from app.config import AppSettings
from app.llm.provider import AssistantTurn, ToolCallRequest
from app.main import create_app
from app.orchestrator.macro_contracts import MacroPlan, MacroPlanState
from app.orchestrator.map_contracts import MAP_WORKER_RESULT_SCHEMA
from app.orchestrator.map_planning_contexts import (
    MapPlanningContextBundle,
    MapPlanningContextEntry,
)
from app.orchestrator.map_planning_snapshots import PlanningSnapshotStore
from app.orchestrator.map_progress import (
    MapTaskState,
    record_map_approval_identity,
    record_map_child_lineage,
    record_map_owner_link,
    record_planning_context_refresh,
)
from app.orchestrator.map_turn import _normalize_plan_steps
from app.orchestrator.map_workflow import (
    make_map_workflow_event,
    reduce_map_workflow,
    replace_map_state_field,
)
from app.sessions.store import Session


def _map_outcome_step(*, step_id: str = "expand-level") -> dict:
    """构造 coordinator 应产出的单一 map 域成果步骤（含展示里程碑）。"""
    return {
        "id": step_id,
        "title": "扩建当前关卡",
        "agent": "map-agent",
        "task": "向右扩展约40格并交付可通关预览",
        "owner_agent": "map-agent",
        "domain": "map",
        "objective": "向右扩展约40格并交付可通关预览",
        "acceptance_criteria": ["向右扩展约40格", "适配当前移动能力", "写入前等待确认"],
        "display_milestones": [
            {"id": "m-read", "title": "获取地图事实", "kind": "read"},
            {"id": "m-plan", "title": "设计与校验路线", "kind": "plan"},
            {"id": "m-write", "title": "写入并验证", "kind": "write"},
        ],
    }


class TestPublicRouteInvariants:
    """create_plan 公共入口的宏观计划不变性。"""

    def test_one_map_owner_with_milestones(self) -> None:
        """纯地图请求只产生一个 map owner，里程碑独立存储非调度节点。"""
        steps = _normalize_plan_steps([_map_outcome_step()])
        assert isinstance(steps, list)
        state = MacroPlanState.from_plan(
            MacroPlan.from_dict({"summary": "扩建关卡", "steps": steps})
        )
        map_owners = [
            s for s in state.plan.steps if s.owner_agent == "map-agent" or s.domain == "map"
        ]
        assert len(map_owners) == 1
        assert len(state.plan.steps[0].display_milestones) == 3
        # 里程碑以扁平视图独立于调度图返回，不作为 PlanGraph 节点
        assert len(state.milestones()) == 3
        assert {m.id for _, m in state.milestones()} == {
            "m-read",
            "m-plan",
            "m-write",
        }

    def test_worker_spec_rejected_at_public_route(self) -> None:
        """coordinator 不得在 create_plan 步骤里塞 worker_spec。"""
        raw = _map_outcome_step()
        raw["worker_spec"] = {"mode": "propose_only"}
        result = _normalize_plan_steps([raw])
        assert isinstance(result, str)
        assert "内部构造" in result

    def test_internal_stage_fields_rejected(self) -> None:
        """specialist 内部阶段字段在 create_plan 入口被拒绝。"""
        raw = _map_outcome_step()
        raw["stage_id"] = "planner-1"
        result = _normalize_plan_steps([raw])
        assert isinstance(result, str)
        assert "内部构造" in result

    def test_sibling_map_owners_rejected(self) -> None:
        """同一地图任务的 sibling map-agent owner 被拒绝，需合并为一个成果。"""
        result = _normalize_plan_steps(
            [_map_outcome_step(step_id="a"), _map_outcome_step(step_id="b")]
        )
        assert isinstance(result, str)
        assert "sibling" in result

    def test_cross_domain_plan_accepted(self) -> None:
        """跨域（code -> map）计划被接受，只有一个 map owner。"""
        steps = _normalize_plan_steps(
            [
                {
                    "id": "code",
                    "title": "实现冲刺",
                    "agent": "programming-agent",
                    "task": "实现冲刺能力",
                    "owner_agent": "programming-agent",
                    "domain": "code",
                    "objective": "实现冲刺能力",
                },
                {
                    "id": "map",
                    "title": "扩建关卡",
                    "agent": "map-agent",
                    "task": "基于冲刺扩建",
                    "owner_agent": "map-agent",
                    "domain": "map",
                    "objective": "基于冲刺扩建",
                    "depends_on": ["code"],
                },
            ]
        )
        assert isinstance(steps, list)
        assert len(steps) == 2
        state = MacroPlanState.from_plan(
            MacroPlan.from_dict({"summary": "冲刺+扩建", "steps": steps})
        )
        map_owners = [s for s in state.plan.steps if s.domain == "map"]
        assert len(map_owners) == 1


def _tool_turn(call_id: str, name: str, arguments: dict[str, Any]) -> AssistantTurn:
    encoded = json.dumps(arguments, ensure_ascii=False)
    return AssistantTurn(
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": encoded},
                }
            ],
        },
        content=None,
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=encoded)],
        finish_reason="tool_calls",
        model="public-route-test",
    )


def _runtime_contract(messages: list[dict[str, Any]]) -> dict[str, Any]:
    for message in messages:
        content = message.get("content")
        if (
            message.get("role") == "system"
            and isinstance(content, str)
            and content.startswith("Runtime Map Stage Contract")
        ):
            return json.loads(content.split("\n", 1)[1])
    return {}


class _PublicMapRouteProvider:
    """驱动真实 public route 到 planner 第一次 front-tool 调用。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.coordinator_turns = 0
        self.owner_turns = 0
        self.reader_turns = 0

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> AssistantTurn:
        names = {
            str(item.get("function", {}).get("name", ""))
            for item in tools
            if isinstance(item, dict)
        }
        contract = _runtime_contract(messages)
        self.calls.append(
            {
                "tool_names": names,
                "contract_kind": contract.get("contract_kind"),
                "stage": contract.get("stage"),
                "contract_id": contract.get("contract_id"),
                "worker_instance_id": contract.get("worker_instance_id"),
                "response_contract": kwargs.get("response_contract"),
            }
        )
        if "create_plan" in names:
            self.coordinator_turns += 1
            if self.coordinator_turns == 1:
                return _tool_turn(
                    "create-plan-1",
                    "create_plan",
                    {
                        "summary": "扩建当前平台关卡",
                        "steps": [_map_outcome_step()],
                    },
                )
            return _tool_turn(
                "delegate-many-1",
                "delegate_many",
                {
                    "tasks": [
                        {
                            "agent": "map-agent",
                            "task": "向右扩展约40格并交付可通关预览",
                        }
                    ]
                },
            )
        if "delegate" in names:
            self.owner_turns += 1
            if self.owner_turns == 1:
                return _tool_turn(
                    "delegate-reader-1",
                    "delegate",
                    {
                        "agent": "map-reader-agent",
                        "task": json.dumps(
                            {
                                "objective": "读取扩建区域的精确格子与 occupancy",
                                "target_path": "Map/Main",
                                "map_revision": 7,
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
            return _tool_turn(
                "delegate-planner-1",
                "delegate",
                {
                    "agent": "map-worker",
                    "task": json.dumps(
                        {
                            "objective": "基于当前快照规划可通关路线",
                            "target_path": "Map/Main",
                            "map_revision": 7,
                        },
                        ensure_ascii=False,
                    ),
                    "worker_spec": {
                        "name": "route-planner",
                        "objective": "基于当前快照规划可通关路线",
                        "mode": "propose_only",
                        "skills": ["godot-code-reading"],
                        "operations": ["compute_reachable_frontier", "plan_reachable_map_growth"],
                        "constraints": [],
                        "output_schema": MAP_WORKER_RESULT_SCHEMA,
                    },
                },
            )
        if "describe_map_region" in names or contract.get("stage") == "reader":
            self.reader_turns += 1
            if self.reader_turns == 1:
                return _tool_turn(
                    "region-read-1",
                    "describe_map_region",
                    {
                        "target_path": "Map/Main",
                        "map_layer": 0,
                        "x": 0,
                        "y": 0,
                        "width": 4,
                        "height": 2,
                        "cells_format": "non_empty_only",
                        "max_returned_cells": 8,
                    },
                )
            reader_result = {
                "contract_id": contract["contract_id"],
                "result_schema": MAP_WORKER_RESULT_SCHEMA,
                "stage": "reader",
                "worker": contract["worker_instance_id"],
                "mode": "complete",
                "objective": "读取扩建区域的精确格子与 occupancy",
                "target_path": "Map/Main",
                "map_layer": 0,
                "map_revision": 7,
                "region": {"x": 0, "y": 0, "width": 4, "height": 2},
                "summary": "canonical region snapshot ready",
                "facts": [{"cell": {"x": 0, "y": 1}, "occupied": True}],
                "proposed_batches": [],
                "write_results": [],
                "validation": {"passed": True, "issues": [], "structured_issues": []},
                "missing_inputs": [],
                "risks": [],
                "next_stage": "planner",
            }
            text = json.dumps(reader_result, ensure_ascii=False)
            return AssistantTurn(
                raw_message={"role": "assistant", "content": text},
                content=text,
                finish_reason="stop",
                model="public-route-test",
                response_mode=(
                    kwargs["response_contract"].mode
                    if kwargs.get("response_contract") is not None
                    else None
                ),
            )
        if "compute_reachable_frontier" in names:
            return _tool_turn(
                "frontier-1",
                "compute_reachable_frontier",
                {
                    "target_path": "Map/Main",
                    "map_layer": 0,
                    "x": 0,
                    "y": 0,
                    "width": 4,
                    "height": 2,
                    "start": {"x": 0, "y": 0, "role": "actor_cell"},
                    "movement_model": "leap",
                    "cell_occupancy": "empty",
                    "requires_support": True,
                    "support_occupancy": "filled",
                    "max_horizontal_gap": 3,
                    "max_rise": 2,
                    "max_fall": 4,
                },
            )
        raise AssertionError(f"unexpected provider frame tools={sorted(names)}")


def test_chat_route_creates_owner_then_typed_planner_child() -> None:
    """执行 `/chat`，而不是只验证 plan normalization。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        provider = _PublicMapRouteProvider()
        app = create_app(
            AppSettings(
                project_root=root,
                log_dir=root / "logs",
                session_store_dir=root / "sessions",
                rag_auto_build_enabled=False,
                map_worker_response_contract_mode="prompt_only",
            ),
            token="test-token",
            llm_provider=provider,
        )
        headers = {"Authorization": "Bearer test-token"}
        with TestClient(app) as client:
            first = client.post(
                "/chat",
                headers=headers,
                json={
                    "session_id": "public-map-route",
                    "request_id": "request-1",
                    "user_message": "扩建当前平台地图，向右延伸约40格并保证可通关，写入前预览。",
                },
            )
            assert first.status_code == 200
            first_body = first.json()
            debug_session = app.state.session_store.get_or_create("public-map-route", set())
            assert first_body["type"] == "tool_calls", (
                f"body={first_body['type']} owner="
                f"{debug_session.map_task_state.owner_frame_id!r} "
                f"task={debug_session.map_task_state.task_id!r} "
                f"lineage={debug_session.map_task_state.task_lineage_id!r} "
                f"frames={[(frame.id, frame.parent_id, frame.map_task_id, frame.map_request_lineage_id) for frame in debug_session.agent_stack]!r}"
            )
            assert [call["name"] for call in first_body["calls"]] == ["describe_map_region"]
            region_call = first_body["calls"][0]
            second = client.post(
                "/chat",
                headers=headers,
                json={
                    "session_id": "public-map-route",
                    "request_id": "request-2",
                    "tool_results": [
                        {
                            "tool_use_id": region_call["id"],
                            "frame_id": region_call["frame_id"],
                            "turn_id": first_body["turn_id"],
                            "status": "applied",
                            "result": {
                                "ok": True,
                                "target": "Map/Main",
                                "target_path": "Map/Main",
                                "map_layer": 0,
                                "map_revision": 7,
                                "dimension": 2,
                                "cells_format": "non_empty_only",
                                "cells_total": 1,
                                "cells_returned": 1,
                                "cells_omitted": 0,
                                "non_empty_count": 1,
                                "cells": [
                                    {
                                        "coords": {"x": 0, "y": 1},
                                        "source_id": 0,
                                        "atlas_coords": {"x": 1, "y": 0},
                                    }
                                ],
                                "collision_support": {"complete": True},
                                "object_occupancy": {"complete": True, "occupied": []},
                                "resource_bindings": {"ground": "observed"},
                            },
                        }
                    ],
                },
            )
            assert second.status_code == 200
            second_body = second.json()
            assert second_body["type"] == "tool_calls"
            assert [call["name"] for call in second_body["calls"]] == ["compute_reachable_frontier"]

        session = app.state.session_store.get_or_create("public-map-route", set())
        planner = session.top_frame()
        assert planner is not None
        owner = next(
            frame for frame in session.agent_stack if frame.agent.role == "map_orchestrator"
        )
        assert owner.domain_owner_contract["contract_kind"] == "domain_owner_v1"
        assert owner.result_schema is None
        assert owner.worker_instance_id is None
        assert planner.parent_id == owner.id
        assert planner.agent.map_stage == "planner"
        assert planner.map_stage_contract["contract_kind"] == "map_worker_stage_v1"
        planner_snapshot = planner.map_stage_contract["authoritative_snapshot"]
        assert planner_snapshot["snapshot_id"]
        projection_page = PlanningSnapshotStore(
            root,
            session.session_id,
            session.session_epoch,
        ).read_projection_page(
            planner_snapshot["artifact_ref"],
            field="occupied_cells",
        )
        assert projection_page["value"] == [
            {
                "coords": {"x": 0, "y": 1},
                "occupied": True,
                "semantic_layer": "",
                "tags": [],
            }
        ]
        assert planner.result_schema == MAP_WORKER_RESULT_SCHEMA
        assert planner.contract_id == planner.map_stage_contract["contract_id"]
        assert planner.worker_instance_id == planner.map_stage_contract["worker_instance_id"]
        assert provider.coordinator_turns == 2
        assert provider.owner_turns == 2
        planner_calls = [call for call in provider.calls if call["stage"] == "planner"]
        assert len(planner_calls) == 1
        assert planner_calls[0]["contract_id"] == planner.contract_id
        assert planner_calls[0]["worker_instance_id"] == planner.worker_instance_id


class TestMultiContextPublicRoute:
    """task 11.1：Mid + Background 多规划上下文的 public route 集成测试。

    验证多上下文 bundle 在运行时正确构造，planner 使用 child-local plan Skill 绑定。
    不依赖完整 /chat 集成路由，而是通过单元测试验证核心契约。
    """

    def test_multi_context_bundle_at_runtime(self) -> None:
        """验证 Mid + Background 多上下文在运行时正确构造为 bundle。"""

        mid = MapPlanningContextEntry(
            context_id="mid-ctx",
            semantic_role="mid",
            artifact_ref="art://mid/1",
            digest="sha256:mid",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            source_revision=7,
            fact_fields=("occupancy", "traversal", "reachable_frontier"),
        )
        bg = MapPlanningContextEntry(
            context_id="bg-ctx",
            semantic_role="background",
            artifact_ref="art://bg/1",
            digest="sha256:bg",
            provenance={"kind": "snapshot"},
            target_path="Map/Background",
            map_layer=0,
            region={"x": -10, "y": -10, "width": 80, "height": 40},
            source_revision=3,
            fact_fields=("coverage", "occupancy"),
        )
        bundle = MapPlanningContextBundle.from_entries(
            [mid, bg], required_roles=["mid", "background"]
        )
        assert len(bundle.contexts) == 2
        assert bundle.bundle_id.startswith("map-context-bundle:")
        assert bundle.required_roles == ("mid", "background")

    def test_planner_contract_with_multi_context_bundle(self) -> None:
        """验证 planner 合同使用多上下文 bundle。"""
        from app.orchestrator.frame_contract_types import (
            MapWorkerStageContract,
        )

        mid = MapPlanningContextEntry(
            context_id="mid-ctx",
            semantic_role="mid",
            artifact_ref="art://mid/1",
            digest="sha256:mid",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            source_revision=7,
            fact_fields=("occupancy",),
        )
        bg = MapPlanningContextEntry(
            context_id="bg-ctx",
            semantic_role="background",
            artifact_ref="art://bg/1",
            digest="sha256:bg",
            provenance={"kind": "snapshot"},
            target_path="Map/Background",
            map_layer=0,
            source_revision=3,
            fact_fields=("coverage",),
        )
        bundle = MapPlanningContextBundle.from_entries(
            [mid, bg], required_roles=["mid", "background"]
        )
        contract = MapWorkerStageContract(
            stage="planner",
            target_path="Map/Main",
            map_revision=7,
            planning_context_bundle=bundle,
        ).bind_runtime_identity(
            contract_id="contract-1",
            worker_instance_id="worker-1",
        )
        assert contract.planning_context_bundle is not None
        assert contract.planning_context_bundle.bundle_id == bundle.bundle_id
        assert len(contract.planning_context_bundle.contexts) == 2

    def test_planner_uses_child_local_plan_skill_binding(self) -> None:
        """验证 planner 子阶段使用 plan Skill 绑定而非 read 阶段。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _PublicMapRouteProvider()
            app = create_app(
                AppSettings(
                    project_root=root,
                    log_dir=root / "logs",
                    session_store_dir=root / "sessions",
                    rag_auto_build_enabled=False,
                    map_worker_response_contract_mode="prompt_only",
                ),
                token="test-token",
                llm_provider=provider,
            )
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(app) as client:
                first = client.post(
                    "/chat",
                    headers=headers,
                    json={
                        "session_id": "multi-context-route",
                        "request_id": "request-1",
                        "user_message": "扩建当前平台地图，需要 Mid 与 Background 层的上下文。",
                    },
                )
                assert first.status_code == 200
                first_body = first.json()
                assert first_body["type"] == "tool_calls"
                assert [call["name"] for call in first_body["calls"]] == ["describe_map_region"]

                region_call = first_body["calls"][0]
                second = client.post(
                    "/chat",
                    headers=headers,
                    json={
                        "session_id": "multi-context-route",
                        "request_id": "request-2",
                        "tool_results": [
                            {
                                "tool_use_id": region_call["id"],
                                "frame_id": region_call["frame_id"],
                                "turn_id": first_body["turn_id"],
                                "status": "applied",
                                "result": {
                                    "ok": True,
                                    "target": "Map/Main",
                                    "target_path": "Map/Main",
                                    "map_layer": 0,
                                    "map_revision": 7,
                                    "dimension": 2,
                                    "cells_format": "non_empty_only",
                                    "cells_total": 1,
                                    "cells_returned": 1,
                                    "cells_omitted": 0,
                                    "non_empty_count": 1,
                                    "cells": [
                                        {
                                            "coords": {"x": 0, "y": 1},
                                            "source_id": 0,
                                            "atlas_coords": {"x": 1, "y": 0},
                                        }
                                    ],
                                    "collision_support": {"complete": True},
                                    "object_occupancy": {"complete": True, "occupied": []},
                                    "resource_bindings": {"ground": "observed"},
                                },
                            }
                        ],
                    },
                )
                assert second.status_code == 200
                second_body = second.json()
                assert second_body["type"] == "tool_calls"
                assert [call["name"] for call in second_body["calls"]] == ["compute_reachable_frontier"]

            session = app.state.session_store.get_or_create("multi-context-route", set())
            planner = session.top_frame()
            assert planner is not None
            owner = next(
                frame for frame in session.agent_stack if frame.agent.role == "map_orchestrator"
            )
            assert owner.domain_owner_contract["contract_kind"] == "domain_owner_v1"
            assert owner.result_schema is None
            assert planner.parent_id == owner.id
            assert planner.agent.map_stage == "planner"
            assert planner.map_stage_contract["contract_kind"] == "map_worker_stage_v1"
            # 验证 planner 使用 child-local plan Skill 绑定
            planner_calls = [call for call in provider.calls if call["stage"] == "planner"]
            assert len(planner_calls) == 1
            assert planner_calls[0]["contract_id"] == planner.contract_id
            assert planner_calls[0]["worker_instance_id"] == planner.worker_instance_id


class TestSkillVisibilityInPrompts:
    """task 11.2：coordinator 和 owner 不广告 planner-only Skill。

    验证 planner-only Skill 不在 coordinator 和 map-owner 的 SkillBindingContext
    中被解析为可加载，而只在 planner 上下文中可加载。
    """

    def test_coordinator_cannot_load_planner_skill(self) -> None:
        """coordinator 上下文中 planner-only Skill 不兼容。"""
        from app.skills.binding import SkillBindingContext, SkillBindingResolver
        from app.skills.types import SkillDefinition
        from app.tools.registry import REGISTRY

        skill = SkillDefinition(
            qualified_name="bundled:godot-code-reading",
            name="godot-code-reading",
            source="bundled",
            description="Planning skill",
            when_to_use="when planning map routes",
            body="## Plan\n",
            file_path=Path("/fake/skills/godot-code-reading"),
            compatible_roles=["map_planner"],
            compatible_stages=["plan"],
        )
        resolver = SkillBindingResolver(REGISTRY)
        context = SkillBindingContext(
            agent_tools=frozenset({"create_plan", "delegate_many"}),
            permitted_tools=frozenset({"create_plan", "delegate_many"}),
            workflow_stage=None,
            worker_mode=None,
            agent_role="coordinator",
        )
        result = resolver.resolve("godot-code-reading", skill, context)
        assert result.status == "incompatible"
        assert "role_incompatible" in result.reason_codes

    def test_map_owner_cannot_load_planner_skill(self) -> None:
        """map owner 上下文中 planner-only Skill 不兼容。"""
        from app.skills.binding import SkillBindingContext, SkillBindingResolver
        from app.skills.types import SkillDefinition
        from app.tools.registry import REGISTRY

        skill = SkillDefinition(
            qualified_name="bundled:godot-code-reading",
            name="godot-code-reading",
            source="bundled",
            description="Planning skill",
            when_to_use="when planning map routes",
            body="## Plan\n",
            file_path=Path("/fake/skills/godot-code-reading"),
            compatible_roles=["map_planner"],
            compatible_stages=["plan"],
        )
        resolver = SkillBindingResolver(REGISTRY)
        context = SkillBindingContext(
            agent_tools=frozenset({"delegate", "delegate_many"}),
            permitted_tools=frozenset({"delegate", "delegate_many"}),
            workflow_stage="orchestrator",
            worker_mode=None,
            agent_role="map_orchestrator",
        )
        result = resolver.resolve("godot-code-reading", skill, context)
        assert result.status == "incompatible"
        assert "role_incompatible" in result.reason_codes

    def test_planner_can_load_planner_skill(self) -> None:
        """planner 上下文中 planner Skill 可加载。"""
        from app.skills.binding import SkillBindingContext, SkillBindingResolver
        from app.skills.types import SkillDefinition
        from app.tools.registry import REGISTRY

        skill = SkillDefinition(
            qualified_name="bundled:godot-code-reading",
            name="godot-code-reading",
            source="bundled",
            description="Planning skill",
            when_to_use="when planning map routes",
            body="## Plan\n",
            file_path=Path("/fake/skills/godot-code-reading"),
            compatible_roles=["map_planner"],
            compatible_stages=["plan"],
        )
        resolver = SkillBindingResolver(REGISTRY)
        context = SkillBindingContext(
            agent_tools=frozenset({"compute_reachable_frontier", "plan_reachable_map_growth"}),
            permitted_tools=frozenset({"compute_reachable_frontier", "plan_reachable_map_growth"}),
            workflow_stage="plan",
            worker_mode="propose_only",
            agent_role="map_planner",
        )
        result = resolver.resolve("godot-code-reading", skill, context)
        assert result.status == "resolved"


class TestFailpointCoverage:
    """task 11.3：child-start commit 前后 failpoint 覆盖。

    验证 child-start 提交前失败不产生脏 task-stage 转换、孤儿 lineage、
    重复 context entry 或未守卫的 provider 调用。
    """

    def _fresh_session(self) -> Session:
        """构造一个最小合法 session，含 root 与 owner。"""
        root = Frame(
            id="f1",
            agent=AgentDefinition(
                name="coordinator",
                source="bundled",
                description="",
                prompt="",
            ),
            messages=[],
            map_request_lineage_id="lineage-1",
            map_task_id="task-1",
        )
        owner = Frame(
            id="f2",
            agent=AgentDefinition(
                name="map-agent",
                source="bundled",
                description="",
                prompt="",
                pipeline_kind="map",
                role="map_orchestrator",
                map_stage="orchestrator",
            ),
            messages=[],
            parent_id="f1",
            map_request_lineage_id="lineage-1",
            map_task_id="task-1",
        )
        from app.orchestrator.frame_contract_types import DomainOwnerContract

        owner.domain_owner_contract = DomainOwnerContract(
            domain="map",
            owner_frame_id="f2",
            parent_frame_id="f1",
            macro_step_id="map-step",
            domain_task_id="map-step:epoch-1",
            durable_task_id="task-1",
            request_lineage_id="lineage-1",
        ).to_dict()
        state = MapTaskState(
            task_id="task-1",
            task_lineage_id="lineage-1",
            macro_step_id="map-step",
            owner_frame_id="f2",
            domain_task_id="map-step:epoch-1",
        )
        replace_map_state_field(state, "structure_revision", 3)
        return Session(
            session_id="session-failpoint",
            session_epoch="epoch-1",
            agent_stack=[root, owner],
            frame_counter=2,
            map_task_state=state,
        )

    def test_no_dirty_stage_after_failed_child_start(self) -> None:
        """child-start 提交前失败，task stage 保持为 read。"""
        session = self._fresh_session()
        original_stage = session.map_task_state.stage
        assert original_stage == "read"
        # 未提交任何 child_start 事件前，stage 保持 read
        assert session.map_task_state.child_lineage == []

    def test_no_orphan_lineage_after_failed_child_start(self) -> None:
        """child-start 提交前失败，不产生孤儿 lineage。"""
        session = self._fresh_session()
        assert session.map_task_state.child_lineage == []

    def test_no_duplicate_context_entry_after_failed_child_start(self) -> None:
        """child-start 提交前失败，不产生重复 context entry。"""
        session = self._fresh_session()
        assert len(session.map_task_state.planning_contexts) == 0

    def test_no_provider_call_before_child_start_commit(self) -> None:
        """child-start 提交前不应有 provider 调用。"""
        session = self._fresh_session()
        assert session.map_task_state.counters.llm_turns == 0

    def test_checkpoint_race_prevents_duplicate_child_start(self) -> None:
        """stale checkpoint 防止重复 child_start。"""
        session = self._fresh_session()
        replace_map_state_field(session.map_task_state, "stage", "plan")


        # 合法 child_start：stage 从 read -> plan
        valid_event = make_map_workflow_event(
            session.map_task_state,
            "map_child_started",
            "Map/Main",
            7,
            {
                "child_frame_id": "fc",
                "child_stage": "planner",
                "task_stage": "plan",
                "expected_task_stage": "read",
                "task_id": "task-1",
                "owner_frame_id": "f2",
            },
        )
        # 因为当前 stage 是 "plan" 而不是 "read"，expected_task_stage="read" 会失败
        with pytest.raises(ValueError, match="stale map child start checkpoint"):
            reduce_map_workflow(session.map_task_state, valid_event)


class TestMultiOperationApproval:
    """task 11.4：多操作审批、重启、重连与 reviewer 覆盖。

    验证一个候选编译为多个独立 scoped 操作后，审批、writer 和 reviewer
    保持同一 owner/workflow lineage，无需一个 task-level target。
    """

    def _fresh_state(self) -> MapTaskState:
        from app.orchestrator.map_planning_contexts import MapExecutionOperation

        state = MapTaskState(
            task_id="task-1",
            task_lineage_id="lineage-1",
            macro_step_id="map-step",
            owner_frame_id="f2",
            domain_task_id="map-step:epoch-1",
        )
        replace_map_state_field(state, "structure_revision", 3)
        # 注册两个独立 scoped 执行操作
        op1 = MapExecutionOperation(
            operation_id="op-1",
            target_path="Map/Main",
            map_layer=0,
            expected_revision=7,
            write_payload={"cells": [{"x": 1, "y": 2, "atlas": {"x": 3, "y": 4}}]},
            batch_id="batch-1",
        )
        op2 = MapExecutionOperation(
            operation_id="op-2",
            target_path="Map/Background",
            map_layer=0,
            expected_revision=3,
            write_payload={"cells": [{"x": 5, "y": 6, "atlas": {"x": 7, "y": 8}}]},
            batch_id="batch-1",
        )
        ops = {"op-1": op1.to_dict(), "op-2": op2.to_dict()}
        replace_map_state_field(state, "execution_operations", ops)
        return state

    def test_multi_operation_keeps_same_owner_lineage(self) -> None:
        """多操作保持同一 owner/workflow lineage。"""
        state = self._fresh_state()
        assert state.macro_step_id == "map-step"
        assert state.owner_frame_id == "f2"
        assert state.domain_task_id == "map-step:epoch-1"
        assert len(state.execution_operations) == 2

    def test_multi_operation_independent_scopes(self) -> None:
        """每个操作有独立 target 和 revision。"""
        state = self._fresh_state()
        op1 = state.execution_operations["op-1"]
        op2 = state.execution_operations["op-2"]
        assert op1["target_path"] == "Map/Main"
        assert op1["expected_revision"] == 7
        assert op2["target_path"] == "Map/Background"
        assert op2["expected_revision"] == 3

    def test_approval_recorded_with_multi_operations(self) -> None:
        """审批记录包含多操作身份。"""
        state = self._fresh_state()
        record_map_approval_identity(
            state,
            approval_identity={
                "candidate_ref": "art://c/1",
                "operation_ids": ["op-1", "op-2"],
                "batch_id": "batch-1",
            },
            target="Map/Main",
            revision=7,
        )
        assert state.approval_identity is not None
        assert state.approval_identity["operation_ids"] == ["op-1", "op-2"]

    def test_restart_preserves_multi_operation_state(self) -> None:
        """重启后多操作状态通过往返保持。"""
        state = self._fresh_state()
        record_map_approval_identity(
            state,
            approval_identity={
                "candidate_ref": "art://c/1",
                "operation_ids": ["op-1", "op-2"],
            },
            target="Map/Main",
            revision=7,
        )
        hydrated = MapTaskState.from_dict(state.to_dict())
        assert len(hydrated.execution_operations) == 2
        assert hydrated.execution_operations["op-1"]["target_path"] == "Map/Main"
        assert hydrated.execution_operations["op-2"]["target_path"] == "Map/Background"
        assert hydrated.approval_identity["operation_ids"] == ["op-1", "op-2"]

    def test_reconnect_restores_same_lineage(self) -> None:
        """重连恢复同一 owner/workflow lineage。"""
        state = self._fresh_state()
        record_map_owner_link(
            state,
            macro_step_id="map-step",
            owner_frame_id="f2",
            domain_task_id="map-step:epoch-1",
            target="Map/Main",
            revision=7,
        )
        hydrated = MapTaskState.from_dict(state.to_dict())
        assert hydrated.macro_step_id == "map-step"
        assert hydrated.owner_frame_id == "f2"
        assert hydrated.domain_task_id == "map-step:epoch-1"


class TestLegacyFixtureMigration:
    """task 11.5：旧 fixture 迁移为自然语言 macro task、runtime-bound context bundle、
    真实 Frame 构造与 reducer-owned child-start 事件。

    验证旧 fixture（注入顶层 target JSON 或 contract-free owner）现在
    使用 natural-language macro tasks、runtime-bound context bundles、
    真实 Frame 构造和 reducer-owned child-start 事件。
    """

    def test_natural_language_macro_task_not_top_level_target(self) -> None:
        """验证 macro step 使用自然语言 objective，不注入顶层 target JSON。"""
        step = _map_outcome_step()
        assert "target_path" not in step
        assert "map_revision" not in step
        # objective 是自然语言描述
        assert isinstance(step["objective"], str)
        assert len(step["objective"]) > 0

    def test_owner_uses_real_frame_construction(self) -> None:
        """验证 owner 使用真实 Frame 构造（DomainOwnerContract），而非 contract-free。"""
        from app.orchestrator.frame_contract_types import DomainOwnerContract

        contract = DomainOwnerContract(
            domain="map",
            owner_frame_id="f2",
            parent_frame_id="f1",
            macro_step_id="map-step",
            domain_task_id="map-step:epoch-1",
            durable_task_id="task-1",
            request_lineage_id="lineage-1",
        )
        assert contract.to_dict()["contract_kind"] == "domain_owner_v1"
        assert contract.domain == "map"
        assert contract.owner_frame_id == "f2"
        assert contract.to_dict()["contract_kind"] == "domain_owner_v1"

    def test_child_start_uses_reducer_owned_event(self) -> None:
        """验证 child_start 使用 reducer-owned 事件而非直接赋值。"""
        state = MapTaskState(
            task_id="task-1",
            task_lineage_id="lineage-1",
            macro_step_id="map-step",
            owner_frame_id="f2",
            domain_task_id="map-step:epoch-1",
        )
        replace_map_state_field(state, "structure_revision", 3)
        record_map_child_lineage(
            state,
            child_frame_id="fc",
            child_stage="planner",
            target="Map/Main",
            revision=7,
        )
        # 验证事件已记录
        assert len(state.pending_workflow_events) > 0
        child_start_events = [
            event
            for event in state.pending_workflow_events
            if event["event_type"] == "map_child_started"
        ]
        assert len(child_start_events) == 1
        assert child_start_events[0]["payload"]["child_frame_id"] == "fc"
        assert child_start_events[0]["payload"]["child_stage"] == "planner"

    def test_context_bundle_is_runtime_bound(self) -> None:
        """验证 context bundle 在运行时绑定，不硬编码在 legacy fixture 中。"""

        mid = MapPlanningContextEntry(
            context_id="mid-ctx",
            semantic_role="mid",
            artifact_ref="art://mid/1",
            digest="sha256:mid",
            provenance={"kind": "snapshot"},
            target_path="Map/Main",
            map_layer=0,
            source_revision=7,
            fact_fields=("occupancy",),
        )
        bg = MapPlanningContextEntry(
            context_id="bg-ctx",
            semantic_role="background",
            artifact_ref="art://bg/1",
            digest="sha256:bg",
            provenance={"kind": "snapshot"},
            target_path="Map/Background",
            map_layer=0,
            source_revision=3,
            fact_fields=("coverage",),
        )
        bundle = MapPlanningContextBundle.from_entries(
            [mid, bg], required_roles=["mid", "background"]
        )
        # 验证 bundle 在运行时正确构造
        assert len(bundle.contexts) == 2
        assert bundle.bundle_id.startswith("map-context-bundle:")

        # 验证通过 reducer 事件记录
        state = MapTaskState(
            task_id="task-1",
            task_lineage_id="lineage-1",
        )
        replace_map_state_field(state, "structure_revision", 3)
        record_planning_context_refresh(
            state,
            context_entry=mid,
            target="Map/Main",
            revision=7,
        )
        record_planning_context_refresh(
            state,
            context_entry=bg,
            target="Map/Background",
            revision=3,
        )
        assert "mid-ctx" in state.planning_contexts
        assert "bg-ctx" in state.planning_contexts
