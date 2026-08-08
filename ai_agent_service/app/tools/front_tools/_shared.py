"""Shared schema helpers and registration wrapper for front-facing Godot tools."""

from __future__ import annotations

from typing import Any

# 从 map_workers 引入：
# - MAP_TARGET_REQUIRED_TOOL_NAMES：必须显式传 target_path 的地图写工具集合
# - requires_map_revision：判断工具是否需要携带 expected_revision 版本号（防并发覆盖）
from app.orchestrator.map_workers import MAP_TARGET_REQUIRED_TOOL_NAMES, requires_map_revision
from app.tools.registry import ToolDef
from app.tools.registry import register as _register_tool


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def _authoritative_snapshot_binding_properties() -> dict[str, Any]:
    """返回仅由运行时注入的平台规划快照身份字段。"""
    return {
        "authoritative_snapshot_id": {"type": "string"},
        "authoritative_snapshot_digest": {"type": "string"},
        "authoritative_snapshot_target": {"type": "string"},
        "authoritative_snapshot_layer": {"type": "integer"},
        "authoritative_snapshot_revision": {"type": "integer"},
        "authoritative_snapshot_coverage_complete": {"type": "boolean"},
        "authoritative_snapshot_traversal_complete": {"type": "boolean"},
        "authoritative_snapshot_frontier_complete": {"type": "boolean"},
    }


def _worker_spec_schema() -> dict[str, Any]:
    """返回动态地图 worker 的参数 schema。"""
    return _object_schema(
        {
            "name": {"type": "string"},
            "objective": {"type": "string"},
            "mode": {
                "type": "string",
                "enum": [
                    "read_only",
                    "propose_only",
                    "write_one_batch",
                    "review_only",
                    "repair_propose",
                    "repair_write_one_batch",
                ],
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional additional map skills. For planner modes the service derives and "
                    "adds required pipeline skills from canonical operation names."
                ),
            },
            "operations": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Canonical registered tool names for this worker stage; planner operations "
                    "deterministically select required planning skills."
                ),
            },
            "constraints": {
                "type": "array",
                "items": _object_schema(
                    {
                        "validator": {"type": "string"},
                        "required_args": {"type": "object"},
                    },
                    ["validator"],
                ),
            },
            "output_schema": {"type": "string", "enum": ["map_worker_result_v1"]},
            "authoritative_snapshot": {
                "type": "object",
                "description": (
                    "Legacy single-context planner input; runtime migrates it into a one-entry "
                    "planning_context_bundle."
                ),
                "properties": {
                    "artifact_ref": {"type": "string"},
                    "snapshot_id": {"type": "string"},
                    "digest": {"type": "string"},
                    "target_path": {"type": "string"},
                    "map_layer": {"type": "integer"},
                    "map_revision": {"type": "integer"},
                    "execution_eligible": {"type": "boolean"},
                },
                "required": [
                    "artifact_ref",
                    "snapshot_id",
                    "digest",
                    "target_path",
                    "map_layer",
                    "map_revision",
                ],
            },
            "planning_context_bundle": {
                "type": "object",
                "description": (
                    "Runtime-owned planner reference contexts; entries may use different "
                    "targets, layers, regions, and source revisions."
                ),
                "properties": {
                    "bundle_id": {"type": "string"},
                    "required_roles": {"type": "array", "items": {"type": "string"}},
                    "contexts": {
                        "type": "array",
                        "items": _object_schema(
                            {
                                "context_id": {"type": "string"},
                                "semantic_role": {"type": "string"},
                                "artifact_ref": {"type": "string"},
                                "digest": {"type": "string"},
                                "provenance": {"type": "object"},
                                "target_path": {"type": "string"},
                                "map_layer": {"type": "integer"},
                                "region": {"type": "object"},
                                "source_revision": {"type": "integer"},
                                "fact_fields": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "fresh": {"type": "boolean"},
                            },
                            [
                                "context_id",
                                "semantic_role",
                                "artifact_ref",
                                "digest",
                            ],
                        ),
                    },
                },
                "required": ["contexts"],
            },
            "required_context_roles": {
                "type": "array",
                "items": {"type": "string"},
            },
            "approved_batch": {
                "type": "object",
                "description": (
                    "Required for write modes; immutable planner/validator artifact identity "
                    "for the exact target and revision."
                ),
                "properties": {
                    "artifact_ref": {"type": "string"},
                    "batch_id": {"type": "string"},
                    "target_path": {"type": "string"},
                    "map_layer": {"type": "integer"},
                    "map_revision": {"type": "integer"},
                    "snapshot_id": {"type": "string"},
                    "snapshot_digest": {"type": "string"},
                    "batch_fingerprint": {"type": "string"},
                    "execution_operations": {
                        "type": "array",
                        "items": _object_schema(
                            {
                                "operation_id": {"type": "string"},
                                "target_path": {"type": "string"},
                                "map_layer": {"type": "integer"},
                                "expected_revision": {"type": "integer"},
                                "write_payload": {"type": "object"},
                                "artifact_ref": {"type": "string"},
                                "batch_id": {"type": "string"},
                            },
                            [
                                "operation_id",
                                "target_path",
                                "map_layer",
                                "expected_revision",
                                "write_payload",
                            ],
                        ),
                    },
                },
                "required": [
                    "artifact_ref",
                    "batch_id",
                    "target_path",
                    "map_layer",
                    "map_revision",
                    "snapshot_id",
                    "snapshot_digest",
                    "batch_fingerprint",
                ],
            },
            "stage_id": {"type": "string"},
            "max_turns": {"type": "integer", "minimum": 1, "maximum": 12},
        },
        ["name", "objective", "mode", "operations", "output_schema"],
    )


def register(tool: ToolDef) -> None:
    """注册前端工具，并给地图写工具补齐版本字段 schema。"""
    if tool.domain == "map" and tool.writes_project:
        parameters = tool.schema.get("parameters")
        if isinstance(parameters, dict):
            properties = parameters.setdefault("properties", {})
            # 仅对需要地图版本守卫的写工具注入 expected_revision / plan_version 等字段，
            # 避免给只读或无需版本控制的工具添加多余参数
            if isinstance(properties, dict) and requires_map_revision(tool.name):
                properties.setdefault(
                    "expected_revision",
                    {
                        "type": "integer",
                        "description": (
                            "Current map_revision returned by the latest read/validate tool. "
                            "The frontend rejects stale writes with map_revision_conflict."
                        ),
                    },
                )
                properties.setdefault(
                    "plan_version",
                    {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Service-managed version of the deterministic map batch plan.",
                    },
                )
                properties.setdefault(
                    "batch_index",
                    {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Zero-based position in the current deterministic batch queue.",
                    },
                )
                properties.setdefault(
                    "postconditions",
                    {
                        "type": "object",
                        "description": "Optional local assertions checked before the next queued batch is released.",
                    },
                )
            required = parameters.setdefault("required", [])
            # 同上：只有需要版本守卫的工具才把 expected_revision 加入必填
            if (
                isinstance(required, list)
                and requires_map_revision(tool.name)
                and "expected_revision" not in required
            ):
                required.append("expected_revision")
            # 只有明确要求 target_path 的写工具才把 target_path 加入必填，
            # 防止地图写入时静默使用隐式选中的地图
            if (
                isinstance(required, list)
                and tool.name in MAP_TARGET_REQUIRED_TOOL_NAMES
                and isinstance(properties, dict)
                and "target_path" in properties
                and "target_path" not in required
            ):
                required.append("target_path")
    _register_tool(tool)


placement_profile_properties = {
    "placement_kind": {
        "type": "string",
        "description": "Generic object placement preset: tree, decor, building, npc, enemy, chest, coin, etc.",
    },
    "kind": {"type": "string"},
    "anchor": {
        "type": "string",
        "enum": [
            "bottom_center",
            "bottom_left",
            "bottom_right",
            "top_center",
            "top_left",
            "top_right",
            "center",
        ],
        "description": "How the object footprint is aligned to the input cell; defaults to bottom_center.",
    },
    "surface_type": {
        "type": "string",
        "enum": [
            "ground",
            "wall",
            "water_surface",
            "water",
            "air",
            "room_center",
            "branch_end",
            "path_edge",
        ],
    },
    "footprint_width": {"type": "integer", "minimum": 1},
    "footprint_height": {"type": "integer", "minimum": 1},
    "footprint_depth": {"type": "integer", "minimum": 1},
    "requires_support": {"type": "boolean"},
    "support_mode": {"type": "string", "enum": ["bottom", "wall"]},
    "support_layers": {"type": "array", "items": {"type": "string"}},
    "forbidden_layers": {"type": "array", "items": {"type": "string"}},
    "clearance": {"type": "integer", "minimum": 0},
    "clearance_left": {"type": "integer", "minimum": 0},
    "clearance_right": {"type": "integer", "minimum": 0},
    "clearance_up": {"type": "integer", "minimum": 0},
    "clearance_down": {"type": "integer", "minimum": 0},
    "clearance_front": {"type": "integer", "minimum": 0},
    "clearance_back": {"type": "integer", "minimum": 0},
    "min_distance_to_protected": {"type": "integer", "minimum": 0},
    "preferred_distance_to_protected": {"type": "integer", "minimum": 0},
    "min_distance_from_same_kind": {"type": "integer", "minimum": 0},
    "requires_reachable": {
        "type": "boolean",
        "description": "When true, the anchor/interaction/entrance point must be reachable from start under traversal.",
    },
    "reachability_point": {
        "type": "string",
        "enum": ["anchor", "interaction", "entrance"],
        "description": "Which point to test for reachability; interaction/entrance use their offset from the anchor.",
    },
    "interaction_offset": {"type": "object"},
    "entrance_offset": {"type": "object"},
    "map_layer": {"type": "integer"},
    "ground_map_layer": {"type": "integer"},
    "start": {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "integer"},
            "z": {"type": "integer"},
            "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
        },
    },
    "traversal": {
        "type": "object",
        "description": "Character reachability rules, separate from the object's requires_support placement rule.",
        "properties": {
            "movement_model": {"type": "string", "enum": ["grid", "leap", "free"]},
            "path_algorithm": {"type": "string", "enum": ["bfs", "astar", "a*"]},
            "cell_occupancy": {"type": "string", "enum": ["empty", "filled"]},
            "requires_support": {"type": "boolean"},
            "support_occupancy": {"type": "string", "enum": ["empty", "filled"]},
            "max_horizontal_gap": {"type": "integer", "minimum": 1},
            "max_rise": {"type": "integer", "minimum": 0},
            "max_fall": {"type": "integer", "minimum": 0},
            "max_step": {"type": "integer", "minimum": 1},
            "gravity_axis": {"type": "string", "enum": ["x", "y", "z"]},
            "gravity_sign": {"type": "integer", "enum": [-1, 1]},
        },
        "required": [
            "movement_model",
            "cell_occupancy",
            "requires_support",
            "support_occupancy",
        ],
    },
    "protected_cells": {"type": "array", "items": {"type": "object"}},
    "path_cells": {"type": "array", "items": {"type": "object"}},
    "route_cells": {"type": "array", "items": {"type": "object"}},
    "frontier_cells": {"type": "array", "items": {"type": "object"}},
    "branch_ends": {"type": "array", "items": {"type": "object"}},
    "room_centers": {"type": "array", "items": {"type": "object"}},
    "reward_cells": {"type": "array", "items": {"type": "object"}},
}
