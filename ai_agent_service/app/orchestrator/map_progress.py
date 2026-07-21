"""地图任务的验收合同、验证阶段与无进展保护。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.agents.types import Frame
    from app.sessions.store import Session

ValidationMode = Literal["diagnostic", "completion"]
MapTaskStatus = Literal["idle", "running", "paused", "completed"]


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
    latest_revisions: dict[str, int] = field(default_factory=dict)
    latest_layers: dict[str, int] = field(default_factory=dict)
    region_reads: dict[str, int] = field(default_factory=dict)
    region_summaries: dict[str, dict[str, Any]] = field(default_factory=dict)
    context_state: dict[str, Any] = field(default_factory=dict)
    completion_blockers: list[dict[str, Any]] = field(default_factory=list)
    auto_iterations: int = 0
    checkpoint: dict[str, Any] | None = None
    resumed_from_checkpoint: bool = False
    pause_reason: str = ""

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
            "latest_revisions",
            "latest_layers",
            "region_reads",
            "region_summaries",
            "context_state",
        ):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        for key in (
            "unresolved_issues",
            "completed_goals",
            "pending_batches",
            "executed_batches",
            "completion_blockers",
        ):
            if not isinstance(data.get(key), list):
                data[key] = []
        if data.get("status") not in {"idle", "running", "paused", "completed"}:
            data["status"] = "idle"
        return cls(**data)

    def make_checkpoint(self, reason: str) -> dict[str, Any]:
        """生成恢复所需的最小结构化检查点并暂停任务。"""
        self.status = "paused"
        self.pause_reason = reason
        self.counters.pauses += 1
        self.checkpoint = {
            "task_id": self.task_id,
            "status": self.status,
            "stage": self.stage,
            "plan_version": self.plan_version,
            "reason": reason,
            "failure_frontier": deepcopy(self.failure_frontier),
            "unresolved_issues": deepcopy(self.unresolved_issues),
            "completed_goals": deepcopy(self.completed_goals),
            "pending_batches": deepcopy(self.pending_batches),
            "executed_batches": deepcopy(self.executed_batches),
            "latest_revisions": dict(self.latest_revisions),
            "known_regions": list(self.region_reads),
        }
        return self.checkpoint


_MAP_PLAN_TOOL_NAMES = frozenset(
    {
        "plan_map_layout",
        "plan_map_algorithms",
        "plan_platform_level",
        "plan_reachable_map_growth",
    }
)

_CONTRACT_KEYS = (
    "target_path",
    "map_layer",
    "start",
    "goal",
    "waypoints",
    "entrances",
    "exits",
    "movement_model",
    "walkable_is_filled",
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


def validation_mode(tool_args: dict[str, Any]) -> ValidationMode:
    """读取验证模式；旧调用默认保持 completion 语义。"""
    return "diagnostic" if tool_args.get("validation_mode") == "diagnostic" else "completion"


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
    session.map_task_state.validation_cache[fingerprint] = dict(result)
    while len(session.map_task_state.validation_cache) > 64:
        session.map_task_state.validation_cache.pop(
            next(iter(session.map_task_state.validation_cache))
        )


def record_no_progress(session: Session, target: str, reason: str) -> dict[str, Any] | None:
    """累计无进展事件，并在第三次时生成暂停检查点。"""
    state = session.map_task_state
    streak = state.no_progress_streaks.get(target, 0) + 1
    state.no_progress_streaks[target] = streak
    state.counters.no_progress_events += 1
    if streak < 3:
        return None
    session.sync_map_task_state()
    return state.make_checkpoint(reason)


def resume_map_task(state: MapTaskState) -> None:
    """从检查点恢复任务，同时保留地图事实和批次进度。"""
    state.status = "running"
    state.resumed_from_checkpoint = True
    state.pause_reason = ""
    state.no_progress_streaks.clear()


def _target(tool_args: dict[str, Any]) -> str:
    """返回验证调用的目标路径。"""
    value = tool_args.get("target_path", "")
    return value if isinstance(value, str) else ""


def _revision(session: Session, tool_args: dict[str, Any]) -> int | None:
    """返回调用声明或会话已知的当前地图 revision。"""
    value = tool_args.get("expected_revision")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return session.latest_map_revisions.get(_target(tool_args))


def validation_call_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """拒绝同 revision 的重复 completion、重复 diagnostic 与验收条件漂移。"""
    if tool_name != "validate_map_region":
        return None
    target = _target(tool_args)
    revision = _revision(session, tool_args)
    mode = validation_mode(tool_args)
    workflow = session.map_validation_workflows.get(target, {})
    same_revision = workflow.get("map_revision") == revision

    if mode == "completion":
        contract_hash = validation_contract_hash(tool_args)
        frozen = session.map_validation_contracts.get(target)
        if isinstance(frozen, dict) and frozen.get("hash") not in (None, contract_hash):
            record_no_progress(session, target, "completion_contract_drift")
            return (
                "completion 验收合同已冻结；禁止修改 start/goal/waypoints/移动参数来绕过失败。"
                "请修改地图，或由用户明确提交新的验收目标。"
            )
        if same_revision and workflow.get("completion_attempted") is True:
            record_no_progress(session, target, "completion_repeated_without_revision")
            next_stage = str(workflow.get("next_stage", "planner"))
            return (
                f"map revision {revision} 已执行过 completion 验证；确定性结果不会因重试改变。"
                f"下一阶段必须是 {next_stage}，产生新 revision 后才能再次 completion。"
            )
        return None

    if same_revision and workflow.get("diagnostic_attempted") is True:
        record_no_progress(session, target, "diagnostic_repeated_without_revision")
        return (
            f"map revision {revision} 已完成 diagnostic；下一阶段必须是 planner，"
            "不得继续更换局部 goal 反复验证。"
        )
    if same_revision and workflow.get("next_stage") == "planner":
        record_no_progress(session, target, "validation_repeated_before_planning")
        return f"map revision {revision} 已要求进入 planner；写入新 revision 前禁止继续验证。"
    return None


def map_write_stage_error(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str | None:
    """诊断结束后，在新规划完成前拒绝地图写入。"""
    target = _target(tool_args)
    workflow = session.map_validation_workflows.get(target, {})
    revision = session.latest_map_revisions.get(target)
    if workflow.get("map_revision") == revision and workflow.get("next_stage") == "planner":
        record_no_progress(session, target, "write_attempted_before_planning")
        return (
            f"map revision {revision} 的诊断阶段已经结束；必须先调用地图规划工具产生新方案，"
            f"再执行 {tool_name}，不能直接试写。"
        )
    return None


def remember_map_plan_progress(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """有效规划完成后允许执行阶段写入，但仍要求新 revision 后再 completion。"""
    if tool_name not in _MAP_PLAN_TOOL_NAMES or result.get("ok") is False:
        return
    blocked_reason = result.get("blocked_reason")
    if blocked_reason:
        # 规划工具可成功返回诊断结果；它不是可执行计划，必须保留规划工具以便修复后重试。
        session.map_task_state.stage = "plan"
        return
    target_value = result.get("target", result.get("target_path", _target(tool_args)))
    target = target_value if isinstance(target_value, str) else ""
    workflow = session.map_validation_workflows.get(target)
    if isinstance(workflow, dict) and workflow.get("next_stage") == "planner":
        current_revision = session.latest_map_revisions.get(target)
        if workflow.get("map_revision") != current_revision:
            return
        workflow["next_stage"] = "write"
        workflow["plan_tool"] = tool_name
        session.map_validation_workflows[target] = workflow
    session.map_task_state.stage = "write"
    session.map_task_state.plan_version += 1
    session.map_task_state.unresolved_issues.clear()
    session.map_task_state.no_progress_streaks[target] = 0


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
    revision_value = result.get("map_revision")
    revision = (
        revision_value
        if isinstance(revision_value, int) and not isinstance(revision_value, bool)
        else _revision(session, tool_args)
    )
    mode = validation_mode(tool_args)
    workflow = session.map_validation_workflows.get(target, {})
    if workflow.get("map_revision") != revision:
        workflow = {"map_revision": revision}

    if mode == "completion":
        contract = validation_contract(tool_args)
        session.map_validation_contracts.setdefault(
            target,
            {"hash": validation_contract_hash(tool_args), "contract": contract},
        )
        workflow["completion_attempted"] = True
        workflow["next_stage"] = "reviewer" if successful else "diagnostic"
        session.map_task_state.stage = "review" if successful else "diagnostic"
        session.map_task_state.unresolved_issues = list(result.get("issues", []))
        if successful:
            session.map_task_state.completed_goals.append(validation_contract(tool_args))
    else:
        workflow["diagnostic_attempted"] = True
        workflow["next_stage"] = "planner"
        session.map_task_state.stage = "plan"
        session.map_task_state.failure_frontier = {
            "region": result.get("region", {}),
            "issues": result.get("issues", []),
            "structured_issues": result.get("structured_issues", []),
        }
    workflow["issues"] = result.get("issues", [])
    session.map_validation_workflows[target] = workflow
    session.map_task_state.counters.validations += 1
    session.map_task_state.no_progress_streaks[target] = 0


def reset_map_task_progress(session: Session, frame: Frame | None = None) -> None:
    """在新用户地图任务开始时重置合同、阶段和当前帧的进展周期。"""
    state = session.map_task_state
    state.task_id = f"map-{session.session_id}-{session.turn_counter + 1}"
    state.status = "running"
    state.stage = "read"
    state.plan_version = 0
    state.counters = MapTaskCounters()
    state.failure_frontier = None
    state.unresolved_issues.clear()
    state.completed_goals.clear()
    state.pending_batches.clear()
    state.executed_batches.clear()
    state.validation_cache.clear()
    state.validation_contracts.clear()
    state.validation_workflows.clear()
    state.no_progress_streaks.clear()
    state.checkpoint = None
    state.resumed_from_checkpoint = False
    state.pause_reason = ""
    if frame is None:
        return
    frame.persistent_turn_count = 0
    frame.persistent_edit_map_turn_count = 0
    frame.map_progress_revision = None
