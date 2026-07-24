"""地图写入资源解析与语义规范化。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MapResourceNormalization:
    """保存一次地图写入资源规范化的结果。"""

    args: dict[str, Any]
    rewritten_operations: int = 0
    error_code: str | None = None
    error_message: str | None = None


def _whole_number(value: Any) -> int | None:
    """把 JSON 中的整数或整数浮点数规范化为整数。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _load_resource_registry(project_root: Path) -> dict[str, dict[str, Any]]:
    """读取工程资源注册表，并忽略损坏或非对象条目。"""
    registry_path = (
        project_root.resolve()
        / ".ai_agent_service"
        / "map_agent"
        / "resource_registry.json"
    )
    try:
        with registry_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(key, str) and isinstance(item, dict)
    }


def _normalized_tags(value: Any) -> set[str]:
    """把资源或操作的 tags 规范化为小写字符串集合。"""
    if not isinstance(value, list):
        return set()
    return {str(tag).strip().lower() for tag in value if str(tag).strip()}


def _registry_atlas(entry: dict[str, Any]) -> tuple[int | None, int | None, int | None]:
    """提取资源注册项的二维 TileSet atlas 标识。"""
    coords_value = entry.get("atlas_coords")
    coords = coords_value if isinstance(coords_value, dict) else {}
    return (
        _whole_number(entry.get("source_id")),
        _whole_number(entry.get("atlas_x", coords.get("x"))),
        _whole_number(entry.get("atlas_y", coords.get("y"))),
    )


def _resource_candidates(
    registry: dict[str, dict[str, Any]],
    operation: dict[str, Any],
) -> list[tuple[int, str]]:
    """按 atlas 精确匹配并根据操作语义给资源候选排序。"""
    wanted = (
        _whole_number(operation.get("source_id")),
        _whole_number(operation.get("atlas_x")),
        _whole_number(operation.get("atlas_y")),
    )
    if any(value is None for value in wanted):
        return []

    semantic = str(operation.get("semantic_layer", "")).strip().lower()
    operation_tags = _normalized_tags(operation.get("tags"))
    candidates: list[tuple[int, str]] = []
    for key, entry in registry.items():
        if _registry_atlas(entry) != wanted:
            continue
        entry_tags = _normalized_tags(entry.get("tags"))
        score = len(operation_tags & entry_tags)
        if semantic and semantic in entry_tags:
            score += 4
        candidates.append((score, key))
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
    return candidates


def _enrich_operation_semantics(
    operation: dict[str, Any],
    resource_entry: dict[str, Any],
) -> None:
    """用注册资源标签补齐平台识别所需的 semantic_layer 与 tags。"""
    entry_tags = _normalized_tags(resource_entry.get("tags"))
    operation_tags = _normalized_tags(operation.get("tags"))
    combined_tags = sorted(entry_tags | operation_tags)
    if combined_tags:
        operation["tags"] = combined_tags
    if str(operation.get("semantic_layer", "")).strip():
        return
    if "ground" in entry_tags:
        operation["semantic_layer"] = "ground"
    elif "platform" in entry_tags:
        operation["semantic_layer"] = "ground"


def _ambiguous_resource_message(
    operation: dict[str, Any],
    candidates: list[tuple[int, str]],
) -> str:
    """生成只允许从已验证候选中选择一次的资源歧义说明。"""
    candidate_names = ", ".join(key for _, key in candidates)
    return (
        "同一 atlas 对应多个已注册资源，服务层拒绝猜测。"
        f"source_id={operation.get('source_id')}, atlas="
        f"({operation.get('atlas_x')},{operation.get('atlas_y')})；"
        f"候选 resource_key：{candidate_names}。"
        "请只选择其中一个 resource/resource_key 后重试，不要再提交裸 atlas。"
    )


def normalize_edit_map_resources(
    project_root: Path,
    tool_args: dict[str, Any],
) -> MapResourceNormalization:
    """把 edit_map fill 的裸 atlas 确定性改写为已注册资源键。

    Args:
        project_root: 当前 Godot 工程根目录。
        tool_args: 已解析但尚未下发的 edit_map 参数。

    Returns:
        包含规范化参数、改写数量或阻断错误的不可变结果。
    """
    normalized = deepcopy(tool_args)
    operations_value = normalized.get("operations")
    if not isinstance(operations_value, list):
        return MapResourceNormalization(args=normalized)

    registry = _load_resource_registry(project_root)
    rewritten = 0
    for operation in operations_value:
        if not isinstance(operation, dict) or operation.get("action", "fill") != "fill":
            continue
        resource_value = operation.get("resource", operation.get("resource_key"))
        resource = resource_value.strip() if isinstance(resource_value, str) else ""
        if resource:
            entry = registry.get(resource)
            if isinstance(entry, dict):
                _enrich_operation_semantics(operation, entry)
            continue
        has_raw_atlas = all(
            _whole_number(operation.get(key)) is not None
            for key in ("source_id", "atlas_x", "atlas_y")
        )
        if not has_raw_atlas:
            continue
        candidates = _resource_candidates(registry, operation)
        if not candidates:
            return MapResourceNormalization(
                args=normalized,
                rewritten_operations=rewritten,
                error_code="map_resource_resolution_failed",
                error_message=(
                    "裸 TileSet atlas 在 resource_registry.json 中没有精确匹配；"
                    f"source_id={operation.get('source_id')}, atlas="
                    f"({operation.get('atlas_x')},{operation.get('atlas_y')})。"
                    "请先注册经过读取验证的资源，禁止让 LLM 猜测 resource_key。"
                ),
            )
        best_score = candidates[0][0]
        best = [candidate for candidate in candidates if candidate[0] == best_score]
        if len(best) > 1:
            return MapResourceNormalization(
                args=normalized,
                rewritten_operations=rewritten,
                error_code="map_resource_ambiguous",
                error_message=_ambiguous_resource_message(operation, best),
            )
        resource = best[0][1]
        operation["resource"] = resource
        operation.pop("resource_key", None)
        for key in ("source_id", "atlas_x", "atlas_y", "alternative_tile"):
            operation.pop(key, None)
        _enrich_operation_semantics(operation, registry[resource])
        rewritten += 1
    return MapResourceNormalization(
        args=normalized,
        rewritten_operations=rewritten,
    )
