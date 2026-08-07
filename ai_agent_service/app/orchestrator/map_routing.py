"""Deterministic Map task routing attributes and conservative plan policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

MapMutationIntent = Literal["read_only", "mutation", "ambiguous"]
MapOperationExtent = Literal["none", "atomic", "multi_cell", "multi_scope", "unknown"]

_READ_ONLY = re.compile(
    r"(?:只|仅)?(?:读取|查看|看看|解释|分析|检查|观察|描述)|"
    r"\b(?:read|show|explain|analy[sz]e|inspect|observe|describe)\b",
    re.IGNORECASE,
)
_MUTATION = re.compile(
    r"(?:创建|新建|生成|修改|编辑|扩建|扩展|删除|移除|放置|添加|绘制|铺|填充|"
    r"移动|替换|修复|清除|重建|调整)|"
    r"\b(?:create|generate|edit|modify|change|expand|extend|delete|remove|place|"
    r"paint|fill|move|replace|repair|fix|clear|rebuild|adjust|build)\b",
    re.IGNORECASE,
)
_MUTATION_NEGATION = re.compile(
    r"(?:不要|不|无需|禁止)(?:执行|进行)?(?:修改|编辑|写入|变更)|"
    r"(?:只|仅)(?:查看|读取|解释|分析)|"
    r"\b(?:do not|don't|without|no)\s+(?:edit|editing|modify|modifying|write|writing|change)\b",
    re.IGNORECASE,
)
_PLAN_OR_DESIGN = re.compile(
    r"(?:规划|方案|设计|布局|路线|关卡|怎么做|如何做)|"
    r"\b(?:plan|design|layout|route|path|level|proposal|approach)\b",
    re.IGNORECASE,
)
_VALIDATION = re.compile(
    r"(?:验证|校验|检测|可达|连通|复核|截图)|"
    r"\b(?:validate|verify|check|test|reachable|reachability|connectivity|review)\b",
    re.IGNORECASE,
)
_APPROVAL = re.compile(
    r"(?:预览|确认|批准|审批)|\b(?:preview|confirm|approve|approval)\b",
    re.IGNORECASE,
)
_CURRENT_FACT = re.compile(
    r"(?:当前|现有|已有|原有|现在|附近|周围|剩余)|"
    r"\b(?:current|existing|present|nearby|around|remaining)\b",
    re.IGNORECASE,
)
_EXPLICIT_PATH = re.compile(r"(?:res://[^\s，,；;]+|(?:map|tilemap)/[A-Za-z0-9_./-]+)", re.IGNORECASE)
_COORDINATE = re.compile(
    r"(?:坐标|cell)\s*[（(\[]?\s*-?\d+\s*[,，]\s*-?\d+\s*[）)\]]?",
    re.IGNORECASE,
)
_LAYER = re.compile(r"(?:第\s*\d+\s*层|layer\s*[:=#]?\s*\d+)", re.IGNORECASE)
_KNOWN_RESOURCE = re.compile(
    r"(?:tile|图块|瓦片|resource|资源)\s*(?:id)?\s*[:=#]?\s*[A-Za-z0-9_./-]+",
    re.IGNORECASE,
)
_MULTI_SCOPE = re.compile(
    r"(?:多个|多处|各个|所有|整张|整个|批量|分别|以及|并且|同时|和终点|和出生点)|"
    r"\b(?:multiple|all|entire|whole|batch|across|respectively|and also)\b",
    re.IGNORECASE,
)
_MULTI_CELL = re.compile(
    r"(?:约?\s*[2-9]\d*\s*(?:格|个|枚|处|块)|区域|范围|矩形|一排|一列)|"
    r"\b(?:region|area|rectangle|row|column|[2-9]\d*\s+(?:cells?|tiles?|objects?))\b",
    re.IGNORECASE,
)
_UNCERTAINTY = re.compile(
    r"(?:合适|适当|若干|一些|优化|美化|改善|大概|随便|看着办)|"
    r"\b(?:appropriate|some|several|optimi[sz]e|improve|roughly|as needed)\b",
    re.IGNORECASE,
)
_OPERATION_SEPARATOR = re.compile(r"(?:，|,|；|;|然后|再|并且|同时|以及|\bthen\b|\band\b)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class MapTaskRoutingAssessment:
    """Runtime-owned facts used to decide whether a visible plan is mandatory."""

    mutation_intent: MapMutationIntent
    explicit_target: bool
    operation_count: int | None
    operation_extent: MapOperationExtent
    known_inputs: bool
    current_fact_dependency: bool
    planning_required: bool
    validation_required: bool
    approval_required: bool
    ambiguous: bool

    @property
    def is_proven_atomic_edit(self) -> bool:
        """Only a fully specified, bounded mutation may skip macro planning."""
        return (
            self.mutation_intent == "mutation"
            and self.explicit_target
            and self.operation_count == 1
            and self.operation_extent == "atomic"
            and self.known_inputs
            and not self.current_fact_dependency
            and not self.planning_required
            and not self.validation_required
            and not self.approval_required
            and not self.ambiguous
        )

    @property
    def requires_visible_plan(self) -> bool:
        """Read-only work never gains mutation authority; other uncertainty plans."""
        if self.mutation_intent == "read_only":
            return False
        return not self.is_proven_atomic_edit

    def plan_reasons(self) -> tuple[str, ...]:
        """Return stable reason codes suitable for tool feedback and tests."""
        reasons: list[str] = []
        if self.mutation_intent == "ambiguous":
            reasons.append("ambiguous_mutation_intent")
        if not self.explicit_target:
            reasons.append("missing_explicit_target")
        if self.operation_count != 1:
            reasons.append("operation_count_not_one")
        if self.operation_extent != "atomic":
            reasons.append(f"operation_extent_{self.operation_extent}")
        if not self.known_inputs:
            reasons.append("unknown_resource_or_cell_inputs")
        if self.current_fact_dependency:
            reasons.append("requires_current_map_facts")
        if self.planning_required:
            reasons.append("requires_layout_or_route_planning")
        if self.validation_required:
            reasons.append("requires_validation")
        if self.approval_required:
            reasons.append("requires_approval")
        if self.ambiguous:
            reasons.append("ambiguous_scope_or_inputs")
        return tuple(dict.fromkeys(reasons))


def assess_map_task(task: str) -> MapTaskRoutingAssessment:
    """Derive conservative routing facts from task semantics, never keyword counts."""
    normalized = " ".join(task.strip().split())
    has_read = _READ_ONLY.search(normalized) is not None
    has_mutation = (
        _MUTATION.search(normalized) is not None
        and _MUTATION_NEGATION.search(normalized) is None
    )
    if has_mutation:
        intent: MapMutationIntent = "mutation"
    elif has_read:
        intent = "read_only"
    else:
        intent = "ambiguous"

    has_path = _EXPLICIT_PATH.search(normalized) is not None
    has_coordinate = _COORDINATE.search(normalized) is not None
    has_layer = _LAYER.search(normalized) is not None
    explicit_target = has_path and has_coordinate and has_layer
    known_inputs = has_coordinate and _KNOWN_RESOURCE.search(normalized) is not None
    multi_scope = _MULTI_SCOPE.search(normalized) is not None
    multi_cell = _MULTI_CELL.search(normalized) is not None
    operation_phrases = [
        part
        for part in _OPERATION_SEPARATOR.split(normalized)
        if _MUTATION.search(part) is not None
    ]
    operation_count = len(operation_phrases) if operation_phrases else (1 if has_mutation else 0)
    if multi_scope:
        extent: MapOperationExtent = "multi_scope"
    elif multi_cell:
        extent = "multi_cell"
    elif has_mutation and explicit_target and known_inputs and operation_count == 1:
        extent = "atomic"
    elif not has_mutation:
        extent = "none"
    else:
        extent = "unknown"

    current_fact_dependency = _CURRENT_FACT.search(normalized) is not None or has_read and has_mutation
    planning_required = _PLAN_OR_DESIGN.search(normalized) is not None
    validation_required = _VALIDATION.search(normalized) is not None
    approval_required = _APPROVAL.search(normalized) is not None
    ambiguous = (
        _UNCERTAINTY.search(normalized) is not None
        or intent == "ambiguous"
        or (has_mutation and (not explicit_target or not known_inputs))
    )
    return MapTaskRoutingAssessment(
        mutation_intent=intent,
        explicit_target=explicit_target,
        operation_count=operation_count,
        operation_extent=extent,
        known_inputs=known_inputs,
        current_fact_dependency=current_fact_dependency,
        planning_required=planning_required,
        validation_required=validation_required,
        approval_required=approval_required,
        ambiguous=ambiguous,
    )
