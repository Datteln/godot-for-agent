"""宏观计划契约的序列化与规范性校验回归测试。"""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.orchestrator.plan_scheduler import PlanGraph
from app.orchestrator.macro_contracts import (
    DOMAIN_OWNER_RESULT_SCHEMA,
    MACRO_PLAN_SCHEMA,
    DisplayMilestone,
    DomainOwnerResult,
    DomainOwnerStatus,
    MacroPlan,
    MacroPlanError,
    MacroPlanMigration,
    MacroPlanState,
    MacroPlanStep,
    OwnerDispatchDecision,
    OwnerDispatchKey,
    MacroApprovalOutcome,
    MacroApprovalRequest,
    PredecessorBinding,
    StageCheckpoint,
    classify_legacy_plan_migration,
    bind_macro_inputs,
    derive_macro_step_status_from_child,
    required_stage_for_map_operation,
    resolve_macro_approval,
    resolve_owner_dispatch,
)


def _map_step(*, step_id: str = "expand", **overrides: Any) -> dict[str, Any]:
    """构造一个合法的宏观地图步骤字典。"""
    base: dict[str, Any] = {
        "id": step_id,
        "owner_agent": "map-agent",
        "domain": "map",
        "objective": "向右扩展关卡并交付可通关预览",
        "acceptance_criteria": ["向右扩展约40格", "适配当前移动能力"],
        "depends_on": [],
        "predecessor_bindings": [],
        "display_milestones": [
            {"id": "m-read", "title": "获取地图事实", "kind": "read"},
            {"id": "m-plan", "title": "设计并校验路线", "kind": "plan"},
        ],
    }
    base.update(overrides)
    return base


def _map_plan(steps: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    """构造一个合法的宏观计划字典。"""
    base: dict[str, Any] = {"summary": "扩建当前关卡", "steps": steps}
    base.update(overrides)
    return base


class TestMacroPlanSerialization:
    """宏观计划与步骤的 from_dict/to_dict 往返与字段保持。"""

    def test_round_trip_preserves_all_fields(self) -> None:
        """合法计划的序列化往返应保持全部字段。"""
        raw = _map_plan([_map_step()])
        plan = MacroPlan.from_dict(raw)
        assert plan.plan_kind == MACRO_PLAN_SCHEMA
        back = MacroPlan.from_dict(plan.to_dict())
        assert back.to_dict() == plan.to_dict()

    def test_round_trip_preserves_dependencies_and_bindings(self) -> None:
        """跨域依赖与前置绑定在往返后保持稳定。"""
        raw = _map_plan(
            [
                {
                    "id": "code",
                    "owner_agent": "programming-agent",
                    "domain": "code",
                    "objective": "实现冲刺能力",
                },
                _map_step(
                    step_id="map",
                    depends_on=["code"],
                    predecessor_bindings=[
                        {"name": "dash_params", "source_step_id": "code", "source_path": "outputs.dash"}
                    ],
                ),
            ]
        )
        plan = MacroPlan.from_dict(raw)
        step = plan.step("map")
        assert step.depends_on == ("code",)
        assert step.predecessor_bindings[0].source_step_id == "code"
        assert MacroPlan.from_dict(plan.to_dict()).to_dict() == plan.to_dict()

    def test_legacy_agent_task_aliases_accepted(self) -> None:
        """兼容期允许 agent/task 作为 owner_agent/objective 的别名。"""
        step = MacroPlanStep.from_dict(
            {"id": "s", "agent": "map-agent", "task": "do something"}, 0
        )
        assert step.owner_agent == "map-agent"
        assert step.objective == "do something"
        assert step.domain == "code"


class TestDisplayMilestoneSerialization:
    """展示里程碑序列化与不可执行性。"""

    def test_milestone_round_trip(self) -> None:
        """里程碑往返保持字段，kind 缺省为 None。"""
        milestone = DisplayMilestone(id="m-read", title="获取地图事实", kind="read")
        back = DisplayMilestone.from_dict(milestone.to_dict())
        assert back == milestone
        assert DisplayMilestone.from_dict(
            {"id": "m", "title": "t"}
        ).kind is None

    def test_milestone_requires_id_and_title(self) -> None:
        """缺少 id 或 title 的里程碑应被拒绝。"""
        with pytest.raises(MacroPlanError, match="display milestone"):
            DisplayMilestone.from_dict({"id": "", "title": "t"})
        with pytest.raises(MacroPlanError, match="display milestone"):
            DisplayMilestone.from_dict({"id": "m", "title": ""})


class TestDomainOwnerResultSerialization:
    """域 owner 发布结果的序列化与终态判定。"""

    def test_result_round_trip(self) -> None:
        """owner 结果往返保持 outputs 与 artifact_refs。"""
        result = DomainOwnerResult(
            owner_frame_id="f1",
            domain_task_id="dt1",
            macro_step_id="expand",
            status="awaiting_confirmation",
            outputs={"preview": "art://preview/1"},
            artifact_refs=("art://snapshot/1",),
            recovery_disposition="resume_after_approval",
        )
        back = DomainOwnerResult.from_dict(result.to_dict())
        assert back.to_dict() == result.to_dict()
        assert back.schema == DOMAIN_OWNER_RESULT_SCHEMA

    @pytest.mark.parametrize(
        "status,terminal",
        [
            ("preview_ready", False),
            ("awaiting_confirmation", False),
            ("completed", True),
            ("blocked", True),
            ("cancelled", True),
            ("failed", True),
        ],
    )
    def test_terminal_status_classification(self, status: str, terminal: bool) -> None:
        """终态发布应被识别为宏观步骤终态来源。"""
        result = DomainOwnerResult(
            owner_frame_id="f",
            domain_task_id="d",
            macro_step_id="s",
            status=cast(DomainOwnerStatus, status),
        )
        assert result.is_terminal is terminal

    def test_unknown_status_rejected(self) -> None:
        """未知 owner 状态应被拒绝。"""
        with pytest.raises(MacroPlanError, match="unknown domain owner status"):
            DomainOwnerResult.from_dict(
                {
                    "owner_frame_id": "f",
                    "domain_task_id": "d",
                    "macro_step_id": "s",
                    "status": "finished",
                }
            )


class TestMacroPlanValidation:
    """宏观计划的规范性校验：依赖、绑定、DAG 与责任越界。"""

    def test_empty_steps_rejected(self) -> None:
        """空步骤计划应被拒绝。"""
        with pytest.raises(MacroPlanError, match="cannot be empty"):
            MacroPlan.from_dict({"summary": "s", "steps": []})

    def test_duplicate_step_ids_rejected(self) -> None:
        """重复步骤 id 应被拒绝。"""
        with pytest.raises(MacroPlanError, match="ids must be unique"):
            MacroPlan.from_dict(
                {"summary": "s", "steps": [_map_step(step_id="a"), _map_step(step_id="a")]}
            )

    def test_cycle_rejected(self) -> None:
        """依赖环应被拒绝。"""
        with pytest.raises(MacroPlanError, match="DAG"):
            MacroPlan.from_dict(
                {
                    "summary": "s",
                    "steps": [
                        {
                            "id": "a",
                            "owner_agent": "programming-agent",
                            "domain": "code",
                            "objective": "o",
                            "depends_on": ["b"],
                        },
                        {
                            "id": "b",
                            "owner_agent": "programming-agent",
                            "domain": "code",
                            "objective": "o",
                            "depends_on": ["a"],
                        },
                    ],
                }
            )

    def test_self_dependency_rejected(self) -> None:
        """自依赖应被拒绝。"""
        with pytest.raises(MacroPlanError, match="depends on itself"):
            MacroPlan.from_dict(
                {"summary": "s", "steps": [_map_step(step_id="a", depends_on=["a"])]}
            )

    def test_unknown_dependency_rejected(self) -> None:
        """引用不存在的依赖应被拒绝。"""
        with pytest.raises(MacroPlanError, match="unknown dependencies"):
            MacroPlan.from_dict(
                {"summary": "s", "steps": [_map_step(step_id="a", depends_on=["ghost"])]}
            )

    def test_binding_source_must_be_dependency(self) -> None:
        """前置绑定来源必须是已声明的依赖。"""
        with pytest.raises(MacroPlanError, match="must be a dependency"):
            MacroPlan.from_dict(
                {
                    "summary": "s",
                    "steps": [
                        _map_step(
                            step_id="a",
                            predecessor_bindings=[
                                {"name": "f", "source_step_id": "ghost"}
                            ],
                        )
                    ],
                }
            )

    def test_duplicate_milestone_ids_rejected(self) -> None:
        """同一步骤内重复里程碑 id 应被拒绝。"""
        with pytest.raises(MacroPlanError, match="duplicate display milestones"):
            MacroPlan.from_dict(
                {
                    "summary": "s",
                    "steps": [
                        _map_step(
                            display_milestones=[
                                {"id": "m", "title": "a", "kind": "read"},
                                {"id": "m", "title": "b", "kind": "plan"},
                            ]
                        )
                    ],
                }
            )

    def test_unknown_domain_rejected(self) -> None:
        """未知领域应被拒绝。"""
        with pytest.raises(MacroPlanError, match="unknown macro step domain"):
            MacroPlanStep.from_dict(
                {"id": "s", "owner_agent": "x", "domain": "quantum", "objective": "o"}, 0
            )

    @pytest.mark.parametrize("forbidden", ["worker_spec", "stage_id", "mode", "approved_batch"])
    def test_internal_fields_rejected(self, forbidden: str) -> None:
        """specialist 内部构造字段应被拒绝，避免责任越界。"""
        raw = _map_step()
        raw[forbidden] = {} if forbidden in {"worker_spec", "approved_batch"} else "x"
        with pytest.raises(MacroPlanError, match="specialist-internal"):
            MacroPlanStep.from_dict(raw, 0)


class TestMacroPlanAccessors:
    """宏观计划的步骤访问与不可变替换。"""

    def test_step_lookup(self) -> None:
        """step 查找返回正确步骤。"""
        plan = MacroPlan.from_dict(_map_plan([_map_step(step_id="expand")]))
        assert plan.step("expand").owner_agent == "map-agent"
        with pytest.raises(MacroPlanError, match="unknown macro step"):
            plan.step("missing")

    def test_replace_step_returns_new_plan(self) -> None:
        """替换步骤返回新计划并保持校验。"""
        plan = MacroPlan.from_dict(_map_plan([_map_step(step_id="expand")]))
        updated = plan.replace_step("expand", status="running")
        assert updated.step("expand").status == "running"
        assert plan.step("expand").status == "pending"
        assert updated.to_dict()["steps"][0]["status"] == "running"


class TestMacroPlanSiblingInvariant:
    """单一开放地图任务至多一个 map-agent owner 的不变性。"""

    def test_multiple_map_owners_rejected_at_contract(self) -> None:
        """同一宏观计划含多个 map-agent owner 步骤应被拒绝。"""
        with pytest.raises(MacroPlanError, match="at most one map-domain outcome"):
            MacroPlan.from_dict(
                {
                    "summary": "s",
                    "steps": [
                        {
                            "id": "read",
                            "owner_agent": "map-agent",
                            "domain": "map",
                            "objective": "read facts",
                        },
                        {
                            "id": "plan",
                            "owner_agent": "map-agent",
                            "domain": "map",
                            "objective": "plan route",
                        },
                    ],
                }
            )

    def test_cross_domain_single_map_owner_accepted(self) -> None:
        """代码 + 地图跨域计划只有一个 map owner，应被接受。"""
        plan = MacroPlan.from_dict(
            {
                "summary": "s",
                "steps": [
                    {
                        "id": "code",
                        "owner_agent": "programming-agent",
                        "domain": "code",
                        "objective": "implement dash",
                    },
                    _map_step(step_id="map", depends_on=["code"]),
                ],
            }
        )
        assert len(plan.steps) == 2


class TestLegacyPlanMigration:
    """遗留多 sibling 地图计划的类型化迁移结论。"""

    def test_multi_sibling_legacy_plan_requires_migration(self) -> None:
        """含多个 map-agent owner 的遗留计划应被分类为需迁移。"""
        legacy = {
            "summary": "s",
            "steps": [
                {"id": "read", "agent": "map-agent", "task": "读取地图"},
                {"id": "plan", "agent": "map-agent", "task": "规划路线"},
                {"id": "write", "agent": "map-agent", "task": "写入批次"},
            ],
        }
        migration = classify_legacy_plan_migration(legacy)
        assert migration is not None
        assert migration.disposition == "regenerate_as_macro_v2"
        assert migration.map_owner_count == 3
        assert set(migration.map_owner_step_ids) == {"read", "plan", "write"}
        assert migration.suggested_owner_agent == "map-agent"
        assert migration.legacy_step_count == 3

    def test_macro_v2_plan_not_migrated(self) -> None:
        """macro_v2 计划不需要迁移。"""
        plan = MacroPlan.from_dict(_map_plan([_map_step()]))
        assert classify_legacy_plan_migration(plan.to_dict()) is None

    def test_stashed_macro_plan_not_migrated(self) -> None:
        """已 stash macro_plan 子结构的计划不需要迁移。"""
        legacy_with_macro = {
            "summary": "s",
            "plan_kind": "legacy",
            "steps": [{"id": "a", "agent": "map-agent", "task": "t"}],
            "macro_plan": MacroPlan.from_dict(_map_plan([_map_step()])).to_dict(),
        }
        assert classify_legacy_plan_migration(legacy_with_macro) is None

    def test_single_map_owner_not_migrated(self) -> None:
        """只有一个 map owner 的遗留计划不需要迁移。"""
        legacy = {
            "summary": "s",
            "steps": [
                {"id": "code", "agent": "programming-agent", "task": "code"},
                {"id": "map", "agent": "map-agent", "task": "expand"},
            ],
        }
        assert classify_legacy_plan_migration(legacy) is None

    def test_classifier_uses_no_natural_language(self) -> None:
        """分类器只按 owner/domain 计数，不解析 task 文本推断阶段。"""
        single_owner_with_plan_keyword = {
            "summary": "s",
            "steps": [
                {"id": "a", "agent": "map-agent", "task": "规划路线并写入"},
            ],
        }
        assert (
            classify_legacy_plan_migration(single_owner_with_plan_keyword) is None
        )


class TestMacroPlanState:
    """宏观计划调度状态：owner 身份、发布状态与展示里程碑分离视图。"""

    def _state(self) -> MacroPlanState:
        """构造一个含展示里程碑的宏观计划调度状态。"""
        return MacroPlanState.from_plan(
            MacroPlan.from_dict(
                _map_plan(
                    [
                        _map_step(
                            step_id="expand",
                            display_milestones=[
                                {"id": "m-read", "title": "获取地图事实", "kind": "read"},
                                {"id": "m-plan", "title": "设计路线", "kind": "plan"},
                            ],
                        )
                    ]
                )
            )
        )

    def test_round_trip(self) -> None:
        """调度状态序列化往返保持一致。"""
        state = self._state()
        assert MacroPlanState.from_dict(state.to_dict()).to_dict() == state.to_dict()

    def test_set_owner_records_identity(self) -> None:
        """set_owner 记录 owner Frame 与持久域任务身份。"""
        state = self._state().set_owner(
            "expand", owner_frame_id="f1", domain_task_id="dt1"
        )
        step = state.step("expand")
        assert step.owner_frame_id == "f1"
        assert step.domain_task_id == "dt1"
        assert step.status == "running"

    def test_publish_advances_status(self) -> None:
        """发布终态结果推进宏观步骤状态，非终态保持 running。"""
        state = self._state().set_owner(
            "expand", owner_frame_id="f1", domain_task_id="dt1"
        )
        awaiting = DomainOwnerResult(
            owner_frame_id="f1",
            domain_task_id="dt1",
            macro_step_id="expand",
            status="awaiting_confirmation",
        )
        state = state.publish("expand", awaiting)
        assert state.owner_status("expand") == "awaiting_confirmation"
        assert state.step("expand").status == "running"
        completed = DomainOwnerResult(
            owner_frame_id="f1",
            domain_task_id="dt1",
            macro_step_id="expand",
            status="completed",
        )
        state = state.publish("expand", completed)
        assert state.step("expand").status == "succeeded"

    def test_milestones_separate_from_scheduler(self) -> None:
        """展示里程碑以扁平视图独立于调度图返回。"""
        milestones = self._state().milestones()
        assert len(milestones) == 2
        assert milestones[0][0] == "expand"
        assert milestones[0][1].id == "m-read"

    def test_owner_status_none_before_publish(self) -> None:
        """未发布时 owner 状态为 None。"""
        assert self._state().owner_status("expand") is None


class TestOwnerDispatch:
    """create-or-resume owner 调度决策。"""

    def _state(self) -> MacroPlanState:
        """构造一个单 map owner 步骤的调度状态。"""
        return MacroPlanState.from_plan(
            MacroPlan.from_dict(_map_plan([_map_step(step_id="expand")]))
        )

    def test_create_decision_when_no_owner(self) -> None:
        """无既有 owner 时返回 create 决策并派生 domain_task_id。"""
        decision = resolve_owner_dispatch(
            self._state(), "expand", session_epoch=7, durable_task_id="dt-root"
        )
        assert decision.action == "create"
        assert decision.owner_frame_id is None
        assert decision.dispatch_key.session_epoch == 7
        assert decision.dispatch_key.durable_task_id == "dt-root"
        assert decision.dispatch_key.domain == "map"
        assert decision.domain_task_id == "expand:7"

    def test_resume_decision_after_owner_recorded(self) -> None:
        """记录 owner 后再次解析返回 resume 既有 owner。"""
        state = self._state().set_owner(
            "expand", owner_frame_id="f1", domain_task_id="expand:7"
        )
        decision = resolve_owner_dispatch(
            state, "expand", session_epoch=7, durable_task_id="dt-root"
        )
        assert decision.action == "resume"
        assert decision.owner_frame_id == "f1"
        assert decision.domain_task_id == "expand:7"
        assert decision.dispatch_key.key == "7:dt-root:map:expand:7"

    def test_dispatch_key_round_trip(self) -> None:
        """调度键序列化往返保持稳定。"""
        key = OwnerDispatchKey(
            session_epoch=3,
            durable_task_id="dt",
            domain="map",
            domain_task_id="expand:3",
        )
        assert OwnerDispatchKey.from_dict(key.to_dict()) == key


class TestSuccessorBinding:
    """前置 owner 发布结果绑定：只消费声明字段，拒绝私有内部子结果。"""

    def _published_state(self) -> MacroPlanState:
        """构造一个 code→map 依赖、code owner 已发布结果的调度状态。"""
        return MacroPlanState.from_plan(
            MacroPlan.from_dict(
                {
                    "summary": "s",
                    "steps": [
                        {
                            "id": "code",
                            "owner_agent": "programming-agent",
                            "domain": "code",
                            "objective": "实现冲刺",
                        },
                        {
                            "id": "map",
                            "owner_agent": "map-agent",
                            "domain": "map",
                            "objective": "扩建",
                            "depends_on": ["code"],
                            "predecessor_bindings": [
                                {
                                    "name": "dash_params",
                                    "source_step_id": "code",
                                    "source_path": "outputs.dash",
                                }
                            ],
                        },
                    ],
                }
            )
        ).publish(
            "code",
            DomainOwnerResult(
                owner_frame_id="fc",
                domain_task_id="code:1",
                macro_step_id="code",
                status="completed",
                outputs={"dash": {"speed": 8}},
                artifact_refs=("art://dash/1",),
            ),
        )

    def test_bind_declared_output(self) -> None:
        """绑定消费 owner 发布的声明输出字段。"""
        inputs = bind_macro_inputs(self._published_state(), "map")
        assert isinstance(inputs, dict)
        assert inputs == {"dash_params": {"speed": 8}}

    def test_bind_fails_when_predecessor_unpublished(self) -> None:
        """前置未发布时 required 绑定返回 dependency_binding_failed。"""
        state = MacroPlanState.from_plan(
            MacroPlan.from_dict(
                {
                    "summary": "s",
                    "steps": [
                        {
                            "id": "code",
                            "owner_agent": "programming-agent",
                            "domain": "code",
                            "objective": "o",
                        },
                        {
                            "id": "map",
                            "owner_agent": "map-agent",
                            "domain": "map",
                            "objective": "o",
                            "depends_on": ["code"],
                            "predecessor_bindings": [
                                {
                                    "name": "x",
                                    "source_step_id": "code",
                                    "source_path": "outputs.x",
                                }
                            ],
                        },
                    ],
                }
            )
        )
        result = bind_macro_inputs(state, "map")
        assert isinstance(result, str)
        assert result.startswith("dependency_binding_failed")

    def test_bind_rejects_private_internal_result(self) -> None:
        """绑定指向私有内部子结果（非声明字段）应被拒绝。"""
        state = self._published_state()
        # 改 map 步骤的绑定路径为非声明的私有字段
        state = MacroPlanState.from_plan(
            state.plan.replace_step(
                "map",
                predecessor_bindings=(
                    PredecessorBinding(
                        name="leak",
                        source_step_id="code",
                        source_path="planner.candidate",
                    ),
                ),
            )
        )
        result = bind_macro_inputs(state, "map")
        assert isinstance(result, str)
        assert result.startswith("dependency_binding_failed")

    def test_optional_binding_skipped_when_unpublished(self) -> None:
        """optional 绑定在前置未发布时跳过而非失败。"""
        state = MacroPlanState.from_plan(
            MacroPlan.from_dict(
                {
                    "summary": "s",
                    "steps": [
                        {
                            "id": "code",
                            "owner_agent": "programming-agent",
                            "domain": "code",
                            "objective": "o",
                        },
                        {
                            "id": "map",
                            "owner_agent": "map-agent",
                            "domain": "map",
                            "objective": "o",
                            "depends_on": ["code"],
                            "predecessor_bindings": [
                                {
                                    "name": "x",
                                    "source_step_id": "code",
                                    "source_path": "outputs.x",
                                    "required": False,
                                }
                            ],
                        },
                    ],
                }
            )
        )
        result = bind_macro_inputs(state, "map")
        assert result == {}


class TestMacroTransitions:
    """宏观步骤终态只由 owner 发布驱动，子帧完成不直接完成宏观步骤。"""

    def _owned_state(self) -> MacroPlanState:
        """构造一个已记录 owner 身份、处于 running 的宏观步骤。"""
        return MacroPlanState.from_plan(
            MacroPlan.from_dict(_map_plan([_map_step(step_id="expand")]))
        ).set_owner("expand", owner_frame_id="f1", domain_task_id="expand:1")

    def test_child_completion_without_publication_keeps_running(self) -> None:
        """子帧输出不含 owner 发布时，宏观步骤保持 running。"""
        state = self._owned_state()
        updated = derive_macro_step_status_from_child(
            state, "expand", {"summary": "child did some work"}
        )
        assert updated.step("expand").status == "running"
        assert updated.step("expand").result is None

    def test_publication_awaiting_keeps_non_terminal(self) -> None:
        """awaiting_confirmation 发布保持非终态。"""
        state = self._owned_state()
        updated = derive_macro_step_status_from_child(
            state,
            "expand",
            {
                "domain_owner_result": {
                    "owner_frame_id": "f1",
                    "domain_task_id": "expand:1",
                    "macro_step_id": "expand",
                    "status": "awaiting_confirmation",
                }
            },
        )
        assert updated.step("expand").status == "running"
        assert updated.owner_status("expand") == "awaiting_confirmation"

    def test_publication_completed_transitions_terminal(self) -> None:
        """completed 发布推进宏观步骤到 succeeded 终态。"""
        state = self._owned_state()
        updated = derive_macro_step_status_from_child(
            state,
            "expand",
            {
                "domain_owner_result": {
                    "owner_frame_id": "f1",
                    "domain_task_id": "expand:1",
                    "macro_step_id": "expand",
                    "status": "completed",
                    "outputs": {"revision": 9},
                }
            },
        )
        assert updated.step("expand").status == "succeeded"
        assert updated.step("expand").result is not None
        assert updated.step("expand").result.outputs == {"revision": 9}

    def test_terminal_step_cannot_be_republished(self) -> None:
        """已终态的宏观步骤不能再被发布。"""
        state = self._owned_state()
        completed = derive_macro_step_status_from_child(
            state,
            "expand",
            {
                "domain_owner_result": {
                    "owner_frame_id": "f1",
                    "domain_task_id": "expand:1",
                    "macro_step_id": "expand",
                    "status": "completed",
                }
            },
        )
        # 再次发布 completed 被静默忽略（derive 捕获 macro_step_already_terminal）
        republished = derive_macro_step_status_from_child(
            completed,
            "expand",
            {
                "domain_owner_result": {
                    "owner_frame_id": "f1",
                    "domain_task_id": "expand:1",
                    "macro_step_id": "expand",
                    "status": "blocked",
                }
            },
        )
        assert republished.step("expand").status == "succeeded"

    def test_publish_directly_rejects_terminal_republication(self) -> None:
        """直接 publish 已终态步骤应抛 macro_step_already_terminal。"""
        state = self._owned_state()
        completed = state.publish(
            "expand",
            DomainOwnerResult(
                owner_frame_id="f1",
                domain_task_id="expand:1",
                macro_step_id="expand",
                status="completed",
            ),
        )
        with pytest.raises(MacroPlanError, match="already terminal"):
            completed.publish(
                "expand",
                DomainOwnerResult(
                    owner_frame_id="f1",
                    domain_task_id="expand:1",
                    macro_step_id="expand",
                    status="blocked",
                ),
            )


class TestSchedulerInvariants:
    """调度器不变性：展示里程碑不成为 PlanGraph 节点，子帧完成不完成宏观步骤。"""

    def test_milestones_never_become_plan_graph_nodes(self) -> None:
        """含展示里程碑的宏观计划投影到 PlanGraph 时，里程碑不成为可执行节点。"""
        macro_plan = MacroPlan.from_dict(
            _map_plan(
                [
                    _map_step(
                        step_id="expand",
                        display_milestones=[
                            {"id": "m-read", "title": "获取地图事实", "kind": "read"},
                            {"id": "m-plan", "title": "设计路线", "kind": "plan"},
                            {"id": "m-write", "title": "写入", "kind": "write"},
                        ],
                    )
                ]
            )
        )
        # 里程碑是步骤字段，不是独立步骤
        assert len(macro_plan.steps) == 1
        assert len(macro_plan.steps[0].display_milestones) == 3
        # 投影到 legacy PlanGraph 时，步骤数仍为 1，里程碑 id 不出现在 step id 集合
        graph = PlanGraph.from_dict(
            {
                "summary": macro_plan.summary,
                "steps": [
                    {
                        "id": s.step_id,
                        "title": s.objective,
                        "agent": s.owner_agent,
                        "task": s.objective,
                        "depends_on": list(s.depends_on),
                    }
                    for s in macro_plan.steps
                ],
            }
        )
        assert len(graph.steps) == 1
        graph_step_ids = {s.step_id for s in graph.steps}
        assert {"m-read", "m-plan", "m-write"}.isdisjoint(graph_step_ids)

    def test_internal_child_completion_never_completes_macro(self) -> None:
        """owner 子帧完成但未发布时，宏观步骤保持 running，不进入终态。"""
        state = MacroPlanState.from_plan(
            MacroPlan.from_dict(_map_plan([_map_step(step_id="expand")]))
        ).set_owner("expand", owner_frame_id="f1", domain_task_id="expand:1")
        # 模拟内部子阶段（reader/planner）完成但未携带 owner 发布
        for child_output in (
            {"summary": "reader done"},
            {"result": {"stage": "planner", "candidate": "x"}},
            {"result": {"stage": "validator", "passed": False}},
        ):
            state = derive_macro_step_status_from_child(
                state, "expand", child_output
            )
        assert state.step("expand").status == "running"
        assert state.owner_status("expand") is None


class TestMapOperationRouting:
    """地图内部确定性 operation 到 stage 的路由，不靠自然语言推断。"""

    @pytest.mark.parametrize(
        "operation,stage",
        [
            ("collect_map_facts", "reader"),
            ("build_authoritative_snapshot", "reader"),
            ("generate_semantic_plan", "planner"),
            ("validate_and_compile", "validator"),
            ("publish_plan", "planner"),
            ("await_approval", "orchestrator"),
            ("execute_approved_batches", "writer"),
            ("verify_map_result", "reviewer"),
        ],
    )
    def test_operation_routes_to_stage(self, operation: str, stage: str) -> None:
        """每个确定性 operation 映射到所需 map_stage。"""
        assert required_stage_for_map_operation(operation) == stage

    def test_unknown_operation_returns_none(self) -> None:
        """未知 operation 不授予任何 stage。"""
        assert required_stage_for_map_operation("plan_route_freely") is None


class TestMacroApproval:
    """审批路由到持久 owner，拒绝 stale 审批，不创建 sibling、不写入。"""

    def _awaiting_state(self) -> MacroPlanState:
        """构造一个 owner 已发布 awaiting_confirmation 的宏观步骤。"""
        return MacroPlanState.from_plan(
            MacroPlan.from_dict(_map_plan([_map_step(step_id="expand")]))
        ).set_owner("expand", owner_frame_id="f1", domain_task_id="expand:1").publish(
            "expand",
            DomainOwnerResult(
                owner_frame_id="f1",
                domain_task_id="expand:1",
                macro_step_id="expand",
                status="awaiting_confirmation",
                outputs={"preview": "art://preview/1"},
            ),
        )

    def test_valid_approval_matches_owner(self) -> None:
        """匹配 owner/epoch/awaiting 的审批被批准。"""
        outcome = resolve_macro_approval(
            self._awaiting_state(),
            MacroApprovalRequest(
                macro_step_id="expand",
                owner_frame_id="f1",
                domain_task_id="expand:1",
                session_epoch=7,
                approved=True,
            ),
            session_epoch=7,
        )
        assert outcome.disposition == "approved"
        assert outcome.owner_frame_id == "f1"

    def test_owner_mismatch_rejected_stale(self) -> None:
        """owner 不匹配的审批被拒绝为 stale。"""
        outcome = resolve_macro_approval(
            self._awaiting_state(),
            MacroApprovalRequest(
                macro_step_id="expand",
                owner_frame_id="other-owner",
                domain_task_id="expand:1",
                session_epoch=7,
                approved=True,
            ),
            session_epoch=7,
        )
        assert outcome.disposition == "rejected_stale"

    def test_epoch_mismatch_rejected_stale(self) -> None:
        """旧 epoch 的审批被拒绝为 stale。"""
        outcome = resolve_macro_approval(
            self._awaiting_state(),
            MacroApprovalRequest(
                macro_step_id="expand",
                owner_frame_id="f1",
                domain_task_id="expand:1",
                session_epoch=6,
                approved=True,
            ),
            session_epoch=7,
        )
        assert outcome.disposition == "rejected_stale"

    def test_user_rejection(self) -> None:
        """用户显式拒绝返回 rejected_by_user。"""
        outcome = resolve_macro_approval(
            self._awaiting_state(),
            MacroApprovalRequest(
                macro_step_id="expand",
                owner_frame_id="f1",
                domain_task_id="expand:1",
                session_epoch=7,
                approved=False,
            ),
            session_epoch=7,
        )
        assert outcome.disposition == "rejected_by_user"

    def test_not_awaiting_rejected(self) -> None:
        """步骤未处于 awaiting 时审批被拒绝（不误授权写入）。"""
        state = MacroPlanState.from_plan(
            MacroPlan.from_dict(_map_plan([_map_step(step_id="expand")]))
        ).set_owner("expand", owner_frame_id="f1", domain_task_id="expand:1")
        outcome = resolve_macro_approval(
            state,
            MacroApprovalRequest(
                macro_step_id="expand",
                owner_frame_id="f1",
                domain_task_id="expand:1",
                session_epoch=7,
                approved=True,
            ),
            session_epoch=7,
        )
        assert outcome.disposition == "rejected_not_awaiting"

    def test_unknown_step_rejected_stale(self) -> None:
        """未知步骤的审批被拒绝为 stale。"""
        outcome = resolve_macro_approval(
            self._awaiting_state(),
            MacroApprovalRequest(
                macro_step_id="ghost",
                owner_frame_id="f1",
                domain_task_id="expand:1",
                session_epoch=7,
                approved=True,
            ),
            session_epoch=7,
        )
        assert outcome.disposition == "rejected_stale"


class TestStageCheckpoint:
    """阶段提交边界检查点：已提交机器事实的幂等身份。"""

    def test_round_trip(self) -> None:
        """检查点序列化往返保持稳定。"""
        cp = StageCheckpoint(
            session_epoch=3, turn_id="t1", request_id="r1", stage_digest="abc123"
        )
        assert StageCheckpoint.from_dict(cp.to_dict()) == cp

    def test_idempotency_key_stable(self) -> None:
        """同一 epoch/turn/digest 的检查点幂等键一致。"""
        cp = StageCheckpoint(2, "t1", "r1", "digest")
        assert cp.idempotency_key == "stage:2:t1:digest"
        same = StageCheckpoint(2, "t1", "r1", "digest")
        assert cp.idempotency_key == same.idempotency_key

    def test_different_digest_breaks_idempotency(self) -> None:
        """digest 变化（事实真实变化）打破幂等，需重新提交。"""
        cp1 = StageCheckpoint(2, "t1", "r1", "digest-a")
        cp2 = StageCheckpoint(2, "t1", "r1", "digest-b")
        assert cp1.idempotency_key != cp2.idempotency_key

    def test_provisional_output_not_in_checkpoint(self) -> None:
        """检查点只携带已提交机器事实身份，不含 provisional 文本/reasoning。"""
        cp = StageCheckpoint(2, "t1", "r1", "digest")
        persisted = cp.to_dict()
        assert "text" not in persisted
        assert "reasoning" not in persisted
        assert set(persisted.keys()) == {
            "session_epoch",
            "turn_id",
            "request_id",
            "stage_digest",
            "idempotency_key",
        }
