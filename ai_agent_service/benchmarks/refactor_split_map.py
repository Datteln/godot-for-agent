"""为 Map turn clean cut 生成按领域归属拆分的 apply_patch 补丁。"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


GROUPS: dict[str, list[str]] = {
    "contracts": [
        "FrontToolCall", "_queued_front_call", "_PendingToolMessage",
        "_PendingServerCall", "_PendingItem", "_tool_message", "MAX_AGENT_DEPTH",
        "EVENT_TEXT_PREVIEW_CHARS", "EVENT_MATCH_PREVIEW_ITEMS",
        "NOOP_SEARCH_TOOLS_HINT_THRESHOLD", "_INTEGER_TEXT", "_NUMBER_TEXT",
        "logger", "AgentPromptFactory",
    ],
    "frame_info": [
        "_find_frame", "_frame_in_active_map_edit", "_map_output_schema_for_frame",
        "_map_stage_for_frame", "_frame_objective", "_frame_semantic_operation",
        "_map_frame_exhausted_payload",
    ],
    "planning": [
        "_with_plan_runtime_metadata", "_plan_step_started", "_plan_step_completed",
        "_normalize_plan_steps", "_handle_create_plan", "_PLAN_COMPLEXITY_LEVELS",
    ],
    "delegation": ["_delegate_child_frame", "_start_delegate_frame"],
    "delegation_continuation": [
        "_map_delegate_result_summary", "_map_delegate_result_payload",
        "_continue_delegate_group",
    ],
    "delegation_group": ["_record_macro_owner", "_start_delegate_group"],
    "structured_completion": [
        "_normalized_map_layers", "_normalized_map_layer_value",
        "_json_object_from_text", "_json_parse_offset", "_slim_map_delegate_value",
        "_map_structured_output_error", "_repair_map_structured_output",
        "_payload_revision", "_same_payload_target", "_blocker_required_revision",
        "_clear_map_blockers", "_append_map_blocker_once",
        "_apply_reader_structured_completion", "_apply_map_structured_completion_result",
        "_MAP_WORKER_RESULT_FIELDS", "_MAP_WORKER_STAGES", "_MAP_OUTPUT_SCHEMA_V1",
        "_MAP_DELEGATE_LIST_LIMIT", "_MAP_DELEGATE_TEXT_LIMIT",
        "_MAP_DELEGATE_DROP_KEYS",
    ],
    "frame_lifecycle": ["_finish_frame", "_handle_frame_turns_exhausted"],
    "tool_arguments": ["_coerce_schema_value", "_normalize_tool_args", "_load_tool_args"],
    "tool_guards": [
        "_planner_route_guard", "_map_route_contract_error",
        "_append_delegate_protocol_errors", "_append_create_plan_protocol_errors",
        "_agent_name_has_role", "_map_agent_targets_from_delegate_call",
        "_requires_create_plan_before_map_delegate", "_append_map_write_protocol_errors",
        "_append_map_plan_protocol_errors", "_append_reader_fallback_protocol_errors",
        "_has_pending_map_write_validation", "_map_validation_arg_error",
        "_is_delegate_map_followup", "_append_map_write_followup_protocol_errors",
        "_MAP_FOLLOWUP_AGENT_ROLES",
    ],
    "budgets": [
        "_uses_persistent_map_budget", "_latest_map_progress_revision",
        "_sync_map_progress_budget", "_map_turn_exhausted",
    ],
    "tool_dispatch": [
        "_map_stage_contract", "_route_unvalidated_platform_writes_to_validator",
        "_stage_effective_tools", "_region_contains", "_cached_map_region_summary",
        "_resumed_full_map_read_error", "_with_map_write_metadata",
    ],
    "events": [
        "_event_tool_args", "_event_result_count", "_event_result_summary",
        "_event_match_items", "_emit_orchestration_event", "_history_timeline_payload",
        "_estimate_stream_token_count", "_delta_callback", "_record_cache_metrics",
        "_emit_cache_hit_event", "_emit_context_usage_event", "_fallback_callback",
    ],
}

DOCS: dict[str, str] = {
    "contracts": "定义 Map turn 处理器共享的封闭数据合同与常量。",
    "frame_info": "提供 Map Frame 的只读身份、阶段与预算描述。",
    "planning": "处理 Map 计划创建、步骤启动与完成投影。",
    "delegation": "创建单个 Map 委派 Frame 并绑定领域合同。",
    "delegation_continuation": "处理 Map 委派结果归并与父级继续执行。",
    "delegation_group": "处理 Map 并行委派组的创建与所有权绑定。",
    "structured_completion": "解析、校验并应用 Map worker 的结构化完成结果。",
    "frame_lifecycle": "处理 Map Frame 完成与预算耗尽转换。",
    "tool_arguments": "解析并规范化 Map 工具参数。",
    "tool_guards": "执行 Map 工具、计划与委派协议守卫。",
    "budgets": "维护 Map turn 的持久预算与耗尽结果。",
    "tool_dispatch": "准备 Map 工具可见性、缓存与写入元数据。",
    "events": "把 Map 执行事实投影为安全的编排事件。",
}


def _defined_names(node: ast.stmt) -> list[str]:
    """返回一个顶层语句创建的模块符号。"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    return []


def _import_bindings(node: ast.Import | ast.ImportFrom) -> set[str]:
    """返回 import 语句在本模块绑定的名称。"""
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.split(".")[0] for alias in node.names}
    return {alias.asname or alias.name for alias in node.names}


def render_patch(module: str, source_path: Path) -> str:
    """从旧 pipeline 精确抽取一个领域模块并返回 apply_patch 文本。"""
    source = source_path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source)
    nodes: dict[str, ast.stmt] = {}
    for node in tree.body:
        for name in _defined_names(node):
            nodes[name] = node
    owner = {name: group for group, names in GROUPS.items() for name in names}
    selected = sorted({nodes[name] for name in GROUPS[module]}, key=lambda item: item.lineno)
    loaded = {
        item.id
        for node in selected
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }
    import_nodes = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
    ]
    imports: list[tuple[int, str]] = []
    seen: set[str] = set()
    for node in import_nodes:
        if loaded & _import_bindings(node):
            text = ast.get_source_segment(source, node)
            if text is not None and text not in seen:
                imports.append((node.lineno, text))
                seen.add(text)
    cross_by_module: dict[str, list[str]] = {}
    for name in sorted(loaded):
        dependency = owner.get(name)
        if dependency is not None and dependency != module:
            cross_by_module.setdefault(dependency, []).append(name)
    content = [f'"""{DOCS[module]}"""', "", "from __future__ import annotations", ""]
    for _, text in sorted(imports):
        content.extend(text.splitlines())
    if imports and cross_by_module:
        content.append("")
    for dependency, names in sorted(cross_by_module.items()):
        if len(names) == 1:
            content.append(f"from app.orchestrator.map_turn.{dependency} import {names[0]}")
        else:
            content.append(f"from app.orchestrator.map_turn.{dependency} import (")
            content.extend(f"    {name}," for name in names)
            content.append(")")
    content.append("")
    for node in selected:
        content.extend(source_lines[node.lineno - 1 : node.end_lineno])
        content.extend(("", ""))
    while content and not content[-1]:
        content.pop()
    content.append("")
    generated = "\n".join(content)
    ast.parse(generated, filename=f"{module}.py")
    target = source_path.parent / "map_turn" / f"{module}.py"
    patch = ["*** Begin Patch", f"*** Add File: {target.resolve()}"]
    patch.extend(f"+{line}" for line in generated.splitlines())
    patch.append("*** End Patch")
    return "\n".join(patch)


def render_runner_patch(source_path: Path) -> str:
    """抽取旧 pipeline 的单步 runner，供后续继续拆解模型与工具阶段。"""
    source = source_path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source)
    nodes: dict[str, ast.stmt] = {}
    for node in tree.body:
        for name in _defined_names(node):
            nodes[name] = node
    policy = nodes["MapTurnPolicy"]
    loaded = {
        item.id
        for item in ast.walk(policy)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }
    owner = {name: group for group, names in GROUPS.items() for name in names}
    imports: list[tuple[int, str]] = []
    seen: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if loaded & _import_bindings(node):
            text = ast.get_source_segment(source, node)
            if text is not None and text not in seen:
                imports.append((node.lineno, text))
                seen.add(text)
    cross_by_module: dict[str, list[str]] = {}
    for name in sorted(loaded):
        dependency = owner.get(name)
        if dependency is not None:
            cross_by_module.setdefault(dependency, []).append(name)
    content = [
        '"""驱动一次 Map turn 的单步转换，循环边界由 TurnDriver 持有。"""',
        "",
        "from __future__ import annotations",
        "",
    ]
    for _, text in sorted(imports):
        content.extend(text.splitlines())
    content.append("")
    for dependency, names in sorted(cross_by_module.items()):
        if len(names) == 1:
            content.append(f"from app.orchestrator.map_turn.{dependency} import {names[0]}")
        else:
            content.append(f"from app.orchestrator.map_turn.{dependency} import (")
            content.extend(f"    {name}," for name in names)
            content.append(")")
    content.extend(("", *source_lines[policy.lineno - 1 : policy.end_lineno], ""))
    generated = "\n".join(content)
    ast.parse(generated, filename="runner.py")
    target = source_path.parent / "map_turn" / "runner.py"
    patch = ["*** Begin Patch", f"*** Add File: {target.resolve()}"]
    patch.extend(f"+{line}" for line in generated.splitlines())
    patch.append("*** End Patch")
    return "\n".join(patch)


def main() -> int:
    """解析命令行并输出单个模块的补丁。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("module", choices=[*sorted(GROUPS), "runner"])
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    if args.module == "runner":
        print(render_runner_patch(args.source))
    else:
        print(render_patch(args.module, args.source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
