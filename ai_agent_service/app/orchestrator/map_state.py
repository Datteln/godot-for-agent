"""地图任务核心状态模型：类型、状态机、生命周期、检查点与共享快照定位。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import MISSING, asdict, dataclass, field, fields
from typing import Any, Final, Literal, TYPE_CHECKING
from app.orchestrator.map_contracts import MAP_RUNTIME_STAGE_TRANSITIONS
from app.orchestrator.map_workflow import (
    assert_map_workflow_write_allowed,
    dispatch_map_workflow_event,
    increment_map_counter,
    make_map_workflow_event,
    replace_map_state_field,
    workflow_scope_identity,
)
if TYPE_CHECKING:
    from app.agents.types import Frame
    from app.sessions.store import Session
ValidationMode = Literal["diagnostic", "completion"]


MapTaskStatus = Literal["idle", "running", "paused", "completed", "cancelled"]


MapTaskPauseKind = Literal[
    "",
    "no_progress_exhausted",
    "client_timeout",
    "user_interrupted",
    "provider_exhausted",
    "budget_exhausted",
    "workflow_blocked",
]


_MAP_PAUSE_KINDS: frozenset[str] = frozenset(
    {
        "",
        "no_progress_exhausted",
        "client_timeout",
        "user_interrupted",
        "provider_exhausted",
        "budget_exhausted",
        "workflow_blocked",
    }
)


# 地图任务全局合法状态转换表：
# 每个键对应的 frozenset 列出从该状态出发允许进入的下一个状态，
# 任何未列出的转换都会被 transition_status 拒绝，确保状态机单调推进。
_MAP_STATUS_TRANSITIONS: dict[MapTaskStatus, frozenset[MapTaskStatus]] = {
    "idle": frozenset({"running"}),
    "running": frozenset({"paused", "completed", "cancelled"}),
    "paused": frozenset({"running", "cancelled"}),
    "completed": frozenset({"running"}),  # 完成后可再次启动新任务
    "cancelled": frozenset({"running"}),  # 取消后可再次启动新任务
}


@dataclass
class MapTaskCounters:
    """记录地图任务的关键执行与缓存计数。"""

    llm_turns: int = 0
    reads: int = 0
    read_cache_hits: int = 0
    validations: int = 0
    validation_cache_hits: int = 0
    writes: int = 0
    executed_batches: int = 0
    failed_batches: int = 0
    revision_advances: int = 0
    no_progress_events: int = 0
    pauses: int = 0


MapTaskFieldScope = Literal["task", "revision", "context", "operation", "session"]


@dataclass(frozen=True)
class MapTaskFieldLifecycle:
    """声明一个地图状态字段的生命周期与恢复策略。"""

    scope: MapTaskFieldScope
    reset_policy: Literal["dataclass_default"] = "dataclass_default"
    resume_policy: Literal["preserve"] = "preserve"


@dataclass
class MapTaskState:
    """集中保存可序列化、可恢复的地图任务状态。"""

    task_id: str = ""
    task_lineage_id: str = ""
    status: MapTaskStatus = "idle"
    stage: str = "read"
    # 独立场景结构版本：每次场景结构发生根本性变化时递增，
    # 用于失效所有依赖旧结构的派生状态（计划、校验缓存、批次等）
    structure_revision: int = 0
    plan_version: int = 0
    counters: MapTaskCounters = field(default_factory=MapTaskCounters)
    failure_frontier: dict[str, Any] | None = None
    unresolved_issues: list[Any] = field(default_factory=list)
    completed_goals: list[Any] = field(default_factory=list)
    pending_batches: list[dict[str, Any]] = field(default_factory=list)
    executed_batches: list[dict[str, Any]] = field(default_factory=list)
    validation_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    validation_contracts: dict[str, dict[str, Any]] = field(default_factory=dict)
    validation_workflows: dict[str, dict[str, Any]] = field(default_factory=dict)
    no_progress_streaks: dict[str, int] = field(default_factory=dict)
    latest_validations: dict[str, dict[str, Any]] = field(default_factory=dict)
    validation_failure_counts: dict[str, int] = field(default_factory=dict)
    planning_attempts: dict[str, int] = field(default_factory=dict)
    planning_fingerprints: dict[str, int] = field(default_factory=dict)
    authoritative_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    planning_contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    planning_context_bundles: dict[str, dict[str, Any]] = field(default_factory=dict)
    execution_operations: dict[str, dict[str, Any]] = field(default_factory=dict)
    planning_attempt_history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    planning_publications: dict[str, dict[str, Any]] = field(default_factory=dict)
    tool_failure_fingerprints: dict[str, dict[str, Any]] = field(default_factory=dict)
    approved_platform_plans: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_revisions: dict[str, int] = field(default_factory=dict)
    latest_layers: dict[str, int] = field(default_factory=dict)
    region_reads: dict[str, int] = field(default_factory=dict)
    region_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    context_state: dict[str, Any] = field(default_factory=dict)
    completion_blockers: list[dict[str, Any]] = field(default_factory=list)
    auto_iterations: int = 0
    checkpoint: dict[str, Any] | None = None
    resume_authorization: dict[str, str] | None = None
    pause_kind: MapTaskPauseKind = ""
    pause_reason: str = ""
    pause_report: dict[str, Any] = field(default_factory=dict)
    workflow_schema_version: int = 1
    workflow_high_water_seq: int = 0
    pending_workflow_events: list[dict[str, Any]] = field(default_factory=list, repr=False)
    workflow_scopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    retry_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    plan_attempt_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    task_convergence_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    transaction_journals: list[dict[str, Any]] = field(default_factory=list)
    # 宏观计划与地图域工作流的类型化链接（task 4.1）：owner 身份、macro 步骤、
    # 域任务、子帧 lineage、owner 发布与审批身份。独立于 MacroPlanState 持久化，
    # 两者通过 (macro_step_id, domain_task_id, owner_frame_id) 稳定关联。
    macro_step_id: str = ""
    owner_frame_id: str = ""
    domain_task_id: str = ""
    child_lineage: list[dict[str, Any]] = field(default_factory=list)
    owner_publication: dict[str, Any] | None = None
    approval_identity: dict[str, Any] | None = None

    def __setattr__(self, name: str, value: Any) -> None:
        """在功能开关启用时检测 reducer-owned 字段的运行时直写。"""
        if name in self.__dict__:
            assert_map_workflow_write_allowed(name)
        super().__setattr__(name, value)

    def transition_status(self, next_status: MapTaskStatus) -> None:
        """按唯一合法转换表推进地图任务状态。

        所有状态变更必须经过此守卫，拒绝任何不在 _MAP_STATUS_TRANSITIONS 中的转换。
        """
        allowed = _MAP_STATUS_TRANSITIONS.get(self.status, frozenset())
        if next_status not in allowed:
            raise ValueError(f"illegal map task status transition: {self.status} -> {next_status}")
        self.status = next_status

    def transition_stage(self, next_stage: str) -> None:
        """按唯一合法转换表推进地图工作流阶段。

        阶段转换由 MAP_RUNTIME_STAGE_TRANSITIONS 约束，确保流水线单向推进。
        """
        dispatch_map_workflow_event(
            self,
            make_map_workflow_event(
                self,
                "stage_transition",
                "__workflow__",
                self.structure_revision,
                {"stage": next_stage},
            ),
        )

    def record_structure_change(self) -> int:
        """推进独立场景结构版本，并失效所有依赖旧结构的派生状态。

        当场景结构发生根本性变化时（如用户重新编辑地图基础结构），调用此方法：
        - 递增 structure_revision 标记结构版本
        - 递增 plan_version 使所有基于旧结构的规划失效
        - 重置 stage 为 read，强制重新读取
        - 清空所有派生缓存：failure_frontier、unresolved_issues、completed_goals、
          pending_batches、validation_cache/contracts/workflows、planning_attempts、
          approved_platform_plans、region_reads/summaries、context_state 等
        """
        next_structure_revision = self.structure_revision + 1
        replace_map_state_field(
            self,
            "structure_revision",
            next_structure_revision,
        )
        replace_map_state_field(self, "plan_version", self.plan_version + 1)
        self.transition_stage("read")
        for field_name, empty_value in (
            ("failure_frontier", None),
            ("unresolved_issues", []),
            ("completed_goals", []),
            ("pending_batches", []),
            ("validation_cache", {}),
            ("validation_contracts", {}),
            ("validation_workflows", {}),
            ("no_progress_streaks", {}),
            ("latest_validations", {}),
            ("validation_failure_counts", {}),
            ("planning_attempts", {}),
            ("planning_fingerprints", {}),
            ("authoritative_snapshots", {}),
            ("planning_contexts", {}),
            ("planning_context_bundles", {}),
            ("execution_operations", {}),
            ("planning_attempt_history", {}),
            ("planning_publications", {}),
            ("tool_failure_fingerprints", {}),
            ("approved_platform_plans", {}),
            ("region_reads", {}),
            ("region_summaries", {}),
            ("completion_blockers", []),
            ("pause_report", {}),
        ):
            replace_map_state_field(self, field_name, empty_value)
        replace_map_state_field(self, "context_state", {})
        return self.structure_revision

    def start(self, task_id: str) -> None:
        """启动新任务并进入 read 阶段。

        由 start_new_task 内部调用，不直接暴露给外部调用方。
        """
        self.start_new_task(task_id)

    def start_new_task(self, task_id: str, *, lineage_id: str = "") -> None:
        """显式替换未暂停的旧任务并启动一个独立新任务。

        若当前有运行中的任务会先取消它；暂停中的任务必须先恢复或取消再替换，
        防止状态机出现"暂停中被静默替换"的歧义。
        """
        if self.status == "paused":
            raise ValueError("paused map task must be resumed or cancelled before replacement")
        dispatch_map_workflow_event(
            self,
            make_map_workflow_event(
                self,
                "task_epoch_started",
                "__workflow__",
                0,
                {
                    "task_id": task_id,
                    "lineage_id": lineage_id,
                },
            ),
        )

    def resume(self, *, lineage_id: str | None = None) -> None:
        """显式恢复已暂停任务。

        仅当 status == paused 时可用；专用命令可同时签发绑定 lineage 的一次性
        授权，普通上下文续接则在当前请求内恢复而不签发后续授权。
        """
        if self.status != "paused":
            raise ValueError(f"cannot resume map task with status={self.status}")
        self.transition_status("running")
        authorization = (
            {"task_id": self.task_id, "lineage_id": lineage_id}
            if isinstance(lineage_id, str) and lineage_id
            else None
        )
        replace_map_state_field(self, "resume_authorization", authorization)
        self.pause_kind = ""
        self.pause_reason = ""
        replace_map_state_field(self, "no_progress_streaks", {})
        replace_map_state_field(self, "pause_report", {})

    def complete(self) -> None:
        """在无 blocker 时完成运行中的地图任务。"""
        if self.status != "running":
            raise ValueError(f"cannot complete map task with status={self.status}")
        self.transition_status("completed")
        replace_map_state_field(self, "plan_attempt_registry", {})
        replace_map_state_field(self, "task_convergence_registry", {})

    def cancel(self, reason: str) -> None:
        """取消运行中或暂停中的地图任务并冻结残留批次。

        取消后清空 pending_batches、completion_blockers 和 checkpoint，
        确保旧任务的残留数据不会继续被执行。
        """
        if self.status not in {"running", "paused"}:
            raise ValueError(f"cannot cancel map task with status={self.status}")
        self.transition_status("cancelled")
        self.pause_kind = ""
        self.pause_reason = reason
        replace_map_state_field(self, "resume_authorization", None)
        replace_map_state_field(self, "pending_batches", [])
        replace_map_state_field(self, "completion_blockers", [])
        replace_map_state_field(self, "checkpoint", None)
        replace_map_state_field(self, "pause_report", {})
        replace_map_state_field(self, "plan_attempt_registry", {})
        replace_map_state_field(self, "task_convergence_registry", {})

    def to_dict(self) -> dict[str, Any]:
        """将任务状态转换为 JSON 可序列化字典。"""
        payload = asdict(self)
        payload.pop("pending_workflow_events", None)
        return payload

    def task_epoch_reset_values(self) -> dict[str, Any]:
        """按字段生命周期元数据生成新任务 epoch 的完整默认值。"""
        declared = set(MAP_TASK_FIELD_LIFECYCLE)
        actual = {item.name for item in fields(self)}
        if declared != actual:
            missing = sorted(actual - declared)
            stale = sorted(declared - actual)
            raise RuntimeError(
                "MapTaskState lifecycle metadata mismatch: " f"missing={missing}, stale={stale}"
            )
        reset_values: dict[str, Any] = {}
        for item in fields(self):
            lifecycle = MAP_TASK_FIELD_LIFECYCLE[item.name]
            if lifecycle.scope == "session":
                continue
            if item.default_factory is not MISSING:
                reset_values[item.name] = item.default_factory()
            elif item.default is not MISSING:
                reset_values[item.name] = deepcopy(item.default)
            else:
                raise RuntimeError(f"MapTaskState field has no reset default: {item.name}")
        return reset_values

    @classmethod
    def from_dict(cls, value: Any) -> MapTaskState:
        """从持久化字典恢复地图任务状态。"""
        if isinstance(value, cls):
            raise TypeError("MapTaskState hydration accepts raw persisted dictionaries only")
        if not isinstance(value, dict):
            return cls()
        field_names = set(cls.__dataclass_fields__)
        data = {key: item for key, item in value.items() if key in field_names}
        counters = data.get("counters")
        if isinstance(counters, dict):
            counter_names = set(MapTaskCounters.__dataclass_fields__)
            data["counters"] = MapTaskCounters(
                **{
                    key: item
                    for key, item in counters.items()
                    if key in counter_names and isinstance(item, int) and not isinstance(item, bool)
                }
            )
        else:
            data["counters"] = MapTaskCounters()
        for key in (
            "validation_cache",
            "validation_contracts",
            "validation_workflows",
            "no_progress_streaks",
            "latest_validations",
            "validation_failure_counts",
            "planning_attempts",
            "planning_fingerprints",
            "authoritative_snapshots",
            "planning_contexts",
            "planning_context_bundles",
            "execution_operations",
            "planning_attempt_history",
            "planning_publications",
            "tool_failure_fingerprints",
            "approved_platform_plans",
            "latest_revisions",
            "latest_layers",
            "region_reads",
            "region_summaries",
            "context_state",
            "workflow_scopes",
            "evidence_registry",
            "retry_registry",
            "plan_attempt_registry",
            "task_convergence_registry",
            "pause_report",
        ):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        for key in (
            "unresolved_issues",
            "completed_goals",
            "pending_batches",
            "executed_batches",
            "completion_blockers",
            "pending_workflow_events",
            "transaction_journals",
        ):
            if not isinstance(data.get(key), list):
                data[key] = []
        # 校验 status 和 stage 是否在合法枚举内；不合法时回退默认值，
        # 防止旧持久化数据或损坏数据导致运行时崩溃。
        if data.get("status") not in _MAP_STATUS_TRANSITIONS:
            data["status"] = "idle"
        if data.get("stage") not in MAP_RUNTIME_STAGE_TRANSITIONS:
            data["stage"] = "read"
        if data.get("pause_kind") not in _MAP_PAUSE_KINDS:
            data["pause_kind"] = ""
        if not isinstance(data.get("resume_authorization"), dict):
            data["resume_authorization"] = None
        high_water = data.get("workflow_high_water_seq", 0)
        if isinstance(high_water, bool) or not isinstance(high_water, int) or high_water < 0:
            raise ValueError("workflow_high_water_seq must be a non-negative integer")
        data["pending_workflow_events"] = []
        return cls(**data)

    def make_checkpoint(
        self,
        reason: str,
        pause_report: dict[str, Any] | None = None,
        *,
        pause_kind: MapTaskPauseKind = "workflow_blocked",
    ) -> dict[str, Any]:
        """生成恢复所需的最小结构化检查点并暂停任务。

        通过 transition_status 进入 paused 状态，将 structure_revision 等
        关键字段连同 failure_frontier、unresolved_issues 等一并快照到 checkpoint。
        """
        self.transition_status("paused")
        self.pause_kind = pause_kind
        self.pause_reason = reason
        target, revision = workflow_scope_identity(self)
        normalized_report = (
            deepcopy(pause_report)
            if isinstance(pause_report, dict) and pause_report
            else _minimal_pause_report(
                self,
                pause_kind=pause_kind,
                reason=reason,
                target=target,
                revision=revision,
            )
        )
        replace_map_state_field(
            self,
            "pause_report",
            normalized_report,
            target=target,
            revision=revision,
        )
        increment_map_counter(self, "pauses", target=target, revision=revision)
        checkpoint = {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "structure_revision": self.structure_revision,  # 独立场景结构版本
            "plan_version": self.plan_version,
            "reason": reason,
            "pause_kind": pause_kind,
            "pause_report": deepcopy(self.pause_report),
            "failure_frontier": deepcopy(self.failure_frontier),
            "unresolved_issues": deepcopy(self.unresolved_issues),
            "completed_goals": deepcopy(self.completed_goals),
            "pending_batches": deepcopy(self.pending_batches),
            "executed_batches": deepcopy(self.executed_batches),
            "latest_revisions": dict(self.latest_revisions),
            "authoritative_snapshots": deepcopy(self.authoritative_snapshots),
            "planning_contexts": deepcopy(self.planning_contexts),
            "planning_context_bundles": deepcopy(self.planning_context_bundles),
            "execution_operations": deepcopy(self.execution_operations),
            "planning_attempt_history": deepcopy(self.planning_attempt_history),
            "planning_publications": deepcopy(self.planning_publications),
            "approved_platform_plans": deepcopy(self.approved_platform_plans),
            "known_regions": list(self.region_reads),
        }
        dispatch_map_workflow_event(
            self,
            make_map_workflow_event(
                self,
                "checkpoint_replaced",
                target,
                revision,
                {"checkpoint": checkpoint},
            ),
        )
        return checkpoint


MAP_TASK_FIELD_LIFECYCLE: Final[dict[str, MapTaskFieldLifecycle]] = {
    "task_id": MapTaskFieldLifecycle("task"),
    "task_lineage_id": MapTaskFieldLifecycle("task"),
    "status": MapTaskFieldLifecycle("task"),
    "stage": MapTaskFieldLifecycle("task"),
    "structure_revision": MapTaskFieldLifecycle("task"),
    "plan_version": MapTaskFieldLifecycle("task"),
    "counters": MapTaskFieldLifecycle("task"),
    "failure_frontier": MapTaskFieldLifecycle("task"),
    "unresolved_issues": MapTaskFieldLifecycle("task"),
    "completed_goals": MapTaskFieldLifecycle("task"),
    "pending_batches": MapTaskFieldLifecycle("task"),
    "executed_batches": MapTaskFieldLifecycle("task"),
    "validation_cache": MapTaskFieldLifecycle("revision"),
    "validation_contracts": MapTaskFieldLifecycle("revision"),
    "validation_workflows": MapTaskFieldLifecycle("revision"),
    "no_progress_streaks": MapTaskFieldLifecycle("revision"),
    "latest_validations": MapTaskFieldLifecycle("revision"),
    "validation_failure_counts": MapTaskFieldLifecycle("revision"),
    "planning_attempts": MapTaskFieldLifecycle("task"),
    "planning_fingerprints": MapTaskFieldLifecycle("task"),
    "authoritative_snapshots": MapTaskFieldLifecycle("revision"),
    "planning_contexts": MapTaskFieldLifecycle("context"),
    "planning_context_bundles": MapTaskFieldLifecycle("task"),
    "execution_operations": MapTaskFieldLifecycle("operation"),
    "planning_attempt_history": MapTaskFieldLifecycle("task"),
    "planning_publications": MapTaskFieldLifecycle("task"),
    "tool_failure_fingerprints": MapTaskFieldLifecycle("task"),
    "approved_platform_plans": MapTaskFieldLifecycle("revision"),
    "latest_revisions": MapTaskFieldLifecycle("revision"),
    "latest_layers": MapTaskFieldLifecycle("revision"),
    "region_reads": MapTaskFieldLifecycle("revision"),
    "region_summaries": MapTaskFieldLifecycle("revision"),
    "context_state": MapTaskFieldLifecycle("task"),
    "completion_blockers": MapTaskFieldLifecycle("revision"),
    "auto_iterations": MapTaskFieldLifecycle("task"),
    "checkpoint": MapTaskFieldLifecycle("task"),
    "resume_authorization": MapTaskFieldLifecycle("task"),
    "pause_kind": MapTaskFieldLifecycle("task"),
    "pause_reason": MapTaskFieldLifecycle("task"),
    "pause_report": MapTaskFieldLifecycle("task"),
    "workflow_schema_version": MapTaskFieldLifecycle("session"),
    "workflow_high_water_seq": MapTaskFieldLifecycle("session"),
    "pending_workflow_events": MapTaskFieldLifecycle("session"),
    "workflow_scopes": MapTaskFieldLifecycle("revision"),
    "evidence_registry": MapTaskFieldLifecycle("revision"),
    "retry_registry": MapTaskFieldLifecycle("task"),
    "plan_attempt_registry": MapTaskFieldLifecycle("task"),
    "task_convergence_registry": MapTaskFieldLifecycle("task"),
    "transaction_journals": MapTaskFieldLifecycle("task"),
    "macro_step_id": MapTaskFieldLifecycle("task"),
    "owner_frame_id": MapTaskFieldLifecycle("task"),
    "domain_task_id": MapTaskFieldLifecycle("task"),
    "child_lineage": MapTaskFieldLifecycle("task"),
    "owner_publication": MapTaskFieldLifecycle("task"),
    "approval_identity": MapTaskFieldLifecycle("task"),
}


@dataclass(frozen=True)
class MapPlanOutcome:
    """表示地图规划结果是否足以安全进入写入阶段。"""

    ok: bool
    executable: bool
    blocked_reason: str | None
    error_code: str | None
    suggested_foothold: dict[str, Any] | None


def resume_map_task(
    state: MapTaskState,
    *,
    lineage_id: str | None = None,
) -> None:
    """从检查点恢复任务，同时保留地图事实和批次进度。

    委托给 state.resume() 统一执行：校验状态合法性、推进 status，并按需
    签发绑定当前 task/lineage 的一次性恢复授权。
    """
    state.resume(lineage_id=lineage_id)


def reset_map_task_progress(
    session: Session,
    frame: Frame | None = None,
    *,
    task_id: str | None = None,
    lineage_id: str = "",
) -> None:
    """在新用户地图任务开始时重置合同、阶段和当前帧的进展周期。

    通过单个 task_epoch_started reducer 事件按生命周期元数据完整初始化状态。
    """
    state = session.map_task_state
    state.start_new_task(
        task_id or f"map-{session.session_id}-{session.turn_counter + 1}",
        lineage_id=lineage_id,
    )
    if frame is None:
        return
    frame.persistent_turn_count = 0
    frame.persistent_edit_map_turn_count = 0
    frame.map_progress_revision = None


def _minimal_pause_report(
    state: MapTaskState,
    *,
    pause_kind: MapTaskPauseKind,
    reason: str,
    target: str,
    revision: int,
) -> dict[str, Any]:
    """从任务状态合成始终非空的最小暂停恢复报告。

    Args:
        state: 当前地图任务状态。
        pause_kind: 类型化暂停原因。
        reason: 具体暂停原因或错误分类。
        target: 当前工作流目标。
        revision: 当前目标 revision。

    Returns:
        可持久化且可直接展示的结构化恢复报告。
    """
    recovery_by_kind = {
        "client_timeout": "确认服务仍可用后发送“继续任务”，从当前检查点恢复。",
        "user_interrupted": "需要继续时发送“继续任务”，或使用专用恢复命令。",
        "provider_exhausted": "检查主模型与备用模型配置和连通性后恢复任务。",
        "budget_exhausted": "缩小任务范围或提高允许预算后从检查点恢复。",
        "no_progress_exhausted": "根据未解决问题补齐输入或调整方案后恢复。",
        "workflow_blocked": "处理未解决问题后从检查点恢复。",
        "": "检查暂停原因后从检查点恢复。",
    }
    return {
        "pause_kind": pause_kind,
        "reason": reason,
        "stage": state.stage,
        "target": target,
        "revision": revision,
        "unresolved_issues": deepcopy(state.unresolved_issues),
        "recovery": recovery_by_kind[pause_kind],
    }
