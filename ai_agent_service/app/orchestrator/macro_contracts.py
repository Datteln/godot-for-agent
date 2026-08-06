"""宏观计划契约：领域级成果步骤、展示里程碑与域 owner 发布结果。

宏观计划（`macro_plan_v2`）只描述 coordinator 层面的领域成果与跨域依赖，
不携带 specialist 内部的 worker_spec、阶段、工具或重试策略。领域 owner 通过
`domain_owner_result_v1` 发布类型化结果，驱动宏观步骤状态转换。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Final, Literal, TypeAlias, cast

from app.orchestrator.runtime_contracts import PlanStepStatus

MACRO_PLAN_SCHEMA: Final = "macro_plan_v2"
DOMAIN_OWNER_RESULT_SCHEMA: Final = "domain_owner_result_v1"

# 域 owner 发布的状态：preview_ready/awaiting_confirmation 为非终态，
# completed/blocked/cancelled/failed 为宏观步骤终态来源。
DomainOwnerStatus: TypeAlias = Literal[
    "preview_ready",
    "awaiting_confirmation",
    "completed",
    "blocked",
    "cancelled",
    "failed",
]
_TERMINAL_OWNER_STATUSES: Final = frozenset(
    {"completed", "blocked", "cancelled", "failed"}
)

# 宏观步骤禁止携带的 specialist 内部构造字段。出现即视为责任越界，
# 拒绝构造 worker Frame。
MACRO_FORBIDDEN_FIELDS: Final = frozenset(
    {
        "worker_spec",
        "stage_id",
        "map_stage",
        "mode",
        "operations",
        "input_schema",
        "output_schema",
        "approved_batch",
        "authoritative_snapshot",
    }
)
_VALID_DOMAINS: Final = frozenset({"map", "code", "resource", "scene"})


class MacroPlanError(ValueError):
    """宏观计划结构、依赖或契约校验失败。"""

    def __init__(self, message: str, *, code: str = "macro_plan_invalid") -> None:
        """初始化带类型化 code 的校验错误。

        Args:
            message: 人类可读的错误描述。
            code: 供 normalization 层翻译为类型化拒绝结果的稳定 code。
        """
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DisplayMilestone:
    """宏观步骤的展示用里程碑，仅用于 UI，不参与调度。

    里程碑不携带调度状态、依赖、尝试、Frame 或工具权限；`kind` 仅为 UI
    匹配提示，无任何执行语义。
    """

    id: str
    title: str
    kind: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DisplayMilestone:
        """从持久化或创建参数恢复展示里程碑。"""
        milestone_id = str(value.get("id", "")).strip()
        title = str(value.get("title", "")).strip()
        if not milestone_id or not title:
            raise MacroPlanError(
                "display milestone requires id and title",
                code="invalid_milestone",
            )
        kind_value = value.get("kind")
        return cls(
            id=milestone_id,
            title=title,
            kind=(str(kind_value).strip() or None) if kind_value is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 原生结构。"""
        return {"id": self.id, "title": self.title, "kind": self.kind}


@dataclass(frozen=True)
class PredecessorBinding:
    """把前置 owner 发布的声明字段或 artifact 引用绑定为后继 owner 输入。

    绑定只能消费前置 owner 的 `domain_owner_result_v1` 声明输出或 artifact
    引用，不能直接访问某个域内部子结果。
    """

    name: str
    source_step_id: str
    source_path: str = ""
    required: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> PredecessorBinding:
        """从持久化或创建参数恢复前置绑定。"""
        name = str(value.get("name", "")).strip()
        source_step_id = str(value.get("source_step_id", "")).strip()
        if not name or not source_step_id:
            raise MacroPlanError(
                "predecessor binding requires name and source_step_id",
                code="invalid_binding",
            )
        return cls(
            name=name,
            source_step_id=source_step_id,
            source_path=str(value.get("source_path", "")).strip(),
            required=value.get("required") is not False,
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 原生结构。"""
        return {
            "name": self.name,
            "source_step_id": self.source_step_id,
            "source_path": self.source_path,
            "required": self.required,
        }


@dataclass(frozen=True)
class DomainOwnerResult:
    """领域 owner 发布的类型化结果，驱动宏观步骤状态转换。

    内部子阶段完成不能直接完成宏观步骤；只有 owner 发布终态结果
    （completed/blocked/cancelled/failed）才决定宏观步骤终态。
    """

    owner_frame_id: str
    domain_task_id: str
    macro_step_id: str
    status: DomainOwnerStatus
    schema: str = DOMAIN_OWNER_RESULT_SCHEMA
    outputs: dict[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    recovery_disposition: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DomainOwnerResult:
        """从持久化或 owner 发布结果恢复域 owner 结果。"""
        owner_frame_id = str(value.get("owner_frame_id", "")).strip()
        domain_task_id = str(value.get("domain_task_id", "")).strip()
        macro_step_id = str(value.get("macro_step_id", "")).strip()
        status = str(value.get("status", "")).strip()
        if not owner_frame_id or not domain_task_id or not macro_step_id:
            raise MacroPlanError(
                "domain owner result requires owner_frame_id, domain_task_id "
                "and macro_step_id",
                code="invalid_owner_result",
            )
        if status not in _OWNER_STATUS_VALUES:
            raise MacroPlanError(
                f"unknown domain owner status: {status!r}",
                code="invalid_owner_status",
            )
        outputs_value = value.get("outputs", {})
        return cls(
            schema=str(value.get("schema", DOMAIN_OWNER_RESULT_SCHEMA)),
            owner_frame_id=owner_frame_id,
            domain_task_id=domain_task_id,
            macro_step_id=macro_step_id,
            status=cast(DomainOwnerStatus, status),
            outputs=dict(outputs_value) if isinstance(outputs_value, dict) else {},
            artifact_refs=tuple(
                str(item)
                for item in value.get("artifact_refs", [])
                if isinstance(item, str)
            ),
            recovery_disposition=(
                str(value["recovery_disposition"]).strip() or None
                if value.get("recovery_disposition") is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化或供后继步骤绑定的发布结果。"""
        return {
            "schema": self.schema,
            "owner_frame_id": self.owner_frame_id,
            "domain_task_id": self.domain_task_id,
            "macro_step_id": self.macro_step_id,
            "status": self.status,
            "outputs": dict(self.outputs),
            "artifact_refs": list(self.artifact_refs),
            "recovery_disposition": self.recovery_disposition,
        }

    @property
    def is_terminal(self) -> bool:
        """返回该发布是否为宏观步骤终态来源。"""
        return self.status in _TERMINAL_OWNER_STATUSES


_OWNER_STATUS_VALUES: Final = frozenset(
    {"preview_ready", "awaiting_confirmation", "completed", "blocked", "cancelled", "failed"}
)


@dataclass(frozen=True)
class MacroPlanStep:
    """一个领域 owner 负责的可执行宏观成果。

    步骤只描述目标、验收、依赖与展示里程碑；不携带 specialist 内部的
    worker_spec、阶段、工具或重试策略。owner_frame_id 与 domain_task_id
    由调度器在创建/恢复 owner 时写入。
    """

    step_id: str
    order: int
    owner_agent: str
    domain: str
    objective: str
    acceptance_criteria: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    predecessor_bindings: tuple[PredecessorBinding, ...] = ()
    display_milestones: tuple[DisplayMilestone, ...] = ()
    status: PlanStepStatus = "pending"
    owner_frame_id: str | None = None
    domain_task_id: str | None = None
    result: DomainOwnerResult | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any], order: int) -> MacroPlanStep:
        """从创建参数或持久化记录恢复宏观步骤。

        出现 `worker_spec` 等 specialist 内部构造字段时直接拒绝，避免
        coordinator 把领域内部工作流漏到宏观计划层。
        """
        forbidden = {
            key
            for key in (MACRO_FORBIDDEN_FIELDS & set(value.keys()))
            if value.get(key) is not None
        }
        if forbidden:
            raise MacroPlanError(
                f"macro step {value.get('id', order)} must not carry "
                f"specialist-internal fields: {sorted(forbidden)}",
                code="internal_field_rejected",
            )
        step_id = str(value.get("id", value.get("step_id", f"step-{order + 1}"))).strip()
        if not step_id:
            step_id = f"step-{order + 1}"
        owner_agent = str(value.get("owner_agent", value.get("agent", ""))).strip()
        domain = str(value.get("domain", "")).strip()
        objective = str(value.get("objective", value.get("task", ""))).strip()
        if not step_id or not owner_agent or not objective:
            raise MacroPlanError(
                "macro step requires id, owner_agent and objective",
                code="invalid_step",
            )
        if domain and domain not in _VALID_DOMAINS:
            raise MacroPlanError(
                f"unknown macro step domain: {domain!r}",
                code="invalid_domain",
            )
        milestones_value = value.get("display_milestones", [])
        raw_result = value.get("result")
        return cls(
            step_id=step_id,
            order=order,
            owner_agent=owner_agent,
            domain=domain or "code",
            objective=objective,
            acceptance_criteria=tuple(
                str(item).strip()
                for item in value.get("acceptance_criteria", [])
                if isinstance(item, str) and item.strip()
            ),
            depends_on=tuple(
                dict.fromkeys(
                    str(item).strip()
                    for item in value.get("depends_on", [])
                    if isinstance(item, str) and item.strip()
                )
            ),
            predecessor_bindings=tuple(
                PredecessorBinding.from_dict(item)
                for item in value.get("predecessor_bindings", [])
                if isinstance(item, dict)
            ),
            display_milestones=tuple(
                DisplayMilestone.from_dict(item)
                for item in milestones_value
                if isinstance(item, dict)
            ),
            status=_coerce_macro_status(value.get("status")),
            owner_frame_id=(
                str(value["owner_frame_id"]).strip()
                if value.get("owner_frame_id") is not None
                else None
            ),
            domain_task_id=(
                str(value["domain_task_id"]).strip()
                if value.get("domain_task_id") is not None
                else None
            ),
            result=(
                DomainOwnerResult.from_dict(raw_result)
                if isinstance(raw_result, dict)
                else None
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的宏观步骤记录。"""
        return {
            "id": self.step_id,
            "owner_agent": self.owner_agent,
            "domain": self.domain,
            "objective": self.objective,
            "acceptance_criteria": list(self.acceptance_criteria),
            "depends_on": list(self.depends_on),
            "predecessor_bindings": [
                binding.to_dict() for binding in self.predecessor_bindings
            ],
            "display_milestones": [
                milestone.to_dict() for milestone in self.display_milestones
            ],
            "status": self.status,
            "owner_frame_id": self.owner_frame_id,
            "domain_task_id": self.domain_task_id,
            "result": self.result.to_dict() if self.result is not None else None,
        }


@dataclass(frozen=True)
class MacroPlan:
    """coordinator 拥有的宏观计划，只含领域级成果步骤与跨域依赖。"""

    summary: str
    steps: tuple[MacroPlanStep, ...]
    plan_kind: str = MACRO_PLAN_SCHEMA

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MacroPlan:
        """恢复并完整校验一个宏观计划 DAG。"""
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise MacroPlanError("macro plan steps cannot be empty", code="empty_plan")
        plan = cls(
            plan_kind=str(value.get("plan_kind", MACRO_PLAN_SCHEMA)),
            summary=str(value.get("summary", "")).strip(),
            steps=tuple(
                MacroPlanStep.from_dict(item, index)
                for index, item in enumerate(raw_steps)
                if isinstance(item, dict)
            ),
        )
        plan._validate()
        return plan

    def _validate(self) -> None:
        """校验步骤字段、依赖引用、绑定来源与 DAG 无环性。

        单一开放地图任务的 sibling owner 不变性需要调度器持久化上下文，
        由域调度层（task 1.4）执行，不在纯模型层断言。
        """
        if not self.steps:
            raise MacroPlanError("macro plan steps cannot be empty", code="empty_plan")
        ids = [step.step_id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise MacroPlanError("macro step ids must be unique", code="duplicate_step_id")
        known = set(ids)
        for step in self.steps:
            unknown = set(step.depends_on) - known
            if unknown:
                raise MacroPlanError(
                    f"macro step {step.step_id} has unknown dependencies: {sorted(unknown)}",
                    code="unknown_dependency",
                )
            if step.step_id in step.depends_on:
                raise MacroPlanError(
                    f"macro step {step.step_id} depends on itself",
                    code="self_dependency",
                )
            for binding in step.predecessor_bindings:
                if binding.source_step_id not in step.depends_on:
                    raise MacroPlanError(
                        f"predecessor binding source {binding.source_step_id} must be a "
                        f"dependency of {step.step_id}",
                        code="binding_source_not_dependency",
                    )
            milestone_ids = [milestone.id for milestone in step.display_milestones]
            if len(set(milestone_ids)) != len(milestone_ids):
                raise MacroPlanError(
                    f"macro step {step.step_id} has duplicate display milestones",
                    code="duplicate_milestone",
                )

        # 单一开放地图任务只能有一个 map-agent owner。创建期无持久 task id，
        # 故按“同一宏观计划至多一个 map owner 步骤”保守判定；持久化 task id
        # 粒度的精化由域调度层（task 2.x）补充。
        map_owner_ids = [
            step.step_id
            for step in self.steps
            if step.owner_agent == "map-agent" or step.domain == "map"
        ]
        if len(map_owner_ids) > 1:
            raise MacroPlanError(
                "a macro plan may own at most one map-domain outcome; collapse sibling "
                f"map-agent steps into one domain-owned outcome: {map_owner_ids}",
                code="multiple_map_owners",
            )

        visiting: set[str] = set()
        visited: set[str] = set()
        by_id = {step.step_id: step for step in self.steps}

        def visit(step_id: str) -> None:
            """深度优先检测依赖环。"""
            if step_id in visiting:
                raise MacroPlanError("macro dependencies must form a DAG", code="cycle")
            if step_id in visited:
                return
            visiting.add(step_id)
            for dependency in by_id[step_id].depends_on:
                visit(dependency)
            visiting.discard(step_id)
            visited.add(step_id)

        for step_id in ids:
            visit(step_id)

    def to_dict(self) -> dict[str, Any]:
        """转换为 Session 可直接持久化的宏观计划。"""
        return {
            "plan_kind": self.plan_kind,
            "summary": self.summary,
            "steps": [step.to_dict() for step in self.steps],
        }

    def step(self, step_id: str) -> MacroPlanStep:
        """返回指定 id 的宏观步骤。"""
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise MacroPlanError(f"unknown macro step: {step_id}", code="unknown_step")

    def replace_step(self, step_id: str, **updates: Any) -> MacroPlan:
        """返回用更新字段替换指定步骤后的新宏观计划。"""
        current = self.step(step_id)
        new_step = replace(current, **updates)
        new_steps = tuple(
            new_step if step.step_id == step_id else step for step in self.steps
        )
        new_plan = MacroPlan(plan_kind=self.plan_kind, summary=self.summary, steps=new_steps)
        new_plan._validate()
        return new_plan


def _coerce_macro_status(value: Any) -> PlanStepStatus:
    """把任意输入规整为合法的宏观步骤状态，非法时回退 pending。"""
    if value in _PLAN_STEP_STATUS_VALUES:
        return cast(PlanStepStatus, value)
    return "pending"


_PLAN_STEP_STATUS_VALUES: Final = frozenset(
    {"pending", "running", "succeeded", "failed", "blocked", "cancelled"}
)
_TERMINAL_STEP_STATUSES: Final = frozenset(
    {"succeeded", "failed", "blocked", "cancelled"}
)


@dataclass(frozen=True)
class MacroPlanMigration:
    """遗留多 sibling 地图计划需要迁移为 macro_v2 的类型化结论。

    仅描述迁移结论本身；实际的 pause/regenerate 路由由域调度层（task 2.x/4.x）
    消费此结论后执行。
    """

    disposition: Literal["regenerate_as_macro_v2"]
    reason: str
    legacy_step_count: int
    map_owner_count: int
    map_owner_step_ids: tuple[str, ...]
    suggested_owner_agent: str = "map-agent"

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化或供 UI 展示的迁移结论。"""
        return {
            "disposition": self.disposition,
            "reason": self.reason,
            "legacy_step_count": self.legacy_step_count,
            "map_owner_count": self.map_owner_count,
            "map_owner_step_ids": list(self.map_owner_step_ids),
            "suggested_owner_agent": self.suggested_owner_agent,
        }


def classify_legacy_plan_migration(plan_dict: Any) -> MacroPlanMigration | None:
    """识别遗留多 sibling 地图计划并返回类型化迁移结论。

    不做自然语言阶段推断：仅当计划缺少 macro_v2 标记且含多个 map-agent owner
    步骤时返回迁移结论；否则返回 None，表示可按各自 loader 继续执行。
    """
    if not isinstance(plan_dict, dict):
        return None
    if str(plan_dict.get("plan_kind", "")).strip() == MACRO_PLAN_SCHEMA:
        return None
    macro_field = plan_dict.get("macro_plan")
    if isinstance(macro_field, dict) and str(
        macro_field.get("plan_kind", "")
    ).strip() == MACRO_PLAN_SCHEMA:
        return None
    raw_steps = plan_dict.get("steps")
    if not isinstance(raw_steps, list):
        return None
    map_owner_step_ids: list[str] = []
    for index, raw in enumerate(raw_steps):
        if not isinstance(raw, dict):
            continue
        owner = str(raw.get("owner_agent", raw.get("agent", ""))).strip()
        domain = str(raw.get("domain", "")).strip()
        if owner == "map-agent" or domain == "map":
            step_id = str(
                raw.get("id", raw.get("step_id", f"step-{index + 1}"))
            ).strip()
            map_owner_step_ids.append(step_id)
    if len(map_owner_step_ids) <= 1:
        return None
    return MacroPlanMigration(
        disposition="regenerate_as_macro_v2",
        reason="legacy plan schedules multiple sibling map-agent owners for one map task",
        legacy_step_count=len(raw_steps),
        map_owner_count=len(map_owner_step_ids),
        map_owner_step_ids=tuple(map_owner_step_ids),
    )


# owner 发布状态到宏观步骤 PlanStepStatus 的基础映射；完整转换校验见 task 2.4。
_OWNER_STATUS_TO_STEP_STATUS: Final = {
    "preview_ready": "running",
    "awaiting_confirmation": "running",
    "completed": "succeeded",
    "blocked": "blocked",
    "cancelled": "cancelled",
    "failed": "failed",
}

# 地图内部确定性 operation 到所需 map_stage 的路由。服务端按 operation 创建/恢复
# 正确子智能体与合同，不靠自然语言推断阶段（design §4）。
MacroMapOperation: TypeAlias = Literal[
    "collect_map_facts",
    "build_authoritative_snapshot",
    "generate_semantic_plan",
    "validate_and_compile",
    "publish_plan",
    "await_approval",
    "execute_approved_batches",
    "verify_map_result",
]
_MAP_OPERATION_REQUIRED_STAGE: Final[dict[str, str]] = {
    "collect_map_facts": "reader",
    "build_authoritative_snapshot": "reader",
    "generate_semantic_plan": "planner",
    "validate_and_compile": "validator",
    "publish_plan": "planner",
    "await_approval": "orchestrator",
    "execute_approved_batches": "writer",
    "verify_map_result": "reviewer",
}


def required_stage_for_map_operation(operation: str) -> str | None:
    """返回地图确定性 operation 所要求的 map_stage，未知 operation 返回 None。

    用于在创建子帧前断言 operation 与 stage 匹配，避免靠 task 文本推断阶段。
    """
    return _MAP_OPERATION_REQUIRED_STAGE.get(operation)


@dataclass(frozen=True)
class StageCheckpoint:
    """阶段提交边界检查点：已提交机器事实的身份，供续接幂等与恢复复用。

    一个有效工具批次在 LLM 续接前提交后产生此检查点；后续续接、重连或重试
    据此判断是否需要重新应用前批工具结果、artifact、reducer 事件、approval
    或 owner 发布。provisional 文本/reasoning 不属于检查点范围。
    """

    session_epoch: int
    turn_id: str
    request_id: str
    stage_digest: str

    @property
    def idempotency_key(self) -> str:
        """返回供幂等判断的稳定键。"""
        return f"stage:{self.session_epoch}:{self.turn_id}:{self.stage_digest}"

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的检查点记录。"""
        return {
            "session_epoch": self.session_epoch,
            "turn_id": self.turn_id,
            "request_id": self.request_id,
            "stage_digest": self.stage_digest,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> StageCheckpoint:
        """从持久化记录恢复检查点。"""
        return cls(
            session_epoch=int(value.get("session_epoch", 0)),
            turn_id=str(value.get("turn_id", "")).strip(),
            request_id=str(value.get("request_id", "")).strip(),
            stage_digest=str(value.get("stage_digest", "")).strip(),
        )


@dataclass(frozen=True)
class MacroApprovalRequest:
    """用户对某宏观地图步骤预览的审批请求。"""

    macro_step_id: str
    owner_frame_id: str
    domain_task_id: str
    session_epoch: int
    approved: bool

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化或事件流消费的审批请求记录。"""
        return {
            "macro_step_id": self.macro_step_id,
            "owner_frame_id": self.owner_frame_id,
            "domain_task_id": self.domain_task_id,
            "session_epoch": self.session_epoch,
            "approved": self.approved,
        }


@dataclass(frozen=True)
class MacroApprovalOutcome:
    """宏观步骤审批解析结论。"""

    disposition: Literal[
        "approved",
        "rejected_by_user",
        "rejected_stale",
        "rejected_not_awaiting",
    ]
    macro_step_id: str
    owner_frame_id: str | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的审批结论。"""
        return {
            "disposition": self.disposition,
            "macro_step_id": self.macro_step_id,
            "owner_frame_id": self.owner_frame_id,
            "reason": self.reason,
        }


def resolve_macro_approval(
    state: MacroPlanState,
    request: MacroApprovalRequest,
    *,
    session_epoch: int,
) -> MacroApprovalOutcome:
    """把用户审批路由到持久化的 owner/checkpoint，拒绝 stale 审批。

    有效审批要求 macro 步骤存在、owner 身份与域任务匹配、session epoch 一致、
    且步骤正处于 awaiting_confirmation/preview_ready 非终态。任何不匹配返回
    `rejected_stale` 或 `rejected_not_awaiting`，且不创建 sibling map-agent、
    不执行写入。用户显式拒绝返回 `rejected_by_user`。
    """
    try:
        step = state.step(request.macro_step_id)
    except MacroPlanError:
        return MacroApprovalOutcome(
            disposition="rejected_stale",
            macro_step_id=request.macro_step_id,
            owner_frame_id=None,
            reason=f"unknown macro step {request.macro_step_id}",
        )
    if request.session_epoch != session_epoch:
        return MacroApprovalOutcome(
            disposition="rejected_stale",
            macro_step_id=request.macro_step_id,
            owner_frame_id=step.owner_frame_id,
            reason="stale session epoch",
        )
    if not step.owner_frame_id or step.owner_frame_id != request.owner_frame_id:
        return MacroApprovalOutcome(
            disposition="rejected_stale",
            macro_step_id=request.macro_step_id,
            owner_frame_id=step.owner_frame_id,
            reason="owner mismatch",
        )
    if step.domain_task_id != request.domain_task_id:
        return MacroApprovalOutcome(
            disposition="rejected_stale",
            macro_step_id=request.macro_step_id,
            owner_frame_id=step.owner_frame_id,
            reason="domain task mismatch",
        )
    if not request.approved:
        return MacroApprovalOutcome(
            disposition="rejected_by_user",
            macro_step_id=request.macro_step_id,
            owner_frame_id=step.owner_frame_id,
            reason="user rejected the preview",
        )
    if step.result is None or step.result.status not in {
        "awaiting_confirmation",
        "preview_ready",
    }:
        return MacroApprovalOutcome(
            disposition="rejected_not_awaiting",
            macro_step_id=request.macro_step_id,
            owner_frame_id=step.owner_frame_id,
            reason=f"step is not awaiting approval (status={step.status})",
        )
    return MacroApprovalOutcome(
        disposition="approved",
        macro_step_id=request.macro_step_id,
        owner_frame_id=step.owner_frame_id,
        reason="approval matches persisted owner and awaiting state",
    )


@dataclass(frozen=True)
class MacroPlanState:
    """宏观计划的持久调度状态：owner 身份、发布状态与展示里程碑的权威视图。

    封装不可变 `MacroPlan`，提供调度器使用的类型化变更方法（创建/恢复 owner、
    发布 owner 结果）与展示里程碑分离视图。状态变更通过返回新实例完成。
    """

    plan: MacroPlan

    @classmethod
    def from_plan(cls, plan: MacroPlan) -> MacroPlanState:
        """从一个已校验的宏观计划构造调度状态。"""
        return cls(plan=plan)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MacroPlanState:
        """从持久化记录恢复宏观计划调度状态。"""
        return cls(plan=MacroPlan.from_dict(value))

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的调度状态记录。"""
        return self.plan.to_dict()

    def step(self, step_id: str) -> MacroPlanStep:
        """返回指定 id 的宏观步骤。"""
        return self.plan.step(step_id)

    def set_owner(
        self,
        step_id: str,
        *,
        owner_frame_id: str,
        domain_task_id: str,
    ) -> MacroPlanState:
        """记录某宏观步骤的 owner Frame 与持久域任务身份，返回新状态。"""
        return replace(
            self,
            plan=self.plan.replace_step(
                step_id,
                owner_frame_id=owner_frame_id,
                domain_task_id=domain_task_id,
                status="running",
            ),
        )

    def publish(self, step_id: str, result: DomainOwnerResult) -> MacroPlanState:
        """发布某 owner 的类型化结果并按基础映射推进宏观步骤状态。

        preview_ready/awaiting_confirmation 保持非终态（running）；completed/
        blocked/cancelled/failed 为终态。已终态的宏观步骤不得再被发布，避免内部
        子阶段完成或重复发布误判宏观步骤终态。完整 predecessor 解锁见 task 2.3。
        """
        current = self.step(step_id)
        if current.status in _TERMINAL_STEP_STATUSES:
            raise MacroPlanError(
                f"macro step {step_id} is already terminal ({current.status}); "
                "owner publication cannot transition it",
                code="macro_step_already_terminal",
            )
        new_status = _OWNER_STATUS_TO_STEP_STATUS.get(result.status, "running")
        return replace(
            self,
            plan=self.plan.replace_step(
                step_id,
                result=result,
                status=cast(PlanStepStatus, new_status),
            ),
        )

    def owner_status(self, step_id: str) -> DomainOwnerStatus | None:
        """返回某步骤的 owner 发布状态，未发布时返回 None。"""
        result = self.step(step_id).result
        return result.status if result is not None else None

    def milestones(self) -> tuple[tuple[str, DisplayMilestone], ...]:
        """返回所有步骤的展示里程碑扁平视图，独立于调度图。"""
        return tuple(
            (step.step_id, milestone)
            for step in self.plan.steps
            for milestone in step.display_milestones
        )


@dataclass(frozen=True)
class OwnerDispatchKey:
    """owner 调度的持久身份键，跨重试、审批、重连与恢复保持稳定。"""

    session_epoch: int
    durable_task_id: str
    domain: str
    domain_task_id: str

    @property
    def key(self) -> str:
        """返回可用于注册表查表的稳定字符串键。"""
        return (
            f"{self.session_epoch}:{self.durable_task_id}:"
            f"{self.domain}:{self.domain_task_id}"
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的调度键记录。"""
        return {
            "session_epoch": self.session_epoch,
            "durable_task_id": self.durable_task_id,
            "domain": self.domain,
            "domain_task_id": self.domain_task_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> OwnerDispatchKey:
        """从持久化记录恢复调度键。"""
        return cls(
            session_epoch=int(value.get("session_epoch", 0)),
            durable_task_id=str(value.get("durable_task_id", "")).strip(),
            domain=str(value.get("domain", "")).strip(),
            domain_task_id=str(value.get("domain_task_id", "")).strip(),
        )


@dataclass(frozen=True)
class OwnerDispatchDecision:
    """create-or-resume owner 调度决策，供帧派发层消费。"""

    action: Literal["resume", "create"]
    step_id: str
    dispatch_key: OwnerDispatchKey
    owner_frame_id: str | None
    domain_task_id: str

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化或供事件流消费的决策记录。"""
        return {
            "action": self.action,
            "step_id": self.step_id,
            "dispatch_key": self.dispatch_key.to_dict(),
            "owner_frame_id": self.owner_frame_id,
            "domain_task_id": self.domain_task_id,
        }


def resolve_owner_dispatch(
    state: MacroPlanState,
    step_id: str,
    *,
    session_epoch: int,
    durable_task_id: str,
) -> OwnerDispatchDecision:
    """根据已持久化的 owner 身份决定 resume 既有 owner 还是 create 新 owner。

    帧派发层在创建子帧前调用本函数：若该步骤已记录 owner_frame_id 则 resume，
    否则返回 create 决策并携带待分配的 domain_task_id。
    """
    step = state.step(step_id)
    domain = step.domain
    domain_task_id = step.domain_task_id or f"{step.step_id}:{session_epoch}"
    dispatch_key = OwnerDispatchKey(
        session_epoch=session_epoch,
        durable_task_id=durable_task_id,
        domain=domain,
        domain_task_id=domain_task_id,
    )
    if step.owner_frame_id:
        return OwnerDispatchDecision(
            action="resume",
            step_id=step_id,
            dispatch_key=dispatch_key,
            owner_frame_id=step.owner_frame_id,
            domain_task_id=domain_task_id,
        )
    return OwnerDispatchDecision(
        action="create",
        step_id=step_id,
        dispatch_key=dispatch_key,
        owner_frame_id=None,
        domain_task_id=domain_task_id,
    )


def _extract_owner_result_field(
    result: DomainOwnerResult, path: str
) -> tuple[bool, Any]:
    """从 owner 发布结果中提取声明的字段或 artifact 引用。

    只允许访问 domain_owner_result_v1 的声明输出（outputs / outputs.<key>）
    与 artifact_refs；任何指向私有内部子结果的路径都被拒绝。
    """
    path = path.strip()
    if not path:
        return True, result.to_dict()
    if path == "outputs":
        return True, dict(result.outputs)
    if path == "artifact_refs":
        return True, list(result.artifact_refs)
    if path.startswith("outputs."):
        key = path[len("outputs.") :].strip()
        if key and key in result.outputs:
            return True, result.outputs[key]
        return False, None
    # 裸键视为 outputs 下的声明字段；非声明字段（私有内部子结果）一律拒绝。
    if path in result.outputs:
        return True, result.outputs[path]
    return False, None


def bind_macro_inputs(state: MacroPlanState, step_id: str) -> dict[str, Any] | str:
    """解析某宏观步骤的全部前置绑定，返回输入字典或类型化错误字符串。

    只消费前置 owner 发布的 domain_owner_result_v1 声明字段或 artifact 引用；
    前置未发布或路径指向私有内部子结果时返回 ``dependency_binding_failed``。
    """
    step = state.step(step_id)
    inputs: dict[str, Any] = {}
    for binding in step.predecessor_bindings:
        predecessor = state.step(binding.source_step_id)
        result = predecessor.result
        if result is None:
            if binding.required:
                return (
                    "dependency_binding_failed: predecessor "
                    f"{binding.source_step_id} has no owner publication"
                )
            continue
        ok, value = _extract_owner_result_field(result, binding.source_path)
        if not ok:
            return (
                "dependency_binding_failed: binding "
                f"{binding.name!r} path {binding.source_path!r} not in "
                f"predecessor {binding.source_step_id} publication"
            )
        inputs[binding.name] = value
    return inputs


def derive_macro_step_status_from_child(
    state: MacroPlanState, step_id: str, child_output: dict[str, Any]
) -> MacroPlanState:
    """从子/owner 帧输出推导宏观步骤状态。

    只有当输出携带 domain_owner_result_v1 发布（含合法 status）时才推进宏观
    步骤；否则子帧完成不改变宏观步骤状态——内部子阶段完成不能直接完成宏观
    步骤，只有 owner 发布才能。已终态步骤的重复发布被静默忽略。
    """
    publication = child_output.get("domain_owner_result")
    if not isinstance(publication, dict):
        return state
    status = str(publication.get("status", "")).strip()
    if status not in _OWNER_STATUS_VALUES:
        return state
    step = state.step(step_id)
    owner_frame_id = str(
        publication.get("owner_frame_id") or step.owner_frame_id or ""
    ).strip()
    domain_task_id = str(
        publication.get("domain_task_id") or step.domain_task_id or ""
    ).strip()
    if not owner_frame_id or not domain_task_id:
        return state
    outputs_value = publication.get("outputs", {})
    result = DomainOwnerResult(
        owner_frame_id=owner_frame_id,
        domain_task_id=domain_task_id,
        macro_step_id=step_id,
        status=cast(DomainOwnerStatus, status),
        outputs=dict(outputs_value) if isinstance(outputs_value, dict) else {},
        artifact_refs=tuple(
            str(item)
            for item in publication.get("artifact_refs", [])
            if isinstance(item, str)
        ),
        recovery_disposition=(
            str(publication["recovery_disposition"]).strip() or None
            if publication.get("recovery_disposition") is not None
            else None
        ),
    )
    try:
        return state.publish(step_id, result)
    except MacroPlanError:
        return state
