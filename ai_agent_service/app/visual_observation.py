"""截图视觉观察的规范化、校验和会话级去重。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

_MAX_DESCRIPTION_CHARS = 2_000
_MAX_INSPECTION_CHARS = 500
_MAX_CONDITIONS = 8
_MAX_CONDITION_CHARS = 200
_MAX_DIMENSIONS = 6
_ALLOWED_DIMENSIONS = frozenset(
    {"layout", "connectivity", "visibility", "color", "composition", "ui", "geometry"}
)


def sanitize_inspection(value: Any) -> dict[str, Any]:
    """校验并截断 LLM 生成的截图观察要求。

    Args:
        value: 截图工具调用中的原始 ``inspection`` 值。

    Returns:
        仅含允许字段的安全观察要求；无有效字段时返回空字典。
    """
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    question = _bounded_text(value.get("question"), _MAX_INSPECTION_CHARS)
    if question:
        result["question"] = question
    focus = _bounded_text(value.get("focus"), _MAX_CONDITION_CHARS)
    if focus:
        result["focus"] = focus
    conditions = value.get("expected_conditions")
    if isinstance(conditions, list):
        safe_conditions = [
            text
            for item in conditions[:_MAX_CONDITIONS]
            if (text := _bounded_text(item, _MAX_CONDITION_CHARS))
        ]
        if safe_conditions:
            result["expected_conditions"] = safe_conditions
    dimensions = value.get("dimensions")
    if isinstance(dimensions, list):
        safe_dimensions = [
            str(item)
            for item in dimensions[:_MAX_DIMENSIONS]
            if isinstance(item, str) and item in _ALLOWED_DIMENSIONS
        ]
        if safe_dimensions:
            result["dimensions"] = safe_dimensions
    return result


def normalize_screenshot_references(
    tool_name: str,
    result: dict[str, Any],
    tool_args: dict[str, Any],
) -> list[dict[str, Any]]:
    """从显式或地图嵌套结果中提取标准截图引用。

    Args:
        tool_name: 前端工具名称。
        result: 前端原始结果。
        tool_args: 原始工具输入。

    Returns:
        一个或零个无二义性的截图引用字典。
    """
    candidates: list[tuple[dict[str, Any], str, bool]] = []
    if tool_name == "capture_viewport_screenshot":
        candidates.append((result, "explicit", False))
    elif tool_name in {"reload_map_targets", "rebuild_map_builder"}:
        nested = result.get("visual_evidence")
        if isinstance(nested, dict):
            candidates.append((nested, "map_automatic", True))
    references: list[dict[str, Any]] = []
    for candidate, scope, advisory in candidates:
        path = str(candidate.get("path", "")).strip()
        if not path or candidate.get("availability") == "unavailable" or candidate.get("ok") is False:
            continue
        reference = {
            "source_tool": tool_name,
            "scope": scope,
            "advisory": bool(candidate.get("advisory", advisory)),
            "path": path,
            "absolute_path": str(candidate.get("absolute_path", "")).strip(),
            "width": _safe_int(candidate.get("width")),
            "height": _safe_int(candidate.get("height")),
            "image_hash": str(candidate.get("image_hash", "")),
            "captured_at_unix_ms": _safe_int(candidate.get("captured_at_unix_ms")),
            "capture_scope": str(candidate.get("capture_scope", "current_viewport")),
            "target": tool_args.get("target", tool_args.get("screenshot_target", {})),
            "inspection": sanitize_inspection(candidate.get("inspection", tool_args.get("inspection", {}))),
            "spatial_facts": candidate.get("spatial_facts", result.get("spatial_facts", {})),
        }
        references.append(reference)
    return references


def observation_key(
    image_path: Path,
    reference: dict[str, Any],
    model: str,
) -> str:
    """计算会话内可重用观察的稳定键。

    Args:
        image_path: 服务端已验证可读的截图路径。
        reference: 已规范化截图引用。
        model: 视觉模型身份。

    Returns:
        由图片字节、目标、观察要求和模型组成的 SHA-256 键。
    """
    digest = hashlib.sha256()
    digest.update(image_path.read_bytes())
    selected = {
        "target": reference.get("target", {}),
        "inspection": reference.get("inspection", {}),
        "capture_scope": reference.get("capture_scope", ""),
        "model": model,
    }
    digest.update(json.dumps(selected, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def make_observation(
    reference: dict[str, Any],
    *,
    status: str,
    model: str,
    description: str = "",
    reason: str = "",
    observation_id: str = "",
    reused: bool = False,
) -> dict[str, Any]:
    """构造不含图片字节的有界视觉观察记录。

    Args:
        reference: 已规范化截图引用。
        status: 终态观察状态。
        model: 视觉模型身份或空值。
        description: 视觉模型返回的描述。
        reason: 终态不可用或失败原因。
        observation_id: 会话内稳定观察标识。
        reused: 是否复用既有终态观察。

    Returns:
        可直接持久化和写入上下文的 JSON 原生字典。
    """
    bounded_description = _bounded_text(description, _MAX_DESCRIPTION_CHARS)
    outcome = _inspection_outcome(bounded_description, bool(reference.get("inspection")))
    return {
        "observation_id": observation_id,
        "status": status,
        "source_tool": reference.get("source_tool", ""),
        "scope": reference.get("scope", ""),
        "advisory": bool(reference.get("advisory", True)),
        "capture_path": reference.get("path", ""),
        "artifact_locator": reference.get("absolute_path") or reference.get("path", ""),
        "image_hash": str(reference.get("image_hash", "")),
        "captured_at_unix_ms": _safe_int(reference.get("captured_at_unix_ms")),
        "width": _safe_int(reference.get("width")),
        "height": _safe_int(reference.get("height")),
        "capture_scope": reference.get("capture_scope", "current_viewport"),
        "inspection": reference.get("inspection", {}),
        "spatial_facts": reference.get("spatial_facts", {}),
        "provenance": {"provider_class": "configured_asset_understanding", "remote_analysis": True},
        "model": model,
        "description": bounded_description,
        "outcome": outcome,
        "confidence": "unknown",
        "limitations": "Visual observation is advisory; engine coordinates come only from frontend spatial facts.",
        "reason": _bounded_text(reason, 300),
        "reused": reused,
    }


def _bounded_text(value: Any, limit: int) -> str:
    """将外部值转换为受限单行文本。"""
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())[:limit]


def _safe_int(value: Any) -> int:
    """把 JSON 数值安全转换为非负整数。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def _inspection_outcome(description: str, has_inspection: bool) -> str:
    """从有界视觉描述提取保守的检查结论。"""
    if not has_inspection:
        return "not_requested"
    lowered = description.lower()
    if "contradicts" in lowered:
        return "contradicts"
    if "matches" in lowered:
        return "matches"
    return "inconclusive"
