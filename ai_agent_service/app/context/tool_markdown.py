"""工具结果的 Markdown 渲染（任务 3.1/3.5）。

每一个工具返回值在进入 LLM 上下文之前都渲染成 Markdown 段落：
`role=tool.content` 永不携带序列化结果 JSON，也不在本变更中引入任何
按工具类别的 Markdown 长度上限（既有工具自身对无界来源的安全边界保持
不变）。未知工具走有界的通用渲染：只保留结构化事实（键、计数、少量
标量），绝不把无界原始载荷带入长期模型上下文。
"""

from __future__ import annotations

import json
from typing import Any

from app.context.grouping import terminal_marker

FILE_READ_TOOLS: frozenset[str] = frozenset(
    {"read_file", "read_resource", "read_image_metadata"}
)
"""文件/资源读取类工具。"""

SEARCH_TOOLS: frozenset[str] = frozenset(
    {"grep_code", "search_codebase", "list_files", "search_tools"}
)
"""检索类工具。"""

MUTATION_TOOLS: frozenset[str] = frozenset(
    {
        "propose_script_edit",
        "propose_tests",
        "apply_text_edit",
        "propose_content_file",
        "create_resource",
        "create_sprite_frames_from_sheet",
        "create_animation_track",
        "create_shader_material",
        "set_project_setting",
        "add_autoload",
        "remove_autoload",
        "add_input_action",
        "remove_input_action",
    }
)
"""内容变更类工具（编辑/写入/配置）。"""

SCENE_NODE_TOOLS: frozenset[str] = frozenset(
    {
        "read_scene_tree",
        "add_node",
        "set_node_property",
        "delete_node",
        "reparent_node",
        "rename_node",
        "instance_scene",
        "duplicate_node",
        "connect_signal",
        "disconnect_signal",
        "add_to_group",
        "remove_from_group",
        "list_node_groups",
        "list_node_signals",
        "list_node_methods",
        "validate_scene_state",
        "list_groups",
        "get_current_scene_path",
        "save_scene",
        "list_open_scenes",
        "open_scene",
        "capture_viewport_screenshot",
        "bake_navigation_mesh",
        "reload_map_targets",
        "rebuild_map_builder",
        "set_resource_property",
    }
)
"""场景/节点操作类工具。"""

SYSTEM_COMMAND_TOOLS: frozenset[str] = frozenset(
    {
        "run_system_command",
        "execute_gd_script",
        "run_tests",
        "run_headless_self_test",
        "git_status",
        "git_diff",
        "export_project",
    }
)
"""系统命令/执行类工具。"""

CLASS_DOCS_TOOL = "read_class_docs"
"""ClassDB 按需查询工具；只保留被查询的有界成员/常量/匹配。"""

DELEGATE_TOOLS: frozenset[str] = frozenset({"delegate", "delegate_many"})
"""委派类工具。"""

MAP_TOOLS: frozenset[str] = frozenset({"describe_map_region", "describe_tilemap_selection"})
"""地图观察类工具：渲染为语义 Markdown 证据（任务 8.5）。"""

_GENERIC_MAX_SCALAR_ITEMS = 12
"""通用渲染里标量列表最多保留的条目数（有界通用事实，非按类别长度上限）。"""


def classify_tool(tool_name: str) -> str:
    """返回工具的渲染类别名（供审计与测试）。"""
    if tool_name in FILE_READ_TOOLS:
        return "file_read"
    if tool_name in SEARCH_TOOLS:
        return "search"
    if tool_name in MUTATION_TOOLS:
        return "mutation"
    if tool_name in MAP_TOOLS:
        return "map"
    if tool_name in SCENE_NODE_TOOLS:
        return "scene_node"
    if tool_name in SYSTEM_COMMAND_TOOLS:
        return "system_command"
    if tool_name == CLASS_DOCS_TOOL:
        return "class_docs"
    if tool_name in DELEGATE_TOOLS:
        return "delegate"
    return "generic"


def parse_result_payload(content: Any) -> Any:
    """把工具消息正文解析为结构化载荷；非 JSON 时原样返回字符串。"""
    if not isinstance(content, str):
        return content
    text = content.strip()
    if not text or text[0] not in "{[":
        return content
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return content


def _scalar(value: Any) -> str:
    """把一个值压成单行可读文本。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        compact = " ".join(value.split())
        return compact if len(compact) <= 200 else compact[:200] + "…"
    if isinstance(value, list):
        return f"[{len(value)} 项]"
    if isinstance(value, dict):
        return "{" + ", ".join(sorted(str(k) for k in value)) + "}"
    return str(value)


def _lines(values: list[str]) -> str:
    """把条目行拼成 Markdown 列表。"""
    return "\n".join(f"- {item}" for item in values)


def derive_identity(
    tool_name: str, input_args: dict[str, Any], payload: Any
) -> tuple[str, str]:
    """推导记录身份键与规范化目标（工具 + 目标）。

    Args:
        tool_name: 工具名。
        input_args: 工具入参。
        payload: 解析后的结果载荷。

    Returns:
        `(identity_key, target)` 二元组。
    """
    body = payload if isinstance(payload, dict) else {}

    def _arg(*keys: str) -> str:
        for key in keys:
            value = input_args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    target = ""
    if tool_name == CLASS_DOCS_TOOL:
        class_name = str(body.get("class_name") or _arg("class_name", "class"))
        mode = str(body.get("mode") or _arg("mode") or "overview")
        query = str(body.get("query") or _arg("query"))
        target = f"class={class_name} mode={mode}" + (f" query={query}" if query else "")
    elif tool_name in SEARCH_TOOLS:
        pattern = _arg("pattern", "query", "path")
        include = _arg("include")
        target = f"pattern={pattern}" + (f" include={include}" if include else "")
    elif tool_name in SYSTEM_COMMAND_TOOLS and tool_name in {
        "run_system_command",
        "execute_gd_script",
    }:
        command = _arg("command", "script", "code")
        target = command if len(command) <= 160 else command[:160] + "…"
    else:
        target = _arg(
            "path",
            "target_path",
            "file_path",
            "node_path",
            "scene_path",
            "parent_path",
            "name",
        )
    if not target:
        result_target = str(
            body.get("path")
            or body.get("node_path")
            or body.get("scene_path")
            or body.get("target")
            or ""
        )
        target = result_target
    identity_key = f"{tool_name}::{target or 'global'}"
    return identity_key, target or "(全局)"


def _unwrap_front_payload(payload: Any) -> tuple[Any, list[str]]:
    """解开前端回传包装（`{"status": ..., "result": ...}`）。

    Returns:
        `(内层结果, 状态说明行列表)`；非前端包装时原样返回。
    """
    if not isinstance(payload, dict):
        return payload, []
    status = payload.get("status")
    if status not in {"applied", "rejected", "error"}:
        return payload, []
    notes = [f"前端执行状态：{status}"]
    error_code = payload.get("error_code")
    if error_code:
        notes.append(f"错误码：{error_code}")
    artifact_refs = payload.get("artifact_refs")
    if isinstance(artifact_refs, list) and artifact_refs:
        notes.append("产物引用：" + ", ".join(str(item) for item in artifact_refs[:8]))
    if payload.get("grant_session_allow"):
        notes.append("已授予会话级总是允许")
    inner = payload.get("result")
    return inner, notes


def _status_of(payload: Any, is_error: bool) -> str:
    """推导结果状态行。"""
    if is_error:
        return "失败"
    if isinstance(payload, dict):
        if payload.get("ok") is False or payload.get("success") is False:
            return "失败"
        if payload.get("status") == "rejected":
            return "被用户拒绝"
        if payload.get("status") == "error":
            return "失败"
    return "成功"


def _render_file_read(tool_name: str, payload: Any) -> str:
    """文件读取类：保留路径、范围元数据与源代码摘录块。"""
    body = payload if isinstance(payload, dict) else {}
    lines: list[str] = []
    content = body.get("content")
    if isinstance(content, str) and content:
        fence = "gdscript" if str(body.get("path", "")).endswith(".gd") else ""
        lines.append("```" + fence)
        lines.append(content)
        lines.append("```")
    meta: list[str] = []
    for key in ("offset", "limit", "lines_returned", "total_lines_scanned"):
        if key in body:
            meta.append(f"{key}={body[key]}")
    if body.get("protected_line_count"):
        meta.append(f"protected_lines={body['protected_line_count']}（生成/超长行以定位符提示代替）")
    if body.get("has_more"):
        meta.append("has_more=true（可用 offset/limit 续读）")
    if meta:
        lines.append("范围元数据：" + "；".join(meta))
    for key in ("properties", "metadata", "resource_type"):
        if key in body:
            lines.append(f"{key}: {_scalar(body[key])}")
    return "\n".join(lines)


def _render_search(tool_name: str, input_args: dict[str, Any], payload: Any) -> str:
    """检索类：保留查询、命中数与既有工具已裁剪的命中摘录。"""
    body = payload if isinstance(payload, dict) else {}
    lines: list[str] = []
    pattern = input_args.get("pattern") or input_args.get("query") or body.get("pattern")
    if pattern:
        lines.append(f"查询：{_scalar(pattern)}")
    include = input_args.get("include") or body.get("include")
    if include:
        lines.append(f"范围：{_scalar(include)}")
    matches = body.get("matches")
    files = body.get("files")
    items: list[Any] = []
    if isinstance(matches, list):
        items = matches
    elif isinstance(files, list):
        items = files
    if items:
        count_key = body.get("match_count")
        lines.append(f"命中：{count_key if count_key is not None else len(items)} 条")
        for item in items[:40]:
            if isinstance(item, dict):
                path = item.get("path") or item.get("file") or ""
                line_no = item.get("line")
                text = item.get("text") or item.get("line_text") or ""
                location = f"{path}:{line_no}" if line_no is not None else str(path)
                text_compact = " ".join(str(text).split())
                if text_compact and len(text_compact) > 160:
                    text_compact = text_compact[:160] + "…"
                lines.append(f"  - {location}" + (f" — {text_compact}" if text_compact else ""))
            else:
                lines.append(f"  - {_scalar(item)}")
    elif tool_name == "search_tools":
        for key in ("activated_tools", "tools", "results"):
            value = body.get(key)
            if isinstance(value, list):
                lines.append(f"{key}：" + ", ".join(_scalar(item) for item in value[:24]))
    for key in ("truncated", "regex_timeout", "scanned_files"):
        if body.get(key):
            lines.append(f"{key}={body[key]}")
    return "\n".join(lines)


def _render_mutation(tool_name: str, input_args: dict[str, Any], payload: Any) -> str:
    """变更类：保留目标、应用要点与校验状态。"""
    body = payload if isinstance(payload, dict) else {}
    lines: list[str] = []
    target = (
        input_args.get("path")
        or input_args.get("target_path")
        or body.get("path")
        or body.get("target_path")
    )
    if target:
        lines.append(f"目标文件：{target}")
    for key in ("summary", "message", "applied", "diff_summary", "lines_changed", "workflow"):
        if key in body:
            lines.append(f"{key}: {_scalar(body[key])}")
    verification = body.get("verification") or body.get("verify")
    if verification is not None:
        lines.append(f"校验状态：{_scalar(verification)}")
    else:
        lines.append("校验状态：待校验（observed）")
    return "\n".join(lines)


def _render_scene_node(tool_name: str, input_args: dict[str, Any], payload: Any) -> str:
    """场景/节点操作类：保留场景/节点身份与操作结果。"""
    body = payload if isinstance(payload, dict) else {}
    lines: list[str] = []
    for key in ("scene_path", "node_path", "parent_path", "new_path", "group", "signal"):
        value = input_args.get(key) or body.get(key)
        if value:
            lines.append(f"{key}: {_scalar(value)}")
    for key in ("ok", "message", "summary", "result", "nodes", "warnings"):
        if key in body:
            lines.append(f"{key}: {_scalar(body[key])}")
    return "\n".join(lines)


def _render_system_command(tool_name: str, input_args: dict[str, Any], payload: Any) -> str:
    """系统命令类：保留用途、退出状态与诊断输出。"""
    body = payload if isinstance(payload, dict) else {}
    lines: list[str] = []
    command = input_args.get("command") or input_args.get("script") or body.get("command")
    if command:
        lines.append(f"命令：{_scalar(command)}")
    for key in ("exit_code", "status", "passed", "summary"):
        if key in body:
            lines.append(f"{key}: {_scalar(body[key])}")
    output = body.get("output") or body.get("stdout") or body.get("log")
    if isinstance(output, str) and output.strip():
        lines.append("```")
        lines.append(output)
        lines.append("```")
    errors = body.get("stderr") or body.get("errors")
    if errors:
        lines.append(f"诊断：{_scalar(errors)}")
    return "\n".join(lines)


def _member_label(item: Any) -> str:
    """提取成员/常量/匹配条目的身份名（任务 3.5：只保留被查询的名字）。"""
    if isinstance(item, dict):
        for key in ("name", "constant", "member", "symbol"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return _scalar(item)
    return _scalar(item)


def _render_class_docs(payload: Any) -> str:
    """ClassDB 查询（任务 3.5）：只保留被查询的有界成员/常量/匹配。

    完整 ClassDB 文档绝不进入模型上下文；overview 请求也只保留显式选定的
    有界子集。
    """
    body = payload if isinstance(payload, dict) else {}
    lines: list[str] = []
    class_name = body.get("class_name")
    mode = body.get("mode", "overview")
    if class_name:
        lines.append(f"类：{class_name}（mode={mode}）")
    if body.get("ok") is False:
        message = body.get("message") or body.get("error") or "查询失败"
        lines.append(f"结果：失败 — {_scalar(message)}")
        return "\n".join(lines)
    members = body.get("members")
    constants = body.get("constants")
    matches = body.get("matches") or body.get("results")
    if isinstance(members, list) and members:
        lines.append("返回成员（有界子集）：")
        for member in members[:60]:
            lines.append(f"  - {_member_label(member)}")
    if isinstance(constants, list) and constants:
        lines.append("返回常量（有界子集）：")
        for constant in constants[:60]:
            lines.append(f"  - {_member_label(constant)}")
    if isinstance(matches, list) and matches:
        lines.append("搜索匹配（有界子集）：")
        for match in matches[:60]:
            lines.append(f"  - {_member_label(match)}")
    overview = body.get("overview")
    if isinstance(overview, dict):
        # overview 模式同样只保留计数与名字列表的有界子集，不保留完整文档。
        summary_bits: list[str] = []
        for key, value in overview.items():
            if isinstance(value, list):
                names = ", ".join(_scalar(item) for item in value[:16])
                more = "…" if len(value) > 16 else ""
                summary_bits.append(f"{key}({len(value)}): {names}{more}")
            else:
                summary_bits.append(f"{key}: {_scalar(value)}")
        lines.extend(summary_bits[:12])
    return "\n".join(lines)


def _render_delegate(payload: Any) -> str:
    """委派结果：保留子 agent 摘要。"""
    body = payload if isinstance(payload, dict) else {}
    summary = body.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    results = body.get("results")
    if isinstance(results, list):
        return "\n".join(f"- {_scalar(item)}" for item in results[:16])
    return "委派完成（无摘要）。"


def _render_generic(payload: Any) -> str:
    """通用渲染：结构化事实（键/计数/少量标量），绝不整体序列化结果对象。"""
    if payload is None:
        return "结果：空。"
    if isinstance(payload, str):
        compact = payload.strip()
        return compact if compact else "结果：空。"
    if isinstance(payload, (int, float, bool)):
        return f"结果：{_scalar(payload)}"
    if isinstance(payload, list):
        lines = [f"结果列表：{len(payload)} 项"]
        for item in payload[:_GENERIC_MAX_SCALAR_ITEMS]:
            lines.append(f"- {_scalar(item)}")
        if len(payload) > _GENERIC_MAX_SCALAR_ITEMS:
            lines.append(f"- …（其余 {len(payload) - _GENERIC_MAX_SCALAR_ITEMS} 项未展开）")
        return "\n".join(lines)
    if isinstance(payload, dict):
        error = payload.get("error")
        if error is not None and len(payload) <= 3:
            return f"错误：{_scalar(error)}"
        lines = ["结果字段："]
        for key in sorted(payload):
            lines.append(f"- {key}: {_scalar(payload[key])}")
        return "\n".join(lines[: _GENERIC_MAX_SCALAR_ITEMS + 2])
    return f"结果：{_scalar(payload)}"


def _cell_identity(cell: dict[str, Any]) -> str:
    """提取一个单元格/网格项的瓦片身份（紧凑表示）。"""
    if not isinstance(cell, dict):
        return str(cell)
    if "item" in cell:
        return f"item={cell.get('item')} orient={cell.get('orientation', 0)}"
    source_id = cell.get("source_id", -1)
    try:
        if int(source_id) < 0:
            return "empty"
    except (TypeError, ValueError):
        pass
    atlas = cell.get("atlas_coords") or {}
    atlas_x = atlas.get("x", "?") if isinstance(atlas, dict) else "?"
    atlas_y = atlas.get("y", "?") if isinstance(atlas, dict) else "?"
    return f"source={source_id} atlas=({atlas_x},{atlas_y}) alt={cell.get('alternative_tile', 0)}"


def _render_map_region(payload: Any) -> str:
    """describe_map_region 语义证据（任务 8.5）。

    保留目标/类型/维度/层、坐标基准、节点位置、瓦片尺寸、请求与观察边界、
    截断与后续查询元数据、legacy 层元数据、瓦片身份与紧凑行连续段；绝不
    暴露前端结果 JSON 或序列化 `tile_data`。
    """
    body = payload if isinstance(payload, dict) else {}
    if body.get("ok") is False:
        message = body.get("message") or body.get("error") or "观察失败"
        lines = [f"- 结果：失败 — {str(message)[:240]}"]
        constraint = body.get("constraint")
        max_cells = body.get("max_cells")
        if constraint or max_cells:
            lines.append(f"- 约束：max_cells={max_cells}；{constraint}")
        return "\n".join(lines)

    lines: list[str] = []
    target = body.get("target")
    map_type = body.get("type")
    dimension = body.get("dimension")
    lines.append(f"- 目标：{target}（{map_type}，{dimension}D）")
    map_layer = body.get("map_layer")
    if map_layer is not None:
        lines.append(f"- legacy 图层：map_layer={map_layer}")
    node_position = body.get("node_position")
    if isinstance(node_position, dict):
        pos = ", ".join(f"{k}={v}" for k, v in node_position.items())
        lines.append(f"- 节点位置：{pos}")
    tile_size = body.get("tile_size") or body.get("cell_size")
    if isinstance(tile_size, dict):
        size = "x".join(str(v) for v in tile_size.values())
        basis = "tile_size" if body.get("tile_size") else "cell_size"
        lines.append(f"- {basis}：{size}")
        lines.append("- 坐标基准：地图单元格坐标；世界坐标 ≈ 节点位置 + 单元坐标 × 尺寸")
    requested = body.get("requested_bounds")
    observed = body.get("observed_bounds")
    if isinstance(requested, dict):
        lines.append(f"- 请求范围：{_bounds_text(requested)}")
    if isinstance(observed, dict):
        lines.append(f"- 观察范围：{_bounds_text(observed)}")
    requested_cells = body.get("requested_cells")
    observed_cells = body.get("observed_cells")
    if requested_cells is not None:
        lines.append(f"- 单元数：请求 {requested_cells} / 观察 {observed_cells}")
    if body.get("truncated"):
        lines.append("- 截断：是（观察或明细超出单次预算）")
        next_query = body.get("next_query")
        if isinstance(next_query, dict):
            hint = ", ".join(f"{k}={v}" for k, v in next_query.items() if k != "note")
            lines.append(f"- 后续查询：{hint}")
        elif isinstance(next_query, str):
            lines.append(f"- 后续查询：{next_query}")
    layers = body.get("layers")
    if isinstance(layers, list) and layers:
        layer_bits = []
        for layer in layers[:16]:
            if isinstance(layer, dict):
                layer_bits.append(
                    f"[{layer.get('index')}] {layer.get('name')}（enabled={layer.get('enabled')}）"
                )
        if layer_bits:
            lines.append("- legacy layers：" + "；".join(layer_bits))

    row_runs = body.get("row_runs")
    if isinstance(row_runs, list) and row_runs:
        identities: list[str] = []
        lines.append("- 行连续段（row_runs）：")
        for run in row_runs[:64]:
            if not isinstance(run, dict):
                continue
            identity = _cell_identity(run)
            if identity not in identities:
                identities.append(identity)
            z_part = f"z={run['z']} " if "z" in run else ""
            lines.append(
                f"  - {z_part}y={run.get('y')} x={run.get('x_start')}..{run.get('x_end')}: {identity}"
            )
        if identities:
            lines.append("- 观察到的瓦片身份：" + "；".join(identities[:24]))

    requested_cells_value = requested_cells if isinstance(requested_cells, int) else 9999
    cells = body.get("cells")
    if isinstance(cells, list) and cells and requested_cells_value <= 25:
        lines.append("- 精确单元明细（显式有界观察）：")
        for cell in cells[:25]:
            if isinstance(cell, dict):
                coords = cell.get("coords")
                lines.append(f"  - {coords}: {_cell_identity(cell)}")

    lines.append("- 变更前必须重新发起有界观察；此证据为当时快照（易失）。")
    return "\n".join(lines)


def _bounds_text(bounds: dict[str, Any]) -> str:
    """把边界字典压成单行文本。"""
    keys = ("x", "y", "z", "width", "height", "depth")
    return ", ".join(f"{k}={bounds[k]}" for k in keys if k in bounds)


def _render_tilemap_selection(payload: Any) -> str:
    """describe_tilemap_selection 结果（任务 7.8）：选择依赖 + 目标路径回退指引。"""
    body = payload if isinstance(payload, dict) else {}
    if body.get("ok"):
        path = body.get("path", "")
        map_type = body.get("type", "TileMapLayer")
        return (
            f"- 已选 {map_type}：{path}\n"
            f"- 后续观察：describe_map_region(target_path={path!r}, 有界 x/y/width/height)"
        )
    message = str(body.get("message", ""))
    lines = [f"- 结果：不可用 — {message[:200]}"]
    candidates = body.get("candidates")
    if isinstance(candidates, list) and candidates:
        lines.append("- 候选节点：" + ", ".join(str(item) for item in candidates[:12]))
    lines.append(
        "- 说明：describe_tilemap_selection 仅在编辑器选中 TileMapLayer 时有效，"
        "不能用于发现地图节点，也不支持 legacy TileMap/GridMap。"
    )
    lines.append(
        "- 回退：用场景事实（read_scene_tree/editor 证据）确认地图节点路径，然后调用 "
        "describe_map_region(target_path=...)；不要重复无参调用本工具。"
    )
    return "\n".join(lines)


def render_tool_result_markdown(
    tool_name: str,
    input_args: dict[str, Any],
    result: Any,
    *,
    is_error: bool = False,
    origin: str = "",
    freshness: str = "observed",
    verified: bool = False,
) -> str:
    """把一个工具返回值渲染为 Markdown 结果段落（任务 3.1）。

    输出永不包含整体序列化的结果 JSON；对无界未知载荷只保留有界结构化
    事实。既有工具自身对无界来源（文件内容、命令输出）的安全边界保持不变，
    本函数不额外施加按工具类别的长度上限。

    Args:
        tool_name: 工具名。
        input_args: 工具入参。
        result: 工具返回值（未序列化的对象或已序列化的正文字符串）。
        is_error: 是否错误结果。
        origin: 工具执行来源（server/front/delegate/system）。
        freshness: 当前结果的新鲜度。
        verified: 是否已通过后续校验。

    Returns:
        Markdown 形态的结果正文。
    """
    payload = parse_result_payload(result)
    inner, front_notes = _unwrap_front_payload(payload)
    if isinstance(inner, dict) and inner.get("error") is not None and len(inner) <= 3:
        is_error = True
    status = _status_of(payload, is_error)
    _, target = derive_identity(tool_name, input_args, payload if not front_notes else inner)

    verification = "已验证" if verified else "待校验"
    header = [
        f"### 工具结果：{tool_name}",
        f"- 目标：{target}",
        f"- 状态：{status}",
        f"- 来源：{origin or 'unknown'}",
        f"- 新鲜度：{freshness}；验证：{verification}",
    ]
    header.extend(f"- {note}" for note in front_notes)

    body_payload = inner if front_notes else payload
    if is_error and isinstance(body_payload, dict) and body_payload.get("error") is not None:
        body = f"错误：{_scalar(body_payload.get('error'))}"
    elif tool_name in FILE_READ_TOOLS:
        body = _render_file_read(tool_name, body_payload)
    elif tool_name in SEARCH_TOOLS:
        body = _render_search(tool_name, input_args, body_payload)
    elif tool_name in MUTATION_TOOLS:
        body = _render_mutation(tool_name, input_args, body_payload)
    elif tool_name in SCENE_NODE_TOOLS:
        body = _render_scene_node(tool_name, input_args, body_payload)
    elif tool_name in SYSTEM_COMMAND_TOOLS:
        body = _render_system_command(tool_name, input_args, body_payload)
    elif tool_name == CLASS_DOCS_TOOL:
        body = _render_class_docs(body_payload)
    elif tool_name == "describe_map_region":
        body = _render_map_region(body_payload)
    elif tool_name == "describe_tilemap_selection":
        body = _render_tilemap_selection(body_payload)
    elif tool_name in DELEGATE_TOOLS:
        body = _render_delegate(body_payload)
    else:
        body = _render_generic(body_payload)

    parts = "\n".join(header)
    if body.strip():
        parts += "\n\n" + body.strip()
    return parts


def render_terminal_markdown(
    tool_name: str,
    reason: str,
    *,
    detail: str = "",
    origin: str = "front",
) -> str:
    """渲染取消/拒绝/超时/重置等终结性工具结果的 Markdown 正文。

    Args:
        tool_name: 工具名。
        reason: 终结原因。
        detail: 可选补充说明。
        origin: 工具执行来源。

    Returns:
        带终结标记的 Markdown 正文。
    """
    lines = [
        terminal_marker(reason),
        f"### 工具结果：{tool_name}（终结）",
        f"- 状态：{reason}",
        f"- 来源：{origin}",
        "- 新鲜度：observed；验证：待校验",
        "- 说明：该工具调用未返回正常结果，此条为协议闭合的终结结果。",
    ]
    if detail.strip():
        lines.append(f"- 详情：{detail.strip()}")
    return "\n".join(lines)
