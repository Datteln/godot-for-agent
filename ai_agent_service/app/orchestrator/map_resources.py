"""地图写入资源语义边界。"""

from __future__ import annotations

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


def normalize_edit_map_resources(
    project_root: Path,
    tool_args: dict[str, Any],
) -> MapResourceNormalization:
    """拒绝未绑定 compiler approval 的裸 atlas 或 GridMap item 写入。

    新规划管线只允许 planner 表达语义资源。精确 TileSet/GridMap 标识必须由
    validator/compiler 从权威快照和当前资源注册表确定，服务层不再把 LLM 给出的
    裸标识反向猜成资源键。

    Args:
        project_root: 当前 Godot 工程根目录；保留该参数以维持调用边界稳定。
        tool_args: 已解析但尚未下发的 ``edit_map`` 参数。

    Returns:
        未包含裸资源身份时返回原义副本；否则返回类型化阻断结果。
    """
    del project_root
    normalized = deepcopy(tool_args)
    operations_value = normalized.get("operations")
    if not isinstance(operations_value, list):
        return MapResourceNormalization(args=normalized)

    for operation in operations_value:
        if not isinstance(operation, dict) or operation.get("action", "fill") != "fill":
            continue
        resource_value = operation.get("resource", operation.get("resource_key"))
        resource = resource_value.strip() if isinstance(resource_value, str) else ""
        raw_2d = any(
            key in operation
            for key in (
                "source_id",
                "atlas_x",
                "atlas_y",
                "atlas_coords",
                "alternative_tile",
            )
        )
        raw_3d = any(key in operation for key in ("item", "orientation"))
        if resource or not (raw_2d or raw_3d):
            continue
        return MapResourceNormalization(
            args=normalized,
            error_code="planner_raw_map_resource_rejected",
            error_message=(
                "edit_map fill 不再接受 planner/worker 提供的裸 TileSet atlas 或 "
                "GridMap item。请让 planner 只提交 semantic resource/reference cell，"
                "并执行 validator/compiler 生成与快照绑定的 approved batch。"
            ),
        )
    return MapResourceNormalization(args=normalized)
