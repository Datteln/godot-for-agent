"""地图 agent 执行链路共享的类型化运行时契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypeAlias

PlanStepStatus: TypeAlias = Literal[
    "pending",
    "running",
    "succeeded",
    "failed",
    "blocked",
    "cancelled",
]
SkillBindingStatus: TypeAlias = Literal["resolved", "missing", "incompatible"]
RetryCategory: TypeAlias = Literal[
    "missing_input",
    "stale_revision",
    "contract_violation",
    "validation_failure",
    "tool_failure",
    "permission_denied",
    "persistence_failure",
    "structured_output",
]
TransactionStatus: TypeAlias = Literal[
    "prepared",
    "committed",
    "rolled_back",
    "failed",
]


@dataclass(frozen=True)
class PlanStepResult:
    """保存一个计划步骤的终态、类型化输出与传播信息。"""

    status: PlanStepStatus
    output: dict[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    error_code: str | None = None
    blocked_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化的 JSON 原生结构。"""
        return asdict(self)


@dataclass(frozen=True)
class SkillBindingResult:
    """描述 Skill 在当前 Agent、阶段、模式和权限下的绑定结果。"""

    status: SkillBindingStatus
    requested_name: str
    qualified_name: str | None = None
    effective_tools: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """转换为 API 和持久化可用的结构。"""
        return asdict(self)


@dataclass(frozen=True)
class MapWorkflowEvent:
    """表示作用于地图执行范围或 workflow 身份的唯一状态变更事实。"""

    event_id: str
    event_type: str
    target: str
    revision: int
    payload: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    turn_id: str | None = None

    @property
    def scope_key(self) -> str:
        """返回事件所属的规范执行范围或 workflow 作用域键。"""
        return f"{self.target.strip()}::revision={self.revision}"

    def to_dict(self) -> dict[str, Any]:
        """转换为可追加到 Session 的事件记录。"""
        return asdict(self)


@dataclass(frozen=True)
class FrameContractViolation:
    """记录子 Frame 偏离创建时冻结接口合同的原因。"""

    frame_id: str
    code: str
    message: str
    expected: dict[str, Any] = field(default_factory=dict)
    actual: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为结构化拒绝结果。"""
        return asdict(self)


@dataclass(frozen=True)
class EvidenceReference:
    """引用与目标、revision、验证合同绑定的不可变证据。"""

    evidence_id: str
    evidence_type: str
    target: str
    revision: int
    contract_id: str
    artifact_ref: str | None = None
    digest: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """转换为 Evidence Registry 条目。"""
        return asdict(self)


@dataclass(frozen=True)
class RetryIdentity:
    """定义一次语义重试的稳定身份与根因分类。"""

    category: RetryCategory
    error_category: str
    root_cause: str
    stage: str
    target: str
    revision: int
    operation: str
    missing_inputs: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        """返回可用于重试预算与幂等判断的稳定键。"""
        missing = ",".join(sorted(self.missing_inputs))
        return (
            f"{self.category}:{self.error_category}:{self.stage}:{self.target}:"
            f"{self.revision}:{self.operation}:{missing}"
        )

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化重试记录。"""
        return asdict(self)


@dataclass(frozen=True)
class MapTransactionJournal:
    """记录一个 approved write group 的撤销事务结果。"""

    transaction_id: str
    target: str
    base_revision: int
    final_revision: int | None
    operation_ids: tuple[str, ...]
    status: TransactionStatus
    undo_token: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化事务日志。"""
        return asdict(self)


@dataclass(frozen=True)
class UnapprovedWriteRejection:
    """把缺少 planner/validator 批次的写入路由回规划阶段。"""

    error_code: str
    message: str
    next_stage: Literal["planner"] = "planner"
    required_artifact: str = "approved_write_batch"

    def to_dict(self) -> dict[str, Any]:
        """转换为工具可消费的类型化拒绝结果。"""
        return asdict(self)
