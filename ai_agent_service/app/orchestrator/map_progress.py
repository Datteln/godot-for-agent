"""地图任务的验收合同、验证阶段与无进展保护。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from app.orchestrator.map_contracts import MAP_RUNTIME_STAGE_TRANSITIONS
from app.orchestrator.map_workers import MAP_PLAN_TOOL_NAMES, PLATFORM_PLAN_TOOL_NAMES
from app.orchestrator.map_workflow import (
    assert_map_workflow_write_allowed,
    dispatch_map_workflow_event,
    make_map_workflow_event,
    migrate_legacy_workflow_scopes,
    replace_map_state_field,
    workflow_scope_identity,
)
from app.orchestrator.map_recovery import (
    SEMANTIC_RETRY_MAX_ATTEMPTS,
    record_semantic_retry,
    retry_pause_report,
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


@dataclass
class MapTaskState:
    """集中保存可序列化、可恢复的地图任务状态。"""

    task_id: str = ""
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
    resumed_from_checkpoint: bool = False
    pause_kind: MapTaskPauseKind = ""
    pause_reason: str = ""
    pause_report: dict[str, Any] = field(default_factory=dict)
    workflow_schema_version: int = 1
    workflow_events: list[dict[str, Any]] = field(default_factory=list)
    workflow_scopes: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    retry_registry: dict[str, dict[str, Any]] = field(default_factory=dict)
    transaction_journals: list[dict[str, Any]] = field(default_factory=list)

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
            raise ValueError(
                f"illegal map task status transition: {self.status} -> {next_status}"
            )
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
        self.structure_revision += 1
        self.plan_version += 1
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
            ("tool_failure_fingerprints", {}),
            ("approved_platform_plans", {}),
            ("region_reads", {}),
            ("region_summaries", {}),
            ("completion_blockers", []),
            ("pause_report", {}),
        ):
            replace_map_state_field(self, field_name, empty_value)
        self.context_state.clear()
        return self.structure_revision

    def start(self, task_id: str) -> None:
        """启动新任务并进入 read 阶段。

        由 start_new_task 内部调用，不直接暴露给外部调用方。
        """
        self.transition_status("running")
        self.task_id = task_id
        self.transition_stage("read")
        self.resumed_from_checkpoint = False
        self.pause_kind = ""
        self.pause_reason = ""
        replace_map_state_field(self, "pause_report", {})
        replace_map_state_field(self, "checkpoint", None)

    def start_new_task(self, task_id: str) -> None:
        """显式替换未暂停的旧任务并启动一个独立新任务。

        若当前有运行中的任务会先取消它；暂停中的任务必须先恢复或取消再替换，
        防止状态机出现"暂停中被静默替换"的歧义。
        """
        if self.status == "paused":
            raise ValueError("paused map task must be resumed or cancelled before replacement")
        if self.status == "running":
            self.cancel("replaced_by_new_task")
        self.start(task_id)

    def resume(self) -> None:
        """显式恢复已暂停任务。

        仅当 status == paused 时可用；恢复后标记 resumed_from_checkpoint，
        并清空无进展计数器避免旧 streak 误触发再次暂停。
        """
        if self.status != "paused":
            raise ValueError(f"cannot resume map task with status={self.status}")
        self.transition_status("running")
        self.resumed_from_checkpoint = True
        self.pause_kind = ""
        self.pause_reason = ""
        replace_map_state_field(self, "no_progress_streaks", {})
        replace_map_state_field(self, "pause_report", {})

    def complete(self) -> None:
        """在无 blocker 时完成运行中的地图任务。"""
        if self.status != "running":
            raise ValueError(f"cannot complete map task with status={self.status}")
        self.transition_status("completed")

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
        self.resumed_from_checkpoint = False
        replace_map_state_field(self, "pending_batches", [])
        replace_map_state_field(self, "completion_blockers", [])
        replace_map_state_field(self, "checkpoint", None)
        replace_map_state_field(self, "pause_report", {})

    def to_dict(self) -> dict[str, Any]:
        """将任务状态转换为 JSON 可序列化字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> MapTaskState:
        """从持久化字典恢复地图任务状态。"""
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
            "workflow_events",
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
        state = cls(**data)
        migrate_legacy_workflow_scopes(state)
        return state

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
        self.counters.pauses += 1
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


def map_pause_message(state: MapTaskState) -> str:
    """按类型化暂停原因生成真实且可恢复的用户提示。

    Args:
        state: 已暂停的地图任务状态。

    Returns:
        包含原因、非空报告和检查点的中文提示。
    """
    target, revision = workflow_scope_identity(state)
    pause_kind = state.pause_kind or "workflow_blocked"
    report = (
        deepcopy(state.pause_report)
        if state.pause_report
        else _minimal_pause_report(
            state,
            pause_kind=pause_kind,
            reason=state.pause_reason or pause_kind,
            target=target,
            revision=revision,
        )
    )
    prefix_by_kind = {
        "client_timeout": "地图任务因客户端等待超时已暂停。",
        "user_interrupted": "地图任务已按用户请求暂停。",
        "provider_exhausted": "地图任务因主模型与备用模型均不可用而暂停。",
        "budget_exhausted": "地图任务因执行预算耗尽已暂停。",
        "no_progress_exhausted": "地图任务因连续无进展已暂停。",
        "workflow_blocked": "地图任务因工作流阻塞已暂停。",
    }
    prefix = prefix_by_kind.get(pause_kind, prefix_by_kind["workflow_blocked"])
    checkpoint = state.checkpoint or {
        "task_id": state.task_id,
        "status": state.status,
        "stage": state.stage,
        "pause_kind": pause_kind,
    }
    return (
        f"{prefix}根因与恢复建议："
        f"{json.dumps(report, ensure_ascii=False, default=str)}；恢复检查点："
        f"{json.dumps(checkpoint, ensure_ascii=False, default=str)}"
    )


@dataclass(frozen=True)
class MapPlanOutcome:
    """表示地图规划结果是否足以安全进入写入阶段。"""

    ok: bool
    executable: bool
    blocked_reason: str | None
    error_code: str | None
    suggested_foothold: dict[str, Any] | None


def map_revision_scope_key(target: str, map_layer: int | None = None) -> str:
    """生成与 Godot 前端一致的 canonical 地图 revision 作用域键。

    格式：无图层时直接返回 target；有图层时返回 "target::map_layer=N"，
    确保不同图层的 revision 互不干扰。
    """
    normalized_target = target.strip()
    if map_layer is None:
        return normalized_target
    return f"{normalized_target}::map_layer={map_layer}"


def latest_map_revision(
    session: Session,
    target: str,
    map_layer: int | None = None,
) -> int | None:
    """优先读取目标图层 revision，兼容无图层旧会话记录。

    查找顺序：
    1. 先用 map_revision_scope_key 生成带图层的作用域键，优先匹配
    2. 若未找到且指定了图层，再检查 latest_layers 记录的图层是否匹配
    3. 最后回退到无图层的 target 键（兼容旧会话）
    """
    state: MapTaskState = session.map_task_state
    # 优先查找带图层作用域的 revision
    scoped = state.latest_revisions.get(
        map_revision_scope_key(target, map_layer)
    )
    if scoped is not None:
        return scoped
    # 若指定了图层但会话记录的图层不匹配，视为无有效 revision
    if map_layer is not None and state.latest_layers.get(target) != map_layer:
        return None
    # 回退到无图层旧格式
    return state.latest_revisions.get(target)


def parse_map_plan_outcome(tool_name: str, result: dict[str, Any]) -> MapPlanOutcome:
    """统一解析顶层及平台子规划中的执行门信息。

    Args:
        tool_name: 返回结果的地图规划工具名。
        result: 前端规划工具返回的结构化结果。

    Returns:
        归一化后的规划结果；只有满足对应工具执行门时才标记为可执行。
    """
    profile_plan_value = result.get("profile_plan")
    profile_plan = profile_plan_value if isinstance(profile_plan_value, dict) else {}

    blocked_reason_value = result.get("blocked_reason") or profile_plan.get("blocked_reason")
    blocked_reason = (
        blocked_reason_value
        if isinstance(blocked_reason_value, str) and blocked_reason_value.strip()
        else None
    )
    error_code_value = result.get("error_code") or profile_plan.get("error_code")
    error_code = (
        error_code_value if isinstance(error_code_value, str) and error_code_value.strip() else None
    )
    suggested_foothold_value = result.get("suggested_foothold") or profile_plan.get(
        "suggested_foothold"
    )
    suggested_foothold = (
        dict(suggested_foothold_value) if isinstance(suggested_foothold_value, dict) else None
    )

    ok = result.get("ok") is not False and profile_plan.get("ok") is not False
    platform_tool = tool_name in {"validate_platform_level_plan", "plan_reachable_map_growth"}
    if platform_tool:
        defaults_value = result.get("ability_used_defaults")
        if defaults_value is None:
            defaults_value = profile_plan.get("ability_used_defaults")
        if isinstance(defaults_value, list) and defaults_value:
            blocked_reason = blocked_reason or "ability_defaults_used"
        jump_graph_value = result.get("jump_graph") or profile_plan.get("jump_graph")
        if isinstance(jump_graph_value, dict) and jump_graph_value.get("passed") is False:
            blocked_reason = blocked_reason or "jump_graph_failed"
        score_value = result.get("score") or profile_plan.get("score")
        if isinstance(score_value, dict) and score_value.get("passed") is False:
            blocked_reason = blocked_reason or "score_failed"
        edit_batches_value = result.get("edit_map_batches")
        if edit_batches_value is None:
            edit_batches_value = profile_plan.get("edit_map_batches")
        if not isinstance(edit_batches_value, list) or not edit_batches_value:
            blocked_reason = blocked_reason or "empty_edit_map_batches"

    executable = ok and blocked_reason is None and error_code is None
    return MapPlanOutcome(
        ok=ok,
        executable=executable,
        blocked_reason=blocked_reason,
        error_code=error_code,
        suggested_foothold=suggested_foothold,
    )


MAP_PLATFORM_PLAN_MAX_ATTEMPTS = 2

_CONTRACT_KEYS = (
    "target_path",
    "map_layer",
    "start",
    "goal",
    "waypoints",
    "entrances",
    "exits",
    "movement_model",
    "cell_occupancy",
    "requires_support",
    "support_occupancy",
    "max_horizontal_gap",
    "max_rise",
    "max_fall",
    "max_step",
    "gravity_axis",
    "gravity_sign",
    "path_algorithm",
    "check_platform_design",
    "min_finish_buffer_width",
)


def has_completion_route(tool_args: dict[str, Any]) -> bool:
    """判断验证参数是否包含可冻结的真实路线约束。"""
    start = tool_args.get("start")
    goal = tool_args.get("goal")
    if isinstance(start, dict) and isinstance(goal, dict):
        return True

    entrances = tool_args.get("entrances")
    exits = tool_args.get("exits")
    if isinstance(entrances, list) and entrances and isinstance(exits, list) and exits:
        return True

    waypoints = tool_args.get("waypoints")
    return isinstance(waypoints, list) and len(waypoints) >= 2


def validation_mode(tool_args: dict[str, Any]) -> ValidationMode:
    """读取验证模式，并将无路线的旧调用安全降级为 diagnostic。"""
    requested_mode = tool_args.get("validation_mode")
    if requested_mode == "diagnostic":
        return "diagnostic"
    if requested_mode == "completion":
        return "completion"
    return "completion" if has_completion_route(tool_args) else "diagnostic"


def validation_contract(tool_args: dict[str, Any]) -> dict[str, Any]:
    """提取不可由模型在重试时漂移的 completion 验收字段。"""
    return {key: tool_args[key] for key in _CONTRACT_KEYS if key in tool_args}


def validation_contract_hash(tool_args: dict[str, Any]) -> str:
    """生成 completion 验收合同的稳定短指纹。"""
    encoded = json.dumps(
        validation_contract(tool_args), ensure_ascii=False, sort_keys=True, default=str
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def validation_request_fingerprint(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """生成可在前端执行前命中的验证请求指纹。"""
    payload = {
        "tool": tool_name,
        "target": _target(tool_args),
        "revision": _revision(session, tool_args),
        "args": tool_args,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def cached_validation_result(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any] | None:
    """返回完全相同 revision 与参数对应的验证缓存。"""
    if tool_name != "validate_map_region":
        return None
    fingerprint = validation_request_fingerprint(session, tool_name, tool_args)
    cached = session.map_task_state.validation_cache.get(fingerprint)
    if not isinstance(cached, dict):
        return None
    session.map_task_state.counters.validation_cache_hits += 1
    return {
        **cached,
        "cache_hit": True,
        "cache_reason": "same_revision_validation_fingerprint",
    }


def remember_validation_cache(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """保存一次真实 validate_map_region 结果供确定性复用。"""
    if tool_name != "validate_map_region":
        return
    fingerprint = validation_request_fingerprint(session, tool_name, tool_args)
    cache = dict(session.map_task_state.validation_cache)
    cache[fingerprint] = dict(result)
    while len(cache) > 64:
        cache.pop(next(iter(cache)))
    replace_map_state_field(
        session.map_task_state,
        "validation_cache",
        cache,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )


def record_no_progress(session: Session, target: str, reason: str) -> dict[str, Any] | None:
    """累计无进展事件，并在第三次时生成暂停检查点。

    map_task_state 已是唯一状态源，无需在生成检查点前额外同步。
    """
    state: MapTaskState = session.map_task_state
    scoped_target, revision = workflow_scope_identity(state, target=target)
    retry = record_semantic_retry(
        state,
        category="validation_failure",
        error_category=reason,
        root_cause=reason,
        stage=state.stage,
        target=scoped_target,
        revision=revision,
        operation={"reason": reason, "scope": target},
        threshold=SEMANTIC_RETRY_MAX_ATTEMPTS,
    )
    retry_key = str(retry["retry_key"])
    streak = int(retry["attempt"])
    streaks = dict(state.no_progress_streaks)
    streaks[retry_key] = streak
    replace_map_state_field(
        state,
        "no_progress_streaks",
        streaks,
        target=scoped_target,
        revision=revision,
    )
    state.counters.no_progress_events += 1
    if streak < SEMANTIC_RETRY_MAX_ATTEMPTS:
        return None
    report = retry_pause_report(
        state,
        stage=state.stage,
        target=scoped_target,
        revision=revision,
        last_attempt=retry,
    )
    if state.status == "running":
        return state.make_checkpoint(
            reason,
            report,
            pause_kind="no_progress_exhausted",
        )
    state.pause_kind = "no_progress_exhausted"
    state.pause_reason = reason
    replace_map_state_field(
        state,
        "pause_report",
        report,
        target=scoped_target,
        revision=revision,
    )
    checkpoint = {
        "task_id": state.task_id,
        "status": state.status,
        "stage": state.stage,
        "reason": reason,
        "pause_kind": state.pause_kind,
        "pause_report": report,
    }
    replace_map_state_field(
        state,
        "checkpoint",
        checkpoint,
        target=scoped_target,
        revision=revision,
    )
    return checkpoint


def resume_map_task(state: MapTaskState) -> None:
    """从检查点恢复任务，同时保留地图事实和批次进度。

    委托给 state.resume() 统一执行：校验状态合法性、推进 status、
    标记 resumed_from_checkpoint 并清空无进展计数器。
    """
    state.resume()


def _target(tool_args: dict[str, Any]) -> str:
    """返回验证调用的目标路径。"""
    value = tool_args.get("target_path", "")
    return value if isinstance(value, str) else ""


def _platform_plan_scope(tool_args: dict[str, Any]) -> str:
    """为平台规划修订次数生成目标与图层隔离的作用域。"""
    layer = tool_args.get("map_layer", 0)
    layer_value = layer if isinstance(layer, int) and not isinstance(layer, bool) else 0
    return f"{_target(tool_args)}::map_layer={layer_value}"


def _platform_plan_fingerprint(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """为 LLM 显式平台方案生成稳定指纹，缺少方案字段时不参与去重。"""
    platforms = tool_args.get("platforms")
    segments = tool_args.get("segments")
    if not isinstance(platforms, list) or not platforms:
        return None
    if not isinstance(segments, list) or not segments:
        return None
    payload = {
        "tool": tool_name,
        "scope": _platform_plan_scope(tool_args),
        "platforms": platforms,
        "segments": segments,
        "start": tool_args.get("start"),
        "frontier": tool_args.get("frontier"),
        "movement": {
            key: tool_args.get(key)
            for key in (
                "movement_model",
                "max_horizontal_gap",
                "max_rise",
                "max_fall",
    "gravity_axis",
    "gravity_sign",
    "frontier_axis",
    "frontier_sign",
)
            if key in tool_args
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


_TOOL_FAILURE_VOLATILE_KEYS = frozenset(
    {
        "batch_index",
        "frame_id",
        "mode",
        "plan_version",
        "task_summary",
        "worker",
        "workflow_constraints",
        "workflow_operations",
        "write_batch_id",
    }
)


def map_tool_call_fingerprint(
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """生成忽略编排元数据的稳定地图工具调用指纹。"""
    normalized = {
        key: value
        for key, value in tool_args.items()
        if key not in _TOOL_FAILURE_VOLATILE_KEYS
    }
    encoded = json.dumps(
        {"tool": tool_name, "args": normalized},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def remember_map_tool_failure(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    error_code: str,
    message: str,
) -> None:
    """记录一次地图工具失败，供后续相同调用在服务层直接阻断。"""
    fingerprint = map_tool_call_fingerprint(tool_name, tool_args)
    failures = dict(session.map_task_state.tool_failure_fingerprints)
    failures[fingerprint] = {
        "tool": tool_name,
        "error_code": error_code,
        "message": message,
    }
    while len(failures) > 128:
        failures.pop(next(iter(failures)))
    replace_map_state_field(
        session.map_task_state,
        "tool_failure_fingerprints",
        failures,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )


def repeated_map_tool_failure_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """在相同地图工具调用已失败时返回不重复下发的阻断消息。"""
    fingerprint = map_tool_call_fingerprint(tool_name, tool_args)
    failure = session.map_task_state.tool_failure_fingerprints.get(fingerprint)
    if not isinstance(failure, dict):
        return None
    return (
        "duplicate_tool_failure_blocked：相同参数的地图工具调用已经失败过，"
        "服务层不会再次下发。"
        f"previous_error_code={failure.get('error_code', 'unknown')}。"
        "必须修改资源键、操作参数或重新规划，禁止原样重试。"
    )


def map_platform_plan_call_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """在执行前拒绝超限或完全相同的平台规划提交。"""
    if tool_name not in PLATFORM_PLAN_TOOL_NAMES:
        return None
    scope = _platform_plan_scope(tool_args)
    attempts = session.map_task_state.planning_attempts.get(scope, 0)
    if attempts >= MAP_PLATFORM_PLAN_MAX_ATTEMPTS:
        return (
            f"平台规划已达到 {MAP_PLATFORM_PLAN_MAX_ATTEMPTS} 次修订上限；"
            "请停止调用工具并返回当前结构化失败原因。"
        )
    fingerprint = _platform_plan_fingerprint(tool_name, tool_args)
    if fingerprint is None:
        return None
    fingerprint_key = f"{scope}::{fingerprint}"
    if session.map_task_state.planning_fingerprints.get(fingerprint_key, 0) > 0:
        return (
            "该 platforms/segments 方案已经校验过，确定性结果不会因重复提交改变；"
            "必须根据 issues/repair_plan 修改具体平台字段。"
        )
    return None


def map_platform_plan_attempt_count(session: Session, tool_args: dict[str, Any]) -> int:
    """返回当前目标和图层已经执行的平台规划次数。"""
    state: MapTaskState = session.map_task_state
    return state.planning_attempts.get(_platform_plan_scope(tool_args), 0)


def _remember_platform_plan_attempt(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> None:
    """记录一次真实执行的平台规划及其显式方案指纹。"""
    if tool_name not in PLATFORM_PLAN_TOOL_NAMES:
        return
    scope = _platform_plan_scope(tool_args)
    state = session.map_task_state
    attempts = dict(state.planning_attempts)
    attempts[scope] = attempts.get(scope, 0) + 1
    replace_map_state_field(
        state,
        "planning_attempts",
        attempts,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )
    fingerprint = _platform_plan_fingerprint(tool_name, tool_args)
    if fingerprint is None:
        return
    fingerprint_key = f"{scope}::{fingerprint}"
    fingerprints = dict(state.planning_fingerprints)
    fingerprints[fingerprint_key] = (
        fingerprints.get(fingerprint_key, 0) + 1
    )
    replace_map_state_field(
        state,
        "planning_fingerprints",
        fingerprints,
        target=_target(tool_args),
        revision=_revision(session, tool_args),
    )


def _validation_scope(tool_args: dict[str, Any]) -> str:
    """返回隔离 TileMap 图层的验证状态键。"""
    layer = tool_args.get("map_layer", 0)
    layer_value = layer if isinstance(layer, int) and not isinstance(layer, bool) else 0
    return f"{_target(tool_args)}::map_layer={layer_value}"


def _revision(session: Session, tool_args: dict[str, Any]) -> int | None:
    """返回调用声明或会话已知的当前地图 revision。

    优先使用工具参数中的 expected_revision；否则按 target_path + map_layer
    从会话状态中查询（图层感知）。
    """
    value = tool_args.get("expected_revision")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    # 提取 map_layer 参数，用于图层感知的 revision 查询
    layer = tool_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    return latest_map_revision(session, _target(tool_args), map_layer)


def validation_call_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """拒绝同 revision 的重复 completion、重复 diagnostic 与验收条件漂移。"""
    if tool_name != "validate_map_region":
        return None
    scope = _validation_scope(tool_args)
    revision = _revision(session, tool_args)
    mode = validation_mode(tool_args)
    workflow = session.map_task_state.validation_workflows.get(scope, {})
    same_revision = workflow.get("map_revision") == revision

    if mode == "completion":
        if not has_completion_route(tool_args):
            record_no_progress(session, scope, "completion_route_missing")
            return (
                "completion 验证必须提供 start+goal、非空 entrances+exits，或至少两个 waypoints；"
                "无路线的图层/区域检查请使用 validation_mode='diagnostic'。"
            )
        contract_hash = validation_contract_hash(tool_args)
        frozen = session.map_task_state.validation_contracts.get(scope)
        if isinstance(frozen, dict) and frozen.get("hash") not in (None, contract_hash):
            record_no_progress(session, scope, "completion_contract_drift")
            return (
                "completion 验收合同已冻结；禁止修改 start/goal/waypoints/移动参数来绕过失败。"
                "请修改地图，或由用户明确提交新的验收目标。"
            )
        if same_revision and workflow.get("completion_attempted") is True:
            record_no_progress(session, scope, "completion_repeated_without_revision")
            next_stage = str(workflow.get("next_stage", "planner"))
            return (
                f"map revision {revision} 已执行过 completion 验证；确定性结果不会因重试改变。"
                f"下一阶段必须是 {next_stage}，产生新 revision 后才能再次 completion。"
            )
        return None

    if same_revision and workflow.get("diagnostic_attempted") is True:
        record_no_progress(session, scope, "diagnostic_repeated_without_revision")
        return (
            f"map revision {revision} 已完成 diagnostic；下一阶段必须是 planner，"
            "不得继续更换局部 goal 反复验证。"
        )
    if same_revision and workflow.get("next_stage") == "planner":
        record_no_progress(session, scope, "validation_repeated_before_planning")
        return f"map revision {revision} 已要求进入 planner；写入新 revision 前禁止继续验证。"
    return None


def map_write_stage_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """只允许平台写入执行同作用域内校验通过的编译批次。

    所有 validation_workflows 和 revision 查询均通过 map_task_state 统一访问，
    revision 查询使用 latest_map_revision 以支持图层感知。
    """
    target = _target(tool_args)
    scope = _validation_scope(tool_args)
    # 统一通过 map_task_state 访问验证工作流状态
    workflow = session.map_task_state.validation_workflows.get(scope, {})
    # 图层感知的 revision 查询
    layer = tool_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    revision = latest_map_revision(session, target, map_layer)
    if workflow.get("map_revision") == revision and workflow.get("next_stage") == "planner":
        record_no_progress(session, scope, "write_attempted_before_planning")
        frontier = session.map_task_state.failure_frontier or {}
        reason = str(
            frontier.get("error_code") or frontier.get("blocked_reason") or "platform_plan_required"
        )
        if reason == "entry_anchor_not_found":
            recovery = (
                "上次 validate_platform_level_plan 失败：entry_anchor_not_found。"
                "请先 describe_map_region 读取相同 target_path/map_layer 的现有连接边界，"
                "由 LLM 修订 entry_anchor/frontier.cell、首个平台和首段路线，再重新提交校验。"
            )
        else:
            recovery = (
                f"上次平台方案校验失败：{reason}。请由 LLM 根据 issues/repair_plan "
                "修改显式 platforms/segments，并重新提交 validate_platform_level_plan。"
            )
        return (
            f"map revision {revision} 的当前图层尚无可执行平台方案。{recovery}"
            f"在校验通过前禁止调用 {tool_name}；该工具只能执行校验器返回的 edit_map_batches。"
        )

    approval = session.map_task_state.approved_platform_plans.get(scope)
    if not isinstance(approval, dict):
        if _looks_like_platform_route_write(tool_name, tool_args):
            record_no_progress(session, scope, "platform_write_without_validated_plan")
            return (
                "拒绝平台路线写入：带有 platform/ground 语义的可站立瓦片必须来自 "
                "validate_platform_level_plan 校验通过后返回的 edit_map_batches。"
                "请勿让 writer 自行拼接 fill。"
            )
        return None
    if approval.get("map_revision") != revision:
        approvals = dict(session.map_task_state.approved_platform_plans)
        approvals.pop(scope, None)
        replace_map_state_field(
            session.map_task_state,
            "approved_platform_plans",
            approvals,
            target=target,
            revision=revision,
        )
        return (
            f"平台方案基于 map revision {approval.get('map_revision')}，当前 revision 为 "
            f"{revision}；旧编译批次已失效，请重新读取边界并提交 "
            "validate_platform_level_plan。"
        )
    approval = deepcopy(approval)
    batches_value = approval.get("remaining_batches")
    batches = list(batches_value) if isinstance(batches_value, list) else []
    matched_index = next(
        (
            index
            for index, batch in enumerate(batches)
            if _compiled_batch_matches(tool_name, tool_args, batch)
        ),
        None,
    )
    if matched_index is None:
        record_no_progress(session, scope, "write_not_from_validated_platform_plan")
        return (
            "拒绝平台地图写入：当前调用不是 validate_platform_level_plan 校验通过后"
            "编译出的剩余 edit_map_batches。禁止 coordinator/writer 临时拼接连续实心 "
            "fill、修改批次 operations，或执行未获批准的可站立路线。"
        )
    approved_batch = batches.pop(matched_index)
    tool_args["plan_version"] = approval.get("plan_version")
    tool_args["batch_index"] = approved_batch.get("batch_index", matched_index)
    tool_args["validated_platform_batch"] = True
    if isinstance(revision, int) and not isinstance(revision, bool):
        approval["map_revision"] = revision + 1
    approval["remaining_batches"] = batches
    approvals = dict(session.map_task_state.approved_platform_plans)
    approvals[scope] = approval
    replace_map_state_field(
        session.map_task_state,
        "approved_platform_plans",
        approvals,
        target=target,
        revision=revision,
    )
    workflow["next_stage"] = "write"
    workflows = dict(session.map_task_state.validation_workflows)
    workflows[scope] = workflow
    replace_map_state_field(
        session.map_task_state,
        "validation_workflows",
        workflows,
        target=target,
        revision=revision,
    )
    return None


def _compiled_batch_matches(
    tool_name: str,
    tool_args: dict[str, Any],
    batch: Any,
) -> bool:
    """判断实际写入是否逐字段对应一个校验器编译批次。"""
    if not isinstance(batch, dict) or batch.get("tool") != tool_name:
        return False
    expected_operations = batch.get("operations")
    actual_operations = tool_args.get("operations")
    if expected_operations != actual_operations:
        return False
    expected_cells = batch.get("expected_cells")
    return expected_cells is None or tool_args.get("expected_cells") == expected_cells


def _looks_like_platform_route_write(
    tool_name: str,
    tool_args: dict[str, Any],
) -> bool:
    """识别声明为平台或可站立地面的直接瓦片写入。"""
    if tool_name != "edit_map":
        return False
    operations = tool_args.get("operations")
    if not isinstance(operations, list):
        return False
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        semantic = str(operation.get("semantic_layer", "")).strip().lower()
        tags_value = operation.get("tags")
        tags = (
            {str(tag).strip().lower() for tag in tags_value}
            if isinstance(tags_value, list)
            else set()
        )
        if semantic == "ground" or "platform" in tags:
            return True
    return False


def platform_write_requires_validation(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> bool:
    """判断平台写入是否必须先执行或重新执行平台方案校验。

    使用 latest_map_revision 进行图层感知查询，确保不同图层的 approval
    和 revision 比较在各自作用域内进行。

    Args:
        session: 当前地图任务会话。
        tool_name: 待执行的地图写工具名。
        tool_args: 待执行工具的结构化参数。

    Returns:
        当前写入属于未批准的平台路线时返回 True，否则返回 False。
    """
    if not _looks_like_platform_route_write(tool_name, tool_args):
        return False

    target = _target(tool_args)
    scope = _validation_scope(tool_args)
    approval = session.map_task_state.approved_platform_plans.get(scope)
    if not isinstance(approval, dict):
        return True

    layer = tool_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    revision = latest_map_revision(session, target, map_layer)
    if approval.get("map_revision") != revision:
        return True

    batches_value = approval.get("remaining_batches")
    batches = batches_value if isinstance(batches_value, list) else []
    return not any(
        _compiled_batch_matches(tool_name, tool_args, batch) for batch in batches
    )


def _platform_edit_batches(result: dict[str, Any]) -> list[dict[str, Any]]:
    """提取平台校验结果中的可执行地图批次。"""
    profile_value = result.get("profile_plan")
    profile = profile_value if isinstance(profile_value, dict) else {}
    batches_value = result.get("edit_map_batches")
    if batches_value is None:
        batches_value = profile.get("edit_map_batches")
    if not isinstance(batches_value, list):
        return []
    return [deepcopy(batch) for batch in batches_value if isinstance(batch, dict)]


def remember_map_plan_progress(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """有效规划完成后允许执行阶段写入，但仍要求新 revision 后再 completion。"""
    if tool_name not in MAP_PLAN_TOOL_NAMES:
        return
    _remember_platform_plan_attempt(session, tool_name, tool_args)
    outcome = parse_map_plan_outcome(tool_name, result)
    target_value = result.get("target", result.get("target_path", _target(tool_args)))
    target = target_value if isinstance(target_value, str) else ""
    scope_args = {**tool_args, "target_path": target}
    result_layer = result.get("map_layer")
    if (
        "map_layer" not in scope_args
        and isinstance(result_layer, int)
        and not isinstance(result_layer, bool)
    ):
        scope_args["map_layer"] = result_layer
    scope = _validation_scope(scope_args)
    layer = scope_args.get("map_layer")
    map_layer = layer if isinstance(layer, int) and not isinstance(layer, bool) else None
    current_revision = latest_map_revision(session, target, map_layer)

    if not outcome.executable:
        # 规划工具可能以成功响应承载诊断结果；任何失败都必须留在规划阶段恢复。
        state = session.map_task_state
        state.transition_stage("plan")
        approvals = dict(state.approved_platform_plans)
        approvals.pop(scope, None)
        replace_map_state_field(
            state,
            "approved_platform_plans",
            approvals,
            target=target,
            revision=current_revision,
        )
        replace_map_state_field(
            state,
            "failure_frontier",
            {
                "tool": tool_name,
                "blocked_reason": outcome.blocked_reason,
                "error_code": outcome.error_code,
                "suggested_foothold": outcome.suggested_foothold,
            },
            target=target,
            revision=current_revision,
        )
        replace_map_state_field(
            state,
            "unresolved_issues",
            [{
                "kind": "map_plan_not_executable",
                "tool": tool_name,
                "blocked_reason": outcome.blocked_reason,
                "error_code": outcome.error_code,
            }],
            target=target,
            revision=current_revision,
        )
        if tool_name in PLATFORM_PLAN_TOOL_NAMES:
            workflow = session.map_task_state.validation_workflows.get(scope, {})
            workflow["map_revision"] = current_revision
            workflow["next_stage"] = "planner"
            workflow["plan_tool"] = tool_name
            workflow["plan_error_code"] = outcome.error_code or outcome.blocked_reason
            workflows = dict(session.map_task_state.validation_workflows)
            workflows[scope] = workflow
            replace_map_state_field(
                session.map_task_state,
                "validation_workflows",
                workflows,
                target=target,
                revision=current_revision,
            )
        return

    if tool_name not in PLATFORM_PLAN_TOOL_NAMES:
        locked_scope = next(
            (
                key
                for key, value in session.map_task_state.validation_workflows.items()
                if key.startswith(f"{target}::")
                and isinstance(value, dict)
                and value.get("map_revision") == current_revision
                and value.get("next_stage") == "planner"
            ),
            None,
        )
        if locked_scope is not None:
            session.map_task_state.transition_stage("plan")
            return

    active_workflow = session.map_task_state.validation_workflows.get(scope)
    if isinstance(active_workflow, dict) and active_workflow.get("next_stage") == "planner":
        if active_workflow.get("map_revision") != current_revision:
            return
        active_workflow["next_stage"] = "write"
        active_workflow["plan_tool"] = tool_name
        workflows = dict(session.map_task_state.validation_workflows)
        workflows[scope] = active_workflow
        replace_map_state_field(
            session.map_task_state,
            "validation_workflows",
            workflows,
            target=target,
            revision=current_revision,
        )
    state = session.map_task_state
    state.transition_stage("write")
    state.plan_version += 1
    replace_map_state_field(
        state,
        "failure_frontier",
        None,
        target=target,
        revision=current_revision,
    )
    replace_map_state_field(
        state,
        "unresolved_issues",
        [],
        target=target,
        revision=current_revision,
    )
    streaks = dict(state.no_progress_streaks)
    streaks[scope] = 0
    replace_map_state_field(
        state,
        "no_progress_streaks",
        streaks,
        target=target,
        revision=current_revision,
    )
    if tool_name in PLATFORM_PLAN_TOOL_NAMES:
        batches = _platform_edit_batches(result)
        for index, batch in enumerate(batches):
            batch["batch_index"] = index
        approvals = dict(state.approved_platform_plans)
        approvals[scope] = {
            "tool": tool_name,
            "map_revision": current_revision,
            "plan_version": state.plan_version,
            "remaining_batches": batches,
        }
        replace_map_state_field(
            state,
            "approved_platform_plans",
            approvals,
            target=target,
            revision=current_revision,
        )


def remember_validation_progress(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
    successful: bool,
) -> None:
    """记录一次真实验证完成后的强制下一阶段。"""
    if tool_name != "validate_map_region":
        return
    target_value = result.get("target", result.get("target_path", _target(tool_args)))
    target = target_value if isinstance(target_value, str) else ""
    scope_args = {**tool_args, "target_path": target}
    result_layer = result.get("map_layer")
    if (
        "map_layer" not in scope_args
        and isinstance(result_layer, int)
        and not isinstance(result_layer, bool)
    ):
        scope_args["map_layer"] = result_layer
    scope = _validation_scope(scope_args)
    revision_value = result.get("map_revision")
    revision = (
        revision_value
        if isinstance(revision_value, int) and not isinstance(revision_value, bool)
        else _revision(session, tool_args)
    )
    mode = validation_mode(tool_args)
    workflow = session.map_task_state.validation_workflows.get(scope, {})
    if workflow.get("map_revision") != revision:
        workflow = {"map_revision": revision}

    if mode == "completion":
        contract = validation_contract(scope_args)
        contracts = dict(session.map_task_state.validation_contracts)
        contracts.setdefault(
            scope,
            {"hash": validation_contract_hash(scope_args), "contract": contract},
        )
        replace_map_state_field(
            session.map_task_state,
            "validation_contracts",
            contracts,
            target=target,
            revision=revision,
        )
        workflow["completion_attempted"] = True
        workflow["next_stage"] = "reviewer" if successful else "diagnostic"
        session.map_task_state.transition_stage(
            "review" if successful else "diagnostic"
        )
        replace_map_state_field(
            session.map_task_state,
            "unresolved_issues",
            list(result.get("issues", [])),
            target=target,
            revision=revision,
        )
        if successful:
            replace_map_state_field(
                session.map_task_state,
                "completed_goals",
                [*session.map_task_state.completed_goals, contract],
                target=target,
                revision=revision,
            )
    else:
        workflow["diagnostic_attempted"] = True
        workflow["next_stage"] = "planner"
        session.map_task_state.transition_stage("plan")
        replace_map_state_field(
            session.map_task_state,
            "failure_frontier",
            {
                "region": result.get("region", {}),
                "issues": result.get("issues", []),
                "structured_issues": result.get("structured_issues", []),
            },
            target=target,
            revision=revision,
        )
    workflow["issues"] = result.get("issues", [])
    workflows = dict(session.map_task_state.validation_workflows)
    workflows[scope] = workflow
    replace_map_state_field(
        session.map_task_state,
        "validation_workflows",
        workflows,
        target=target,
        revision=revision,
    )
    session.map_task_state.counters.validations += 1
    streaks = dict(session.map_task_state.no_progress_streaks)
    streaks[scope] = 0
    replace_map_state_field(
        session.map_task_state,
        "no_progress_streaks",
        streaks,
        target=target,
        revision=revision,
    )


def reset_map_task_progress(
    session: Session,
    frame: Frame | None = None,
    *,
    task_id: str | None = None,
) -> None:
    """在新用户地图任务开始时重置合同、阶段和当前帧的进展周期。

    通过 state.start_new_task() 统一处理状态转换：若当前有运行中任务会先取消，
    暂停中任务会拒绝替换（必须先恢复或取消），然后启动独立新任务。
    """
    state = session.map_task_state
    state.start_new_task(task_id or f"map-{session.session_id}-{session.turn_counter + 1}")
    state.plan_version = 0
    state.counters = MapTaskCounters()
    for field_name, empty_value in (
        ("failure_frontier", None),
        ("unresolved_issues", []),
        ("completed_goals", []),
        ("pending_batches", []),
        ("executed_batches", []),
        ("validation_cache", {}),
        ("validation_contracts", {}),
        ("validation_workflows", {}),
        ("no_progress_streaks", {}),
        ("planning_attempts", {}),
        ("planning_fingerprints", {}),
        ("tool_failure_fingerprints", {}),
        ("approved_platform_plans", {}),
        ("completion_blockers", []),
    ):
        replace_map_state_field(state, field_name, empty_value)
    state.context_state.pop("reader_exhausted", None)
    replace_map_state_field(state, "checkpoint", None)
    state.resumed_from_checkpoint = False
    state.pause_reason = ""
    if frame is None:
        return
    frame.persistent_turn_count = 0
    frame.persistent_edit_map_turn_count = 0
    frame.map_progress_revision = None
