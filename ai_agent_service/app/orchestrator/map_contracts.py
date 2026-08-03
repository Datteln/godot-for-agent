"""地图工作流的版本化 schema、阶段映射和合法转换合同。"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Final, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.types import Frame

MAP_WORKER_RESULT_SCHEMA: Final = "map_worker_result_v1"
MapResponseMode = Literal["json_schema", "json_object", "prompt_only"]

MAP_WORKER_RESULT_JSON_SCHEMA_V1: Final[dict[str, Any]] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": MAP_WORKER_RESULT_SCHEMA,
    "title": MAP_WORKER_RESULT_SCHEMA,
    "type": "object",
    "additionalProperties": True,
    "required": [
        "contract_id",
        "result_schema",
        "stage",
        "worker",
        "mode",
        "objective",
        "target_path",
        "map_layer",
        "map_revision",
        "region",
        "summary",
        "facts",
        "proposed_batches",
        "write_results",
        "validation",
        "missing_inputs",
        "risks",
        "next_stage",
    ],
    "properties": {
        "contract_id": {"type": "string"},
        "result_schema": {"const": MAP_WORKER_RESULT_SCHEMA},
        "stage": {
            "type": "string",
            "enum": ["reader", "planner", "writer", "validator", "repairer", "reviewer"],
        },
        "worker": {"type": "string"},
        "mode": {"type": "string"},
        "objective": {"type": "string"},
        "target_path": {"type": "string"},
        "map_layer": {
            "oneOf": [
                {"type": "integer"},
                {"type": "array", "minItems": 1, "items": {"type": "integer"}},
                {"const": "all"},
            ]
        },
        "map_revision": {"type": "integer"},
        "region": {"type": "object"},
        "summary": {"type": "string"},
        "facts": {"type": "array"},
        "proposed_batches": {"type": "array"},
        "write_results": {"type": "array"},
        "validation": {
            "type": "object",
            "additionalProperties": True,
            "required": ["passed", "issues", "structured_issues"],
            "properties": {
                "passed": {"type": "boolean"},
                "completion_allowed": {"type": "boolean"},
                "issues": {"type": "array"},
                "structured_issues": {"type": "array"},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
            },
        },
        "missing_inputs": {"type": "array"},
        "risks": {"type": "array"},
        "planning_status": {
            "type": "string",
            "enum": ["pending", "delivered"],
        },
        "execution_status": {
            "type": "string",
            "enum": [
                "pending",
                "approved",
                "blocked_by_validation",
                "blocked_by_missing_facts",
            ],
        },
        "authoritative_snapshot": {"type": "object"},
        "semantic_plan": {
            "type": "object",
            "properties": {
                "platforms": {"type": "array"},
                "segments": {"type": "array"},
                "semantic_resources": {"type": "array"},
                "reference_cells": {"type": "array"},
                "rationale": {"type": "string"},
            },
        },
        "approved_batches": {"type": "array"},
        "next_stage": {
            "type": "string",
            "enum": [
                "reader",
                "planner",
                "writer",
                "validator",
                "repairer",
                "reviewer",
                "replan",
                "complete",
                "completed",
            ],
        },
    },
}

MAP_RUNTIME_STAGE_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    "read": frozenset({"read", "plan", "write"}),
    "plan": frozenset({"read", "plan", "write"}),
    "write": frozenset({"read", "plan", "write", "validate"}),
    "validate": frozenset({"read", "plan", "write", "validate", "review", "diagnostic"}),
    "review": frozenset({"read", "plan", "review", "diagnostic"}),
    "diagnostic": frozenset({"read", "plan", "write", "validate", "review", "diagnostic"}),
}

MAP_WORKER_TO_RUNTIME_STAGE: Final[dict[str, str]] = {
    "reader": "read",
    "planner": "plan",
    "writer": "write",
    "validator": "validate",
    "repairer": "write",
    "reviewer": "review",
}

MAP_WORKER_NEXT_STAGES: Final[dict[str, frozenset[str]]] = {
    "reader": frozenset({"reader", "planner", "replan"}),
    "planner": frozenset({"reader", "planner", "writer", "replan"}),
    "writer": frozenset({"planner", "validator", "reviewer", "replan"}),
    "validator": frozenset({"planner", "validator", "reviewer", "replan"}),
    "repairer": frozenset({"planner", "validator", "reviewer", "replan"}),
    "reviewer": frozenset({"planner", "reviewer", "complete", "completed", "replan"}),
}

MAP_WORKER_STAGES: Final = frozenset(MAP_WORKER_NEXT_STAGES)


def map_worker_required_fields() -> frozenset[str]:
    """从规范 Schema 派生顶层必填字段，避免维护第二份字段清单。"""
    required = MAP_WORKER_RESULT_JSON_SCHEMA_V1.get("required", [])
    return frozenset(str(item) for item in required)


def specialized_map_worker_schema(frame: Frame) -> dict[str, Any]:
    """用 Frame 已冻结且已知的合同值收紧 map worker wire Schema。"""
    schema = copy.deepcopy(MAP_WORKER_RESULT_JSON_SCHEMA_V1)
    properties = schema["properties"]
    constraints: tuple[tuple[str, Any], ...] = (
        ("contract_id", frame.contract_id),
        ("result_schema", frame.result_schema),
        ("stage", frame.map_stage_contract.get("stage")),
        ("worker", frame.worker_instance_id),
        ("target_path", frame.map_stage_contract.get("target_path")),
        ("map_revision", frame.map_stage_contract.get("map_revision")),
    )
    for field_name, value in constraints:
        if value is None or value == "":
            continue
        if field_name == "map_revision" and (not isinstance(value, int) or isinstance(value, bool)):
            continue
        # const 是更紧的约束：丢弃基础 schema 的 enum，避免 const 值不在 enum 中时
        # 产生不可满足字段（如 orchestrator 帧的 stage="orchestrator" 不在 worker enum
        # ["reader","planner","writer","validator","repairer","reviewer"] 内，导致正确输出被误判）。
        specialized = {key: prop for key, prop in properties[field_name].items() if key != "enum"}
        specialized["const"] = value
        properties[field_name] = specialized
    if frame.allowed_next_stages:
        properties["next_stage"] = {
            **properties["next_stage"],
            "enum": list(frame.allowed_next_stages),
        }
    return schema


def map_worker_schema_digest(schema: dict[str, Any]) -> str:
    """返回 Schema 的稳定短摘要，供持久化元数据和安全诊断关联。"""
    encoded = json.dumps(
        schema,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _stage_example(frame: Frame) -> dict[str, Any]:
    """生成只使用冻结合同值的最小阶段示例。"""
    stage = str(frame.map_stage_contract.get("stage") or frame.agent.map_stage or "reader")
    next_stage = frame.allowed_next_stages[0] if frame.allowed_next_stages else stage
    revision = frame.map_stage_contract.get("map_revision")
    return {
        "contract_id": frame.contract_id or "",
        "result_schema": MAP_WORKER_RESULT_SCHEMA,
        "stage": stage,
        "worker": frame.worker_instance_id or "",
        "mode": "partial",
        "objective": "使用当前 Frame 已收集的事实完成本阶段",
        "target_path": str(frame.map_stage_contract.get("target_path") or ""),
        "map_layer": 0,
        "map_revision": revision if isinstance(revision, int) else 0,
        "region": {},
        "summary": "简短阶段结论",
        "facts": [],
        "proposed_batches": [],
        "write_results": [],
        "validation": {
            "passed": False,
            "completion_allowed": False,
            "issues": [],
            "structured_issues": [],
        },
        "missing_inputs": [],
        "risks": [],
        "planning_status": "pending",
        "execution_status": "pending",
        "authoritative_snapshot": {},
        "semantic_plan": {
            "platforms": [],
            "segments": [],
            "semantic_resources": [],
            "reference_cells": [],
            "rationale": "",
        },
        "approved_batches": [],
        "next_stage": next_stage,
    }


def render_map_worker_response_guidance(
    frame: Frame,
    mode: MapResponseMode,
) -> str:
    """为最终文本回合渲染与 canonical Schema 同源的紧凑系统合同。"""
    schema = specialized_map_worker_schema(frame)
    prefix = (
        f"Final Map Worker Result Contract: schema={MAP_WORKER_RESULT_SCHEMA}; "
        "只输出一个 JSON object，不得调用工具或添加 Markdown。"
    )
    if mode == "json_schema":
        return (
            prefix
            + " Provider 已收到完整 wire schema；本地仍会按相同 schema 与冻结 Frame 合同校验。"
        )
    return (
        prefix
        + "\nWire JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\nMinimal stage-correct example:\n"
        + json.dumps(
            _stage_example(frame),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def arm_map_worker_structured_completion(
    frame: Frame,
    *,
    mode: MapResponseMode,
    correction_limit: int,
) -> None:
    """把下一轮设为专用 Schema 约束的无工具最终结构化回合。"""
    schema = specialized_map_worker_schema(frame)
    frame.force_text_only = True
    frame.response_contract_mode = mode
    frame.structured_correction_limit = max(0, correction_limit)
    frame.response_contract_schema_digest = map_worker_schema_digest(schema)
    guidance = render_map_worker_response_guidance(frame, mode)
    if not any(
        message.get("role") == "system"
        and isinstance(message.get("content"), str)
        and message["content"].startswith("Final Map Worker Result Contract:")
        for message in frame.messages
    ):
        frame.messages.append({"role": "system", "content": guidance})


def validate_map_worker_schema(
    payload: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[str, ...]:
    """校验 canonical Schema 使用到的 JSON Schema 子集并返回稳定字段错误。"""
    errors: list[str] = []

    def validate(value: Any, node: dict[str, Any], path: str) -> None:
        """递归校验类型、必填、const、enum、oneOf 和数组约束。"""
        if "oneOf" in node:
            candidates = node["oneOf"]
            matches = 0
            for candidate in candidates:
                candidate_errors: list[str] = []
                before = len(errors)
                validate(value, candidate, path)
                candidate_errors.extend(errors[before:])
                del errors[before:]
                if not candidate_errors:
                    matches += 1
            if matches != 1:
                errors.append(f"{path}: oneOf_mismatch")
            return
        expected_type = node.get("type")
        type_matches = {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
        }
        if isinstance(expected_type, str) and not type_matches.get(expected_type, True):
            errors.append(f"{path}: expected_{expected_type}")
            return
        if "const" in node and value != node["const"]:
            errors.append(f"{path}: const_mismatch")
        enum = node.get("enum")
        if isinstance(enum, list) and value not in enum:
            errors.append(f"{path}: enum_mismatch")
        if isinstance(value, dict):
            required = node.get("required", [])
            for key in required if isinstance(required, list) else []:
                if key not in value:
                    errors.append(f"{path}.{key}: required")
            properties = node.get("properties", {})
            if isinstance(properties, dict):
                for key, child in properties.items():
                    if key in value and isinstance(child, dict):
                        validate(value[key], child, f"{path}.{key}")
        if isinstance(value, list):
            minimum = node.get("minItems")
            if isinstance(minimum, int) and len(value) < minimum:
                errors.append(f"{path}: minItems")
            items = node.get("items")
            if isinstance(items, dict):
                for index, item in enumerate(value):
                    validate(item, items, f"{path}[{index}]")

    validate(payload, schema, "$")
    return tuple(errors)
