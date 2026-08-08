"""地图工具参数/结果推导：提取器、区域推导、区域读签名与共享常量。"""

from __future__ import annotations

from app.orchestrator.map_context import latest_map_revision
from app.orchestrator.map_workers import MAP_REVISION_GUARDED_TOOL_NAMES, MAP_VALIDATION_TOOL_NAMES, MAP_WRITE_TOOL_NAMES
from app.sessions.store import Session
from typing import Any
_MAP_CONTEXT_MAX_TARGETS = 8


_MAP_CONTEXT_MAX_REGIONS_PER_LAYER = 24


_MAP_CONTEXT_MAX_SUMMARY_CHARS = 2048


_MAP_CONTEXT_MAX_TOTAL_CHARS = 262_144


_MAP_ATLAS_SUMMARY_LIMIT = 12


_MAP_MATCH_SUMMARY_LIMIT = 12


_MAP_VALIDATION_REPEAT_LIMIT = 2


_MAP_OBJECT_PLACEMENT_TOOL_NAMES = frozenset(
    {
        "place_map_objects",
        "find_placement_anchors",
        "validate_object_placements",
        "repair_placements",
    }
)


_MAP_COMPLETION_TOOL_NAMES = MAP_REVISION_GUARDED_TOOL_NAMES | MAP_VALIDATION_TOOL_NAMES


_MAP_REGION_READ_GUARDED_TOOL_NAMES = (
    MAP_WRITE_TOOL_NAMES
    | MAP_VALIDATION_TOOL_NAMES
    | frozenset(
        {
            "plan_map_layout",
            "plan_map_algorithms",
            "validate_platform_level_plan",
            "plan_reachable_map_growth",
            "compute_reachable_frontier",
            "convert_map_coords",
            "find_placement_anchors",
            "query_spatial_index",
            "sample_poisson_points",
            "sample_noise_grid",
        }
    )
) - frozenset({"write_resource_registry", "ensure_standard_map_layers"})


def _map_revision_from_result(result: dict[str, Any]) -> int | None:
    """从地图工具结果中提取最新可用的地图版本号。"""
    for key in ("map_revision", "actual_revision", "next_expected_revision"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _preferred_map_layer_from_layers(layers: Any) -> int | None:
    """从 legacy TileMap 图层列表中选一个更像前景/碰撞层的图层。"""
    if not isinstance(layers, list):
        return None
    ranked_keywords = ("mid", "foreground", "front", "ground", "collision")
    fallback: int | None = None
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        index = layer.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        if fallback is None:
            fallback = index
        name = str(layer.get("name", "")).lower()
        if any(keyword in name for keyword in ranked_keywords):
            return index
    return fallback


def _map_layer_from_result(result: dict[str, Any], *, prefer_layers: bool = False) -> int | None:
    """从地图工具结果中提取最新确认或建议的地图图层。"""
    if prefer_layers:
        preferred = _preferred_map_layer_from_layers(result.get("layers"))
        if preferred is not None:
            return preferred
    for key in ("map_layer", "suggested_map_layer"):
        value = result.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _map_target_from_result(tool_args: dict[str, Any], result: dict[str, Any]) -> str:
    """从工具入参与结果中提取地图目标路径。"""
    for key in ("target_path", "target"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value
    value = tool_args.get("target_path")
    return value if isinstance(value, str) else ""


def _single_known_map_target(session: Session) -> str:
    """返回当前会话唯一已知地图目标；多目标时不猜。"""
    # latest_revisions 的 key 可能是 "target" 或 "target::map_layer=N"，
    # 这里取 "::map_layer=" 前的 target_path 部分，再与 latest_layers 合并，
    # 得到去重后的目标集合。
    targets = {
        key.split("::map_layer=", 1)[0] for key in session.map_task_state.latest_revisions
    } | set(session.map_task_state.latest_layers)
    state_targets = session.map_task_state.context_state.get("targets")
    if isinstance(state_targets, dict):
        targets.update(str(target) for target in state_targets if str(target))
    return next(iter(targets)) if len(targets) == 1 else ""


def _resolved_map_tool_args(session: Session, tool_args: dict[str, Any]) -> dict[str, Any]:
    """用会话里已确认的地图目标和图层补齐工具参数。"""
    resolved = dict(tool_args)
    target = resolved.get("target_path")
    if not isinstance(target, str) or not target:
        target = _single_known_map_target(session)
        if target:
            resolved["target_path"] = target
    layer = resolved.get("map_layer", resolved.get("ground_map_layer"))
    if isinstance(layer, int) and not isinstance(layer, bool):
        resolved["map_layer"] = layer
        return resolved
    if isinstance(target, str) and target:
        latest_layer = session.map_task_state.latest_layers.get(target)
        if latest_layer is not None:
            resolved["map_layer"] = latest_layer
    return resolved


def _map_tool_requires_map_layer(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> bool:
    """判断地图工具是否必须带 2D map_layer。"""
    if tool_name in {
        "validate_platform_level_plan",
        "plan_reachable_map_growth",
        "compute_reachable_frontier",
    }:
        return True
    if "map_layer" in tool_args or "ground_map_layer" in tool_args:
        return True
    target = tool_args.get("target_path")
    return isinstance(target, str) and target in session.map_task_state.latest_layers


def _map_tool_missing_required_context(
    session: Session,
    tool_name: str,
    tool_args: dict[str, Any],
) -> str:
    """返回地图工具执行前仍缺少的关键上下文字段。"""
    if _map_region_from_tool_args(tool_name, tool_args) is None:
        return ""
    if not isinstance(tool_args.get("target_path"), str) or not tool_args.get("target_path"):
        return "target_path"
    if _map_tool_requires_map_layer(session, tool_name, tool_args) and "map_layer" not in tool_args:
        return "map_layer"
    return ""


def _map_region_from_write_args(
    tool_args: dict[str, Any], result_dict: dict[str, Any]
) -> dict[str, int] | None:
    """从地图写工具参数中推导需要重读的区域。"""
    region = tool_args.get("region", tool_args.get("rect", result_dict.get("region")))
    if isinstance(region, dict):
        return {
            str(key): int(value)
            for key, value in region.items()
            if isinstance(value, int) and not isinstance(value, bool)
        }

    operations = tool_args.get("operations")
    if not isinstance(operations, list):
        return None

    min_x: int | None = None
    min_y: int | None = None
    max_x: int | None = None
    max_y: int | None = None
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        x_value = operation.get("to_x", operation.get("x"))
        y_value = operation.get("to_y", operation.get("y"))
        if (
            not isinstance(x_value, int)
            or isinstance(x_value, bool)
            or not isinstance(y_value, int)
            or isinstance(y_value, bool)
        ):
            continue
        width = operation.get("width", 1)
        height = operation.get("height", 1)
        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
        ):
            continue
        op_max_x = x_value + max(width, 1) - 1
        op_max_y = y_value + max(height, 1) - 1
        min_x = x_value if min_x is None else min(min_x, x_value)
        min_y = y_value if min_y is None else min(min_y, y_value)
        max_x = op_max_x if max_x is None else max(max_x, op_max_x)
        max_y = op_max_y if max_y is None else max(max_y, op_max_y)

    if min_x is None or min_y is None or max_x is None or max_y is None:
        return None
    return {
        "x": min_x,
        "y": min_y,
        "width": max_x - min_x + 1,
        "height": max_y - min_y + 1,
    }


def _direct_region_from_args(tool_args: dict[str, Any]) -> dict[str, int] | None:
    """从直接区域字段中提取地图区域。"""
    required = ("x", "y", "width", "height")
    if any(
        not isinstance(tool_args.get(key), int) or isinstance(tool_args.get(key), bool)
        for key in required
    ):
        return None
    region = {key: int(tool_args[key]) for key in required}
    if isinstance(tool_args.get("z"), int) and not isinstance(tool_args.get("z"), bool):
        region["z"] = int(tool_args["z"])
    if isinstance(tool_args.get("depth"), int) and not isinstance(tool_args.get("depth"), bool):
        region["depth"] = int(tool_args["depth"])
    return region


def _entry_sample_region_from_args(tool_args: dict[str, Any]) -> dict[str, int] | None:
    """从平台规划 entry_sample 字段中提取真实边界采样区域。"""
    mapping = {
        "x": "entry_sample_x",
        "y": "entry_sample_y",
        "width": "entry_sample_width",
        "height": "entry_sample_height",
    }
    if any(
        not isinstance(tool_args.get(source), int) or isinstance(tool_args.get(source), bool)
        for source in mapping.values()
    ):
        return None
    return {target: int(tool_args[source]) for target, source in mapping.items()}


def _points_region(points: Any) -> dict[str, int] | None:
    """从对象/单元点列表推导最小包围区域。"""
    if not isinstance(points, list):
        return None
    min_x: int | None = None
    min_y: int | None = None
    max_x: int | None = None
    max_y: int | None = None
    for item in points:
        if not isinstance(item, dict):
            continue
        x_value = item.get("x")
        y_value = item.get("y")
        if (
            not isinstance(x_value, int)
            or isinstance(x_value, bool)
            or not isinstance(y_value, int)
            or isinstance(y_value, bool)
        ):
            continue
        min_x = x_value if min_x is None else min(min_x, x_value)
        min_y = y_value if min_y is None else min(min_y, y_value)
        max_x = x_value if max_x is None else max(max_x, x_value)
        max_y = y_value if max_y is None else max(max_y, y_value)
    if min_x is None or min_y is None or max_x is None or max_y is None:
        return None
    return {"x": min_x, "y": min_y, "width": max_x - min_x + 1, "height": max_y - min_y + 1}


def _map_region_from_tool_args(tool_name: str, tool_args: dict[str, Any]) -> dict[str, int] | None:
    """从地图工具入参推导它依赖的真实地图区域。"""
    if tool_name in {"validate_platform_level_plan", "plan_reachable_map_growth"}:
        entry_region = _entry_sample_region_from_args(tool_args)
        if entry_region is not None:
            return entry_region
    direct_region = _direct_region_from_args(tool_args)
    if direct_region is not None:
        return direct_region
    write_region = _map_region_from_write_args(tool_args, {})
    if write_region is not None:
        return write_region
    for key in ("objects", "cells", "path_cells", "route_cells", "frontier_cells"):
        region = _points_region(tool_args.get(key))
        if region is not None:
            return region
    return None


def _map_region_read_signature(tool_name: str, tool_args: dict[str, Any]) -> str | None:
    """生成地图区域读取签名，用于约束地图工具先读区域。"""
    region = _map_region_from_tool_args(tool_name, tool_args)
    if region is None:
        return None
    target = tool_args.get("target_path", "")
    if not isinstance(target, str):
        target = ""
    map_layer = tool_args.get("map_layer", tool_args.get("ground_map_layer"))
    if not isinstance(map_layer, int) or isinstance(map_layer, bool):
        return None
    z_value = region.get("z", 0)
    depth = region.get("depth", 1)
    return "|".join(
        str(value)
        for value in (
            target,
            map_layer,
            region["x"],
            region["y"],
            z_value,
            region["width"],
            region["height"],
            depth,
        )
    )


def _parsed_map_region_read_signature(
    signature: str,
) -> tuple[str, int, int, int, int, int, int, int] | None:
    """解析地图读取签名，供同 revision 的区域包含复用。"""
    parts = signature.split("|")
    if len(parts) != 8:
        return None
    try:
        return (
            parts[0],
            int(parts[1]),
            int(parts[2]),
            int(parts[3]),
            int(parts[4]),
            int(parts[5]),
            int(parts[6]),
            int(parts[7]),
        )
    except ValueError:
        return None


def _map_region_signature_contains(outer: str, inner: str) -> bool:
    """判断 outer 签名的区域是否完整覆盖 inner。"""
    outer_value = _parsed_map_region_read_signature(outer)
    inner_value = _parsed_map_region_read_signature(inner)
    if outer_value is None or inner_value is None:
        return False
    outer_target, outer_layer, ox, oy, oz, ow, oh, od = outer_value
    inner_target, inner_layer, ix, iy, iz, iw, ih, depth = inner_value
    return (
        outer_target == inner_target
        and outer_layer == inner_layer
        and ox <= ix
        and oy <= iy
        and oz <= iz
        and ox + ow >= ix + iw
        and oy + oh >= iy + ih
        and oz + od >= iz + depth
    )


def _current_map_region_signature(
    session: Session,
    requested_signature: str,
    target: str,
) -> str | None:
    """查找当前 revision 下精确或完整覆盖请求的已读区域签名。"""
    # 从请求签名中解析出 map_layer，用于获取图层感知的最新 revision，
    # 避免多图层场景下拿到错误图层的 revision 导致缓存命中失败。
    parsed = _parsed_map_region_read_signature(requested_signature)
    map_layer = parsed[1] if parsed is not None else None
    latest_revision = latest_map_revision(session, target, map_layer)
    for signature, revision in reversed(session.map_task_state.region_reads.items()):
        if latest_revision is not None and revision != latest_revision:
            continue
        if signature == requested_signature or _map_region_signature_contains(
            signature, requested_signature
        ):
            return signature
    return None


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__") and name not in {"_MODEL_LOG_FIELDS"}
]
