"""工具结果摘要：map/front 工具的结果压缩与展示。"""

from __future__ import annotations

import hashlib
import json
import re
from ._map_derivation import _MAP_ATLAS_SUMMARY_LIMIT, _MAP_CONTEXT_MAX_SUMMARY_CHARS, _MAP_MATCH_SUMMARY_LIMIT, _MAP_OBJECT_PLACEMENT_TOOL_NAMES
from ._text_utils import _truncate_text
from app.history_bounds import HISTORY_MAX_JSON_CHARS as _HISTORY_TOOL_MAX_JSON_CHARS, bounded_history_value as _bounded_history_value, bounded_tool_message_body as _bounded_tool_message_body, json_char_size as _json_char_size, summarize_history_text as _summarize_history_text
from app.orchestrator.map_workers import MAP_VALIDATION_TOOL_NAMES
from typing import Any
_HISTORY_PREVIEW_LIMIT = 2000


def _looks_like_create_plan_result(content: dict[str, Any]) -> bool:
    """判断工具结果是否是 `create_plan` 的内部回填载荷。"""
    tasks = content.get("tasks")
    note = str(content.get("note", ""))
    return bool(content.get("ok", False)) and isinstance(tasks, list) and "delegate_many" in note


def _format_create_plan_history_summary(input_args: dict[str, Any], content: dict[str, Any]) -> str:
    """把 `create_plan` 历史结果压缩成用户可读的计划摘要。"""
    summary = str(input_args.get("summary", "")).strip()
    raw_steps = input_args.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = content.get("tasks", [])
    steps = [step for step in raw_steps if isinstance(step, dict)]

    title = f"Plan created: {summary}" if summary else "Plan created"
    lines = [title]
    for index, step in enumerate(steps[:8], start=1):
        step_title = str(step.get("title", "")).strip()
        task = str(step.get("task", "")).strip()
        agent = str(step.get("agent", "")).strip()
        label = step_title or task or "Untitled step"
        suffix = f" ({agent})" if agent else ""
        lines.append(f"{index}. {label}{suffix}")
    if len(steps) > 8:
        lines.append(f"... {len(steps) - 8} more step(s)")
    return "\n".join(lines)


def _looks_like_delegate_group_result(content: dict[str, Any]) -> bool:
    """判断工具结果是否是 `delegate_many` 的子任务汇总载荷。"""
    results = content.get("results")
    if not isinstance(results, list) or not results:
        return False
    return all(isinstance(item, dict) and "summary" in item for item in results)


def _format_delegate_group_history_summary(content: dict[str, Any]) -> str:
    """把 `delegate_many` 子任务结果转换成可渲染 Markdown 的历史块。"""
    results = content.get("results")
    if not isinstance(results, list):
        return "Delegate results:"

    lines = ["Delegate results:"]
    for index, item in enumerate(results[:8], start=1):
        if not isinstance(item, dict):
            continue
        agent = str(item.get("agent", "")).strip()
        summary = str(item.get("summary", "")).strip()
        heading = f"**{index}. {agent or 'delegate'}**"
        lines.extend(["", heading, _truncate_text(summary or "No summary", 1600)])
    if len(results) > 8:
        lines.append("")
        lines.append(f"... {len(results) - 8} more result(s)")
    return "\n".join(lines)


def _looks_like_delegate_result(content: dict[str, Any]) -> bool:
    """判断工具结果是否是单个 `delegate` 子任务摘要。"""
    return "summary" in content and set(content.keys()).issubset(
        {"summary", "agent", "frame_id", "error"}
    )


def _format_delegate_history_summary(content: dict[str, Any]) -> str:
    """把单个 `delegate` 子任务结果转换成可渲染 Markdown 的历史块。"""
    agent = str(content.get("agent", "")).strip()
    summary = str(content.get("summary", "")).strip()
    title = f"Delegate result: {agent}" if agent else "Delegate result:"
    return f"{title}\n{_truncate_text(summary or 'No summary', 2000)}"


def _front_result_lines(value: Any, *, indent: int = 0, max_items: int = 80) -> list[str]:
    """把前端工具结果转为有界的 Markdown 列表，保留节点层级。"""
    prefix = "  " * indent
    if isinstance(value, dict):
        lines: list[str] = []
        for index, (key, item) in enumerate(value.items()):
            if index >= max_items:
                lines.append(f"{prefix}- ... (truncated)")
                break
            if isinstance(item, dict | list):
                lines.append(f"{prefix}- {key}:")
                lines.extend(_front_result_lines(item, indent=indent + 1, max_items=max_items))
            else:
                lines.append(f"{prefix}- {key}: {item}")
        return lines
    if isinstance(value, list):
        lines = []
        for index, item in enumerate(value):
            if index >= max_items:
                lines.append(f"{prefix}- ... (truncated)")
                break
            if isinstance(item, dict | list):
                lines.append(f"{prefix}- item {index + 1}:")
                lines.extend(_front_result_lines(item, indent=indent + 1, max_items=max_items))
            else:
                lines.append(f"{prefix}- {item}")
        return lines
    return [f"{prefix}- {value}"]


def _front_tool_error_message(result: dict[str, Any]) -> str:
    """提取前端工具结果中的错误摘要。"""
    if result.get("ok") is not False:
        return ""
    for key in ("message", "error", "error_code"):
        value = result.get(key)
        if value not in (None, ""):
            return str(value)
    return "Unknown error"


def _safe_artifact_name(value: str) -> str:
    """把任意字符串转成可作为 artifact 文件名片段的短标识。"""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    if cleaned:
        return cleaned[:80]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _region_summary_from_value(value: Any) -> dict[str, Any]:
    """从工具结果里抽取标准 region 字段。"""
    if not isinstance(value, dict):
        return {}
    region = value.get("region")
    if isinstance(region, dict):
        return dict(region)
    keys = ("x", "y", "z", "width", "height", "depth")
    return {key: value[key] for key in keys if key in value}


def _top_atlas_summary(value: Any, limit: int = _MAP_ATLAS_SUMMARY_LIMIT) -> Any:
    """截取 atlas_summary 的前 N 项，避免完整瓦片分布进入 history。"""
    if isinstance(value, list):
        return value[:limit]
    if not isinstance(value, dict):
        return value
    items = list(value.items())
    try:
        items.sort(
            key=lambda item: (
                int(item[1].get("count", item[1])) if isinstance(item[1], dict) else int(item[1])
            ),
            reverse=True,
        )
    except (TypeError, ValueError):
        pass
    return {str(key): entry for key, entry in items[:limit]}


def _map_result_summary(
    tool_name: str,
    result: dict[str, Any],
    artifact_ref: str | None,
    artifact_locator: dict[str, str] | None = None,
    effective_tools: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """把大型地图工具结果压缩成可进入 LLM history 的小摘要。"""
    if tool_name == "capture_viewport_screenshot":
        keep_keys = (
            "ok",
            "path",
            "absolute_path",
            "width",
            "height",
            "focus",
            "semantic_description",
            "semantic",
            "message",
            "error_code",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        for key in ("semantic_description", "semantic", "message"):
            if isinstance(summary.get(key), str):
                summary[key] = _truncate_text(str(summary[key]), _MAP_CONTEXT_MAX_SUMMARY_CHARS)
        for key in ("rendered_nodes", "nodes_missing_visual_resource"):
            value = result.get(key)
            if isinstance(value, list):
                summary[key] = value[:_MAP_MATCH_SUMMARY_LIMIT]
                summary[f"{key}_omitted"] = max(0, len(value) - _MAP_MATCH_SUMMARY_LIMIT)
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        if artifact_locator is not None:
            summary.update(artifact_locator)
        return summary

    if tool_name == "describe_map_region":
        cells = result.get("cells", [])
        summary: dict[str, Any] = {
            "ok": result.get("ok", True),
            "target": result.get("target", result.get("target_path")),
            "target_path": result.get("target_path", result.get("target")),
            "type": result.get("type"),
            "dimension": result.get("dimension"),
            "map_layer": result.get("map_layer"),
            "map_revision": result.get("map_revision"),
            "region": _region_summary_from_value(result),
            "used_bounds": result.get("used_bounds"),
            "layers": result.get("layers"),
            "cells_format": result.get("cells_format"),
            "cells_total": result.get("cells_total"),
            "cells_returned": result.get("cells_returned"),
            "non_empty_count": (
                result.get("non_empty_count")
                if "non_empty_count" in result
                else (len(cells) if isinstance(cells, list) else result.get("cells"))
            ),
            "cells_omitted": (
                result.get("cells_omitted")
                if "cells_omitted" in result
                else (isinstance(cells, list) and bool(cells))
            ),
            "artifact_ref": artifact_ref,
        }
        if "atlas_summary" in result:
            atlas_summary = _top_atlas_summary(result.get("atlas_summary"))
            summary["atlas_summary"] = atlas_summary
            summary["atlas_summary_top"] = atlas_summary
            summary["atlas_summary_omitted"] = True
        if artifact_locator is not None:
            summary.update(artifact_locator)
        if (
            artifact_locator is not None
            and "read_map_artifact" in effective_tools
            and (
                result.get("cells_omitted")
                or result.get("cells_returned") != result.get("cells_total")
            )
        ):
            summary["exact_cells_hint"] = (
                "需要精确 cell 坐标/atlas 时，调用 read_map_artifact，传入 "
                "artifact_ref、artifact_turn_id、artifact_entry_id，并用 field='cells' 分页；"
                "不要从 cells_total/non_empty_count/atlas_summary 推断具体坐标。"
            )
        for key in (
            "message",
            "warning",
            "warnings",
            "stale_warning",
            "suggested_map_layer",
            "next_expected_revision",
        ):
            if key in result:
                summary[key] = result[key]
        return {key: value for key, value in summary.items() if value is not None}

    if tool_name == "query_spatial_index":
        matches = result.get("matches", [])
        summary = dict(result)
        if isinstance(matches, list):
            summary["matches"] = matches[:_MAP_MATCH_SUMMARY_LIMIT]
            summary["matches_omitted"] = max(0, len(matches) - _MAP_MATCH_SUMMARY_LIMIT)
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        if artifact_locator is not None:
            summary.update(artifact_locator)
            if "read_map_artifact" in effective_tools:
                summary["artifact_read_hint"] = (
                    "使用 read_map_artifact 和该 map_tool_result 定位信息读取完整 matches。"
                )
        return summary

    if tool_name in _MAP_OBJECT_PLACEMENT_TOOL_NAMES:
        keep_keys = (
            "ok",
            "passed",
            "changed",
            "target",
            "target_path",
            "parent_path",
            "dimension",
            "map_layer",
            "map_revision",
            "region",
            "message",
            "error_code",
            "coords",
            "blocking_cell",
            "support_cells",
            "hint",
            "failed_index",
            "failed_object",
            "batch_atomic",
            "placement_profile",
            "candidate_source",
            "rejected_summary",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        for key, limit in (
            ("objects", 20),
            ("paths", 40),
            ("anchors", 24),
            ("placements", 40),
            ("issues", 40),
            ("repair_plan", 24),
            ("moved", 40),
            ("plans", 24),
        ):
            value = result.get(key)
            if isinstance(value, list):
                summary[key] = _bounded_history_value(
                    value[:limit],
                    max_string_chars=4000,
                    max_list_items=limit,
                    max_dict_items=80,
                )
                summary[f"{key}_omitted"] = max(0, len(value) - limit)
        if "instance_summary" in result:
            summary["instance_summary"] = _bounded_history_value(
                result["instance_summary"],
                max_string_chars=4000,
                max_list_items=40,
                max_dict_items=80,
            )
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        if artifact_locator is not None:
            summary.update(artifact_locator)
            if "read_map_artifact" in effective_tools:
                summary["artifact_read_hint"] = (
                    "使用 read_map_artifact 和该 map_tool_result 定位信息读取完整字段。"
                )
        return summary

    if tool_name in MAP_VALIDATION_TOOL_NAMES:
        keep_keys = (
            "ok",
            "passed",
            "completion_allowed",
            "blocking_completion",
            "target",
            "target_path",
            "map_layer",
            "map_revision",
            "region",
            "issues",
            "structured_issues",
            "message",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        if artifact_ref is not None:
            summary["artifact_ref"] = artifact_ref
        if artifact_locator is not None:
            summary.update(artifact_locator)
            if "read_map_artifact" in effective_tools:
                summary["artifact_read_hint"] = (
                    "使用 read_map_artifact 和该 map_tool_result 定位信息读取完整校验结果。"
                )
        return summary

    return result


def _front_tool_result_summary(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """为非地图前端工具生成有界 history 摘要。"""
    if tool_name in {
        "run_system_command",
        "execute_gd_script",
        "run_tests",
        "run_headless_self_test",
        "git_diff",
        "git_status",
        "export_project",
    }:
        keep_keys = (
            "ok",
            "status",
            "exit_code",
            "pid",
            "path",
            "shell",
            "working_directory",
            "timeout_ms",
            "output_truncated",
            "error_code",
            "message",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        output = result.get("output")
        if isinstance(output, str) and output:
            summary["output"] = _summarize_history_text(output, 24_000)
            summary["output_omitted_for_history"] = len(output) > 24_000
        return summary

    if tool_name == "read_class_docs":
        summary = {
            key: result[key]
            for key in ("source", "class_name", "parent", "path", "base", "load_error")
            if key in result
        }
        for key, limit in (("methods", 40), ("properties", 50), ("signals", 30)):
            value = result.get(key)
            if isinstance(value, list):
                summary[key] = _bounded_history_value(value[:limit], max_string_chars=2000)
                summary[f"{key}_omitted"] = max(0, len(value) - limit)
        constants = result.get("constants")
        if isinstance(constants, dict):
            items = list(constants.items())
            summary["constants"] = {str(key): value for key, value in items[:80]}
            summary["constants_omitted"] = max(0, len(items) - 80)
        return summary

    if tool_name == "read_image_metadata":
        keep_keys = (
            "ok",
            "path",
            "absolute_path",
            "width",
            "height",
            "format",
            "message",
            "error_code",
            "semantic_description",
            "semantic",
        )
        summary = {key: result[key] for key in keep_keys if key in result}
        colors = result.get("dominant_colors")
        if isinstance(colors, list):
            summary["dominant_colors"] = colors[:16]
            summary["dominant_colors_omitted"] = max(0, len(colors) - 16)
        for key in ("semantic_description", "semantic", "message"):
            if isinstance(summary.get(key), str):
                summary[key] = _truncate_text(str(summary[key]), _MAP_CONTEXT_MAX_SUMMARY_CHARS)
        return summary

    if tool_name == "read_resource":
        summary = {
            key: result[key]
            for key in ("ok", "path", "type", "script_path", "message", "error_code")
            if key in result
        }
        properties = result.get("properties")
        if isinstance(properties, dict):
            summary["properties"] = _bounded_history_value(
                properties,
                max_string_chars=2000,
                max_list_items=30,
                max_dict_items=80,
            )
        return summary

    if tool_name in {"read_scene_tree", "read_runtime_state"}:
        return _bounded_history_value(
            result,
            max_string_chars=2000,
            max_list_items=60,
            max_dict_items=100,
        )

    if tool_name == "validate_scene_state":
        summary = {
            key: result[key]
            for key in ("ok", "passed", "failed", "message", "error_code")
            if key in result
        }
        results = result.get("results")
        if isinstance(results, list):
            summary["results"] = _bounded_history_value(
                results[:40],
                max_string_chars=2000,
                max_list_items=40,
                max_dict_items=80,
            )
            summary["results_omitted"] = max(0, len(results) - 40)
        return summary

    if tool_name == "read_debugger_errors":
        items = result.get("items")
        summary = {"ok": result.get("ok", True)}
        if isinstance(items, list):
            summary["items"] = _bounded_history_value(items[:30], max_string_chars=4000)
            summary["items_omitted"] = max(0, len(items) - 30)
        return summary

    return _bounded_history_value(result)


def _history_payload_for_front_tool(
    tool_name: str,
    payload: dict[str, Any],
    artifact_ref: str | None,
    artifact_locator: dict[str, str] | None = None,
    effective_tools: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """生成写入 agent tool history 的瘦 payload。"""
    result = payload.get("result")
    if not isinstance(result, dict):
        return _bounded_tool_message_body(payload)
    if tool_name in {
        "capture_viewport_screenshot",
        "describe_map_region",
        "query_spatial_index",
        "validate_map_region",
        "validate_layer_coverage",
        "validate_object_placements",
        "place_map_objects",
        "find_placement_anchors",
        "repair_placements",
    }:
        slim = dict(payload)
        slim["result"] = _map_result_summary(
            tool_name,
            result,
            artifact_ref,
            artifact_locator,
            effective_tools,
        )
        return _bounded_tool_message_body(slim)
    if (
        tool_name
        in {
            "run_system_command",
            "execute_gd_script",
            "run_tests",
            "run_headless_self_test",
            "git_diff",
            "git_status",
            "export_project",
            "read_scene_tree",
            "read_runtime_state",
            "read_class_docs",
            "read_image_metadata",
            "read_resource",
            "validate_scene_state",
            "read_debugger_errors",
        }
        or _json_char_size(result) > _HISTORY_TOOL_MAX_JSON_CHARS
    ):
        slim = dict(payload)
        slim["result"] = _front_tool_result_summary(tool_name, result)
        return _bounded_tool_message_body(slim)
    slim = _bounded_tool_message_body(payload)
    if isinstance(slim, dict):
        return slim
    return slim


def _front_tool_summary(name: str, input_args: dict[str, Any], result: dict[str, Any]) -> str:
    """为前端工具生成可读摘要，并保留返回的节点与状态。"""
    title = name.replace("_", " ").capitalize()
    if name == "read_scene_tree":
        node_name = str(result.get("name", result.get("path", "Scene"))).strip()
        node_type = str(result.get("type", "Node")).strip()
        children = result.get("children", [])
        child_count = len(children) if isinstance(children, list) else 0
        suffix = f": {node_name} ({node_type})" if node_name else ""
        return f"Read scene tree{suffix}\n{child_count} top-level child node(s)"
    if name == "read_runtime_state":
        edited_scene = result.get("edited_scene", {})
        scene_name = (
            str(edited_scene.get("name", edited_scene.get("path", "Scene"))).strip()
            if isinstance(edited_scene, dict)
            else "Scene"
        )
        selected = result.get("selected_nodes", [])
        selected_count = len(selected) if isinstance(selected, list) else 0
        return f"Read runtime state: {scene_name}\n{selected_count} selected node(s)"
    if name == "read_image_metadata":
        path = str(result.get("path", input_args.get("path", ""))).strip()
        width = result.get("width")
        height = result.get("height")
        colors = result.get("dominant_colors", [])
        color_values = [
            str(item.get("hex", "")).strip()
            for item in colors
            if isinstance(item, dict) and str(item.get("hex", "")).strip()
        ][:5]
        lines = [f"Read image metadata: {path}" if path else "Read image metadata"]
        if width is not None and height is not None:
            lines.append(f"{width}x{height}")
        if color_values:
            lines.append("Dominant colors: " + ", ".join(color_values))
        return "\n".join(lines)
    if name == "capture_viewport_screenshot":
        path = str(result.get("path", result.get("absolute_path", ""))).strip()
        width = result.get("width")
        height = result.get("height")
        lines = [f"Capture viewport screenshot: {path}" if path else "Capture viewport screenshot"]
        if width is not None and height is not None:
            lines.append(f"{width}x{height}")
        return "\n".join(lines)
    if name == "read_class_docs":
        cls = input_args.get("class_name", "")
        title = f"Read class docs: {cls}" if cls else "Read class docs"
    elif name == "add_node":
        node_type = input_args.get("type", "")
        node_name = input_args.get("name", "")
        parent = input_args.get("parent_path", ".")
        title = f"Add {node_type} '{node_name}' under '{parent}'"
        error = _front_tool_error_message(result)
        if error:
            return f"{title}\nError: {error}"
        path = str(result.get("path", "")).strip()
        lines = [title]
        if path:
            lines.append(f"Done: `{path}`")
        return "\n".join(lines)
    elif name == "set_node_property":
        path = input_args.get("path", "")
        prop = input_args.get("property", "")
        value = input_args.get("value", "")
        title = f"Set {path}.{prop} = {value}"
    elif name == "instance_scene":
        scene_path = input_args.get("scene_path", "")
        parent = input_args.get("parent_path", ".")
        title = f"Instance {scene_path} under '{parent}'"
        error = _front_tool_error_message(result)
        if error:
            return f"{title}\nError: {error}"
        path = str(result.get("path", "")).strip()
        position = result.get("position", {})
        lines = [title]
        if path:
            lines.append(f"Done: `{path}`")
        if isinstance(position, dict) and ("x" in position or "y" in position):
            lines.append(f"Position: ({position.get('x', '?')}, {position.get('y', '?')})")
        return "\n".join(lines)
    elif name == "duplicate_node":
        path = input_args.get("path", "")
        title = f"Duplicate node {path}"
    elif name == "open_scene":
        path = input_args.get("path", "")
        title = f"Open scene {path}"
        error = _front_tool_error_message(result)
        if error:
            return f"{title}\nError: {error}"
        opened_path = str(result.get("path", path)).strip()
        root_name = str(result.get("root_name", "")).strip()
        root_type = str(result.get("root_type", "")).strip()
        lines = [title]
        if opened_path:
            lines.append(f"Done: `{opened_path}`")
        if root_name or root_type:
            lines.append(f"Root: {root_name} ({root_type})".strip())
        return "\n".join(lines)
    elif name == "save_scene":
        title = "Save current scene"
    elif name == "delete_node":
        path = input_args.get("path", "")
        title = f"Delete node {path}"
    elif name == "reparent_node":
        path = input_args.get("path", "")
        new_parent = input_args.get("new_parent_path", "")
        title = f"Reparent {path} under '{new_parent}'"
    elif name == "rename_node":
        path = input_args.get("path", "")
        new_name = input_args.get("name", "")
        title = f"Rename {path} to '{new_name}'"
    elif name == "connect_signal":
        path = input_args.get("path", "")
        signal = input_args.get("signal", "")
        target = input_args.get("target_path", "")
        method = input_args.get("method", "")
        title = f"Connect {path}.{signal} -> {target}.{method}"
    elif name == "disconnect_signal":
        path = input_args.get("path", "")
        signal = input_args.get("signal", "")
        target = input_args.get("target_path", "")
        method = input_args.get("method", "")
        title = f"Disconnect {path}.{signal} -> {target}.{method}"
    elif name == "add_to_group":
        path = input_args.get("path", "")
        group = input_args.get("group", "")
        title = f"Add {path} to group '{group}'"
    elif name == "remove_from_group":
        path = input_args.get("path", "")
        group = input_args.get("group", "")
        title = f"Remove {path} from group '{group}'"
    elif name == "bake_navigation_mesh":
        path = input_args.get("path", "")
        title = f"Bake navigation mesh for {path}"
    elif name == "create_animation_track":
        player = input_args.get("player_path", "")
        animation = input_args.get("animation", "")
        track_path = input_args.get("track_path", "")
        title = f"Set animation track {animation}@{player} ({track_path})"
    elif name == "run_tests":
        kind = input_args.get("kind", "")
        title = f"Run tests ({kind})" if kind else "Run tests"
    elif name == "run_headless_self_test":
        title = "Run headless self-test"
    elif name == "describe_map_context":
        error = _front_tool_error_message(result)
        if error:
            return f"Describe map context\nError: {error}"
        scene = str(result.get("scene", "")).strip()
        revision = result.get("map_revision")
        maps = result.get("maps", []) if isinstance(result.get("maps"), list) else []
        map_count = len(maps)
        total_layers = sum(
            len(m.get("layers", [])) if isinstance(m.get("layers"), list) else 0
            for m in maps
            if isinstance(m, dict)
        )
        total_cells = 0
        for m in maps:
            if isinstance(m, dict):
                for layer in (m.get("layers", []) if isinstance(m.get("layers"), list) else []):
                    if isinstance(layer, dict):
                        total_cells += int(layer.get("cell_count", 0) or 0)
        lines: list[str] = ["Describe map context"]
        if scene:
            lines.append(f"Scene: `{scene}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        lines.append(f"{map_count} map(s), {total_layers} layer(s), {total_cells} cell(s)")
        notes = result.get("notes", []) if isinstance(result.get("notes"), list) else []
        for note in notes[:3]:
            if isinstance(note, str):
                lines.append(f"  • {note}")
        return "\n".join(lines)
    elif name == "edit_map":
        error = _front_tool_error_message(result)
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        cells = result.get("cells")
        ops = result.get("operations")
        mode = str(result.get("mode", "")).strip()
        lines = ["Edit map"]
        if target:
            lines.append(f"Target: `{target}`")
        if error:
            lines.append(f"Error: {error}")
            if revision is not None:
                lines.append(f"Revision: {revision}")
            return "\n".join(lines)
        if revision is not None:
            lines.append(f"Revision: {revision}")
        if ops is not None:
            lines.append(f"Operations: {ops}")
        if cells is not None:
            lines.append(f"Cells: {cells}")
        if mode:
            lines.append(f"Mode: {mode}")
        validation = result.get("validation")
        if isinstance(validation, dict):
            v_passed = validation.get("passed")
            v_issues = (
                validation.get("issues", []) if isinstance(validation.get("issues"), list) else []
            )
            if v_passed is True:
                lines.append("Validation: Passed ✓")
            elif v_passed is False:
                lines.append("Validation: Failed ✗")
                for issue in v_issues[:3]:
                    if isinstance(issue, str):
                        lines.append(f"  • {issue}")
        gap = result.get("coverage_gap_warning")
        if gap and isinstance(gap, str):
            lines.append(f"Coverage gap: {gap}")
        return "\n".join(lines)
    elif name == "repair_layer_coverage":
        error = _front_tool_error_message(result)
        if error:
            return f"Repair layer coverage\nError: {error}"
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        repaired = result.get("repaired", False)
        cells = result.get("cells")
        lines = ["Repair layer coverage"]
        if target:
            lines.append(f"Target: `{target}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        if repaired:
            lines.append("Repaired ✓")
        else:
            lines.append("No repair needed")
        if cells is not None:
            lines.append(f"Cells: {cells}")
        return "\n".join(lines)
    elif name == "repair_placements":
        error = _front_tool_error_message(result)
        if error:
            return f"Repair placements\nError: {error}"
        target = str(result.get("target", "")).strip()
        repaired = result.get("repaired_count", result.get("repaired"))
        lines = ["Repair placements"]
        if target:
            lines.append(f"Target: `{target}`")
        if repaired is not None:
            lines.append(f"Repaired: {repaired}")
        return "\n".join(lines)
    elif name == "repair_map_region":
        error = _front_tool_error_message(result)
        if error:
            return f"Repair map region\nError: {error}"
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        lines = ["Repair map region"]
        if target:
            lines.append(f"Target: `{target}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        return "\n".join(lines)
    elif name == "compact_spatial_index":
        error = _front_tool_error_message(result)
        if error:
            return f"Compact spatial index\nError: {error}"
        entries = result.get("entries_total", result.get("entries"))
        lines = ["Compact spatial index: Done"]
        if entries is not None:
            lines.append(f"Entries: {entries}")
        return "\n".join(lines)
    elif name == "write_resource_registry":
        error = _front_tool_error_message(result)
        if error:
            return f"Write resource registry\nError: {error}"
        count = result.get("resource_count", result.get("count"))
        lines = ["Write resource registry: Done"]
        if count is not None:
            lines.append(f"Resources: {count}")
        return "\n".join(lines)
    elif name == "save_map_blueprint":
        error = _front_tool_error_message(result)
        if error:
            return f"Save map blueprint\nError: {error}"
        path = str(result.get("path", "")).strip()
        lines = ["Save map blueprint"]
        if path:
            lines.append(f"Saved: `{path}`")
        return "\n".join(lines)
    elif name == "apply_map_blueprint":
        error = _front_tool_error_message(result)
        if error:
            return f"Apply map blueprint\nError: {error}"
        target = str(result.get("target", "")).strip()
        lines = ["Apply map blueprint: Done"]
        if target:
            lines.append(f"Target: `{target}`")
        return "\n".join(lines)
    elif name == "ensure_standard_map_layers":
        error = _front_tool_error_message(result)
        if error:
            return f"Ensure standard map layers\nError: {error}"
        target = str(result.get("target", "")).strip()
        created = result.get("created_count", result.get("created"))
        lines = ["Ensure standard map layers"]
        if target:
            lines.append(f"Target: `{target}`")
        if created is not None:
            lines.append(f"Created: {created} layer(s)")
        return "\n".join(lines)
    elif name == "paint_terrain_connect":
        error = _front_tool_error_message(result)
        if error:
            return f"Paint terrain connect\nError: {error}"
        target = str(result.get("target", "")).strip()
        cells = result.get("cells")
        lines = ["Paint terrain connect"]
        if target:
            lines.append(f"Target: `{target}`")
        if cells is not None:
            lines.append(f"Cells: {cells}")
        return "\n".join(lines)
    elif name == "place_map_objects":
        error = _front_tool_error_message(result)
        if error:
            return f"Place map objects\nError: {error}"
        placed = result.get("placed_count", result.get("placed"))
        lines = ["Place map objects"]
        if placed is not None:
            lines.append(f"Placed: {placed} object(s)")
        return "\n".join(lines)
    elif name == "describe_map_region":
        error = _front_tool_error_message(result)
        if error:
            return f"Describe map region\nError: {error}"
        lines: list[str] = ["Describe map region"]

        # Check if this is a cell-focused result (has cells_format)
        cells_format = result.get("cells_format")
        if cells_format:
            cells_total = result.get("cells_total", 0)
            cells_returned = result.get("cells_returned", 0)
            non_empty_count = result.get("non_empty_count", 0)
            artifact_ref = result.get("artifact_ref", "")
            lines.append(
                f"Cells: {cells_total} total, {cells_returned} returned, {non_empty_count} non-empty"
            )
            if artifact_ref:
                lines.append(f"Artifact: `{artifact_ref}`")
        else:
            # Map overview result
            scene = str(result.get("scene", "")).strip()
            revision = result.get("map_revision")
            maps = result.get("maps", []) if isinstance(result.get("maps"), list) else []
            map_count = len(maps)
            total_layers = sum(
                len(m.get("layers", [])) if isinstance(m.get("layers"), list) else 0
                for m in maps
                if isinstance(m, dict)
            )
            total_cells = 0
            for m in maps:
                if isinstance(m, dict):
                    for layer in (m.get("layers", []) if isinstance(m.get("layers"), list) else []):
                        if isinstance(layer, dict):
                            total_cells += int(layer.get("cell_count", 0) or 0)
            if scene:
                lines.append(f"Scene: `{scene}`")
            if revision is not None:
                lines.append(f"Revision: {revision}")
            lines.append(f"{map_count} map(s), {total_layers} layer(s), {total_cells} cell(s)")

        notes = result.get("notes", []) if isinstance(result.get("notes"), list) else []
        for note in notes[:3]:
            if isinstance(note, str):
                lines.append(f"  • {note}")
        return "\n".join(lines)
    elif name == "validate_map_region":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate map region\nError: {error}"
        target = str(result.get("target", "")).strip()
        revision = result.get("map_revision")
        passed = result.get("passed", False)
        region = result.get("region", {}) if isinstance(result.get("region"), dict) else {}
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        lines = ["Validate map region"]
        if target:
            lines.append(f"Target: `{target}`")
        if revision is not None:
            lines.append(f"Revision: {revision}")
        if isinstance(region, dict) and region:
            x = region.get("x", "?")
            y = region.get("y", "?")
            w = region.get("width", "?")
            h = region.get("height", "?")
            lines.append(f"Region: ({x}, {y}) {w}×{h}")
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "validate_layer_coverage":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate layer coverage\nError: {error}"
        passed = result.get("passed", False)
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        lines = ["Validate layer coverage"]
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "validate_object_placements":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate object placements\nError: {error}"
        passed = result.get("passed", False)
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        checked = result.get("checked_count", result.get("total_checked"))
        lines = ["Validate object placements"]
        if checked is not None:
            lines.append(f"Checked: {checked} object(s)")
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "query_spatial_index":
        error = _front_tool_error_message(result)
        if error:
            return f"Query spatial index\nError: {error}"
        count = result.get("count", result.get("entries_count"))
        lines = ["Query spatial index"]
        if count is not None:
            lines.append(f"{count} entries")
        return "\n".join(lines)
    elif name == "find_placement_anchors":
        error = _front_tool_error_message(result)
        if error:
            return f"Find placement anchors\nError: {error}"
        anchors = result.get("anchors", []) if isinstance(result.get("anchors"), list) else []
        lines = ["Find placement anchors"]
        lines.append(f"{len(anchors)} anchor(s) found")
        return "\n".join(lines)
    elif name == "sample_noise_grid":
        error = _front_tool_error_message(result)
        if error:
            return f"Sample noise grid\nError: {error}"
        count = result.get("sample_count", result.get("count"))
        lines = ["Sample noise grid"]
        if count is not None:
            lines.append(f"{count} samples")
        return "\n".join(lines)
    elif name == "sample_poisson_points":
        error = _front_tool_error_message(result)
        if error:
            return f"Sample Poisson points\nError: {error}"
        count = result.get("point_count", result.get("count"))
        lines = ["Sample Poisson points"]
        if count is not None:
            lines.append(f"{count} points")
        return "\n".join(lines)
    elif name == "compose_map_blueprint_grammar":
        error = _front_tool_error_message(result)
        if error:
            return f"Compose map blueprint grammar\nError: {error}"
        lines = ["Compose map blueprint grammar: Done"]
        return "\n".join(lines)
    elif name == "validate_scene_state":
        error = _front_tool_error_message(result)
        if error:
            return f"Validate scene state\nError: {error}"
        passed = result.get("passed", False)
        issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
        lines = ["Validate scene state"]
        if passed:
            lines.append("Passed ✓")
        else:
            lines.append("Failed ✗")
            for issue in issues[:5]:
                if isinstance(issue, str):
                    lines.append(f"  • {issue}")
                elif isinstance(issue, dict):
                    lines.append(f"  • {issue.get('message', issue.get('type', str(issue)))}")
        return "\n".join(lines)
    elif name == "run_system_command":
        shell = str(result.get("shell", input_args.get("shell", "auto")))
        status = str(result.get("status", "unknown"))
        exit_code = result.get("exit_code")
        command = str(input_args.get("command", "")).strip()
        summary = f"Shell {command}" if command else "Run system command"
        detail = f"{status} (shell={shell}"
        if exit_code is not None:
            detail += f", exit={exit_code}"
        detail += ")"
        output = str(result.get("output", "")).strip()
        lines = [summary, detail]
        if output:
            lines.extend(["```", _truncate_text(output, 4000), "```"])
        return "\n".join(lines)
    error = _front_tool_error_message(result)
    if error:
        return f"{title}\nError: {error}"
    # Compact shallow fallback: only top-level scalar keys, never recurse into nested dicts/lists
    lines = [f"{title}:"]
    _SHALLOW_SKIP_KEYS = frozenset({"error", "errors", "detail", "details", "traceback"})
    for key, val in list(result.items())[:12]:
        if key in _SHALLOW_SKIP_KEYS:
            continue
        if isinstance(val, (dict, list)):
            if isinstance(val, list):
                lines.append(f"{key}: {len(val)} item(s)")
            else:
                lines.append(f"{key}: {len(val)} field(s)")
        elif val not in (None, ""):
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


def _display_tool_content(content: str) -> str:
    """Pretty-print JSON tool content when possible，并截断过长内容。"""
    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return _truncate_text(content, _HISTORY_PREVIEW_LIMIT)
    text = json.dumps(parsed, ensure_ascii=False, indent=2)
    return "```json\n" + _truncate_text(text, _HISTORY_PREVIEW_LIMIT) + "\n```"


def _compact_tool_summary(name: str, inner: dict[str, Any], input_args: dict[str, Any]) -> str:
    """Generate concise tool result summary instead of full JSON dump.

    For tools not in specific categories (read/edit/grep), create a short
    key-value style summary showing only important fields, similar to the
    frontend EventFormatter display.

    Args:
        name: Tool name
        inner: Parsed tool result dictionary
        input_args: Tool input arguments

    Returns:
        Compact summary string, e.g., "Validate map region:\n• passed: True\n• issues_count: 0"
    """
    # Extract important status/result fields
    summary_parts = []

    # Common status fields
    if "ok" in inner:
        summary_parts.append(f"ok: {inner['ok']}")
    if "passed" in inner:
        summary_parts.append(f"passed: {inner['passed']}")
    if "success" in inner:
        summary_parts.append(f"success: {inner['success']}")
    if "status" in inner:
        summary_parts.append(f"status: {inner['status']}")

    # Common result fields
    if "message" in inner:
        msg = str(inner["message"])
        if len(msg) > 100:
            msg = msg[:100] + "..."
        summary_parts.append(f"message: {msg}")
    if "result" in inner and not isinstance(inner["result"], (dict, list)):
        summary_parts.append(f"result: {inner['result']}")
    if "count" in inner:
        summary_parts.append(f"count: {inner['count']}")
    if "issues_count" in inner:
        summary_parts.append(f"issues_count: {inner['issues_count']}")
    if "issues" in inner and isinstance(inner["issues"], list):
        count = len(inner["issues"])
        if count > 0:
            summary_parts.append(f"issues: {count} item(s)")

    # Path/file related fields
    if "path" in inner:
        summary_parts.append(f"path: {inner['path']}")
    if "file_path" in inner:
        summary_parts.append(f"file_path: {inner['file_path']}")

    # If no important fields found, show a minimal summary
    if not summary_parts:
        # Show a few generic fields if present
        for key in ["target", "region", "data", "output"]:
            if key in inner:
                val = inner[key]
                if isinstance(val, dict):
                    summary_parts.append(f"{key}: {len(val)} field(s)")
                elif isinstance(val, list):
                    summary_parts.append(f"{key}: {len(val)} item(s)")
                elif isinstance(val, str) and len(val) <= 100:
                    summary_parts.append(f"{key}: {val}")
                else:
                    summary_parts.append(f"{key}: {type(val).__name__}")

    # Format as bullet list
    if summary_parts:
        display_name = name.replace("_", " ").title()
        return f"{display_name}:\n" + "\n".join(f"• {part}" for part in summary_parts)
    else:
        # Fallback: just show tool name with success status
        display_name = name.replace("_", " ").title()
        return f"{display_name}: completed"


__all__ = [
    name
    for name in globals()
    if name.startswith("_") and not name.startswith("__") and name not in {"_MODEL_LOG_FIELDS"}
]
