"""地图结构化修复、语义重试身份与熔断报告。"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from app.orchestrator.map_workflow import (
    dispatch_map_workflow_event,
    make_map_workflow_event,
)
from app.orchestrator.runtime_contracts import RetryCategory, RetryIdentity

STRUCTURED_REPAIR_MAX_ATTEMPTS = 3
SEMANTIC_RETRY_MAX_ATTEMPTS = 3

_VOLATILE_OPERATION_KEYS = frozenset(
    {
        "call_id",
        "request_id",
        "turn_id",
        "frame_id",
        "timestamp",
        "tool_use_id",
    }
)


def structured_error_category(error: str) -> str:
    """把易变错误文本归并为稳定的结构类别。"""
    lowered = error.casefold()
    categories = (
        ("invalid_json", ("合法 json", "json object")),
        ("missing_fields", ("缺少字段", "missing field")),
        ("frame_contract", ("contract", "不一致", "mismatch")),
        ("invalid_validation", ("validation",)),
        ("invalid_array_field", ("必须是 array",)),
        ("invalid_map_layer", ("map_layer",)),
        ("invalid_stage", ("stage 必须",)),
        ("evidence_contract", ("截图证据", "evidence")),
    )
    for category, markers in categories:
        if any(marker in lowered for marker in markers):
            return category
    return "invalid_structured_output"


def safe_structured_diagnostic(error: str) -> dict[str, Any]:
    """保留可诊断类别与长度，不回显不可信原始 worker 内容。"""
    normalized = " ".join(error.split())
    return {
        "category": structured_error_category(error),
        "message": normalized[:500],
        "message_digest": hashlib.sha256(error.encode("utf-8")).hexdigest()[:16],
        "message_chars": len(error),
    }


def structured_repair_actions(category: str) -> list[str]:
    """返回服务端实际采取的保守修复动作。"""
    common = [
        "forced_validation_failure",
        "disabled_completion",
        "normalized_required_arrays",
        "preserved_safe_structured_fields",
    ]
    if category == "invalid_json":
        return [*common, "replaced_unparseable_payload"]
    if category == "frame_contract":
        return [*common, "restored_frame_owned_stage_identity"]
    return common


def normalized_operation_signature(value: Any) -> str:
    """对操作参数做语义规范化并返回稳定摘要。"""
    normalized = _normalize_operation(value)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def record_semantic_retry(
    state: Any,
    *,
    category: RetryCategory,
    error_category: str,
    root_cause: str,
    stage: str,
    target: str,
    revision: int,
    operation: Any,
    missing_inputs: list[Any] | None = None,
    threshold: int = SEMANTIC_RETRY_MAX_ATTEMPTS,
) -> dict[str, Any]:
    """按 scope + 规范操作 + 类别累计重试，并保留最早根因。"""
    missing_names = tuple(sorted(_missing_input_names(missing_inputs or [])))
    identity = RetryIdentity(
        category=category,
        error_category=error_category,
        root_cause=root_cause,
        stage=stage,
        target=target,
        revision=revision,
        operation=normalized_operation_signature(operation),
        missing_inputs=missing_names,
    )
    previous = state.retry_registry.get(identity.key, {})
    attempt = int(previous.get("attempt", 0)) + 1
    first_root_cause = str(previous.get("first_root_cause", "")) or root_cause
    entry = {
        **identity.to_dict(),
        "retry_key": identity.key,
        "attempt": attempt,
        "threshold": threshold,
        "exhausted": attempt >= threshold,
        "first_root_cause": first_root_cause,
        "last_root_cause": root_cause,
        "recovery_guidance": _recovery_guidance(category, error_category),
    }
    dispatch_map_workflow_event(
        state,
        make_map_workflow_event(
            state,
            "retry_recorded",
            target or "__workflow__",
            revision,
            {"retry_key": identity.key, "retry": entry},
        ),
    )
    return entry


def retry_pause_report(
    state: Any,
    *,
    stage: str,
    target: str,
    revision: int,
    last_attempt: dict[str, Any],
) -> dict[str, Any]:
    """聚合同一 scope 的类别计数并输出可恢复暂停信息。"""
    scoped = [
        item
        for item in state.retry_registry.values()
        if item.get("stage") == stage
        and item.get("target") == target
        and item.get("revision") == revision
    ]
    category_counts: dict[str, int] = {}
    for item in scoped:
        category = str(item.get("error_category", item.get("category", "unknown")))
        category_counts[category] = category_counts.get(category, 0) + int(
            item.get("attempt", 0)
        )
    earliest = next(
        (
            str(item.get("first_root_cause", ""))
            for item in scoped
            if str(item.get("first_root_cause", ""))
        ),
        str(last_attempt.get("first_root_cause", "")),
    )
    return {
        "type": "map_retry_exhausted",
        "first_root_cause": earliest,
        "category_counts": category_counts,
        "stage": stage,
        "target": target,
        "revision": revision,
        "last_attempt": dict(last_attempt),
        "recovery_guidance": str(
            last_attempt.get(
                "recovery_guidance",
                "Inspect the first root cause and supply changed typed inputs.",
            )
        ),
    }


def _normalize_operation(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _normalize_operation(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _VOLATILE_OPERATION_KEYS
        }
    if isinstance(value, list):
        return [_normalize_operation(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def _missing_input_names(values: list[Any]) -> list[str]:
    names: list[str] = []
    for value in values:
        if isinstance(value, dict):
            name = value.get("field", value.get("name", value.get("kind", "")))
        else:
            name = value
        normalized = str(name).strip()
        if normalized:
            names.append(normalized)
    return names


def _recovery_guidance(category: RetryCategory, error_category: str) -> str:
    if category == "missing_input":
        return "Run the typed reader recovery step and bind its facts into the retry."
    if category == "structured_output":
        return (
            f"Repair the {error_category} contract violation; do not retry unchanged output."
        )
    return "Change the scoped inputs or resolve the first root cause before retrying."
