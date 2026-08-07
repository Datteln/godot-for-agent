"""从完整 runner 提取模型、响应路由与工具执行阶段补丁。"""

from __future__ import annotations

import argparse
import ast
import textwrap
from pathlib import Path

PHASES: dict[str, tuple[int, int]] = {
    "model_cycle": (1, 15),
    "response_routing": (15, 38),
    "tool_cycle": (38, 53),
}

ALIASES = [
    "session = context.runtime.session",
    "delegate_artifact_store = context.runtime.delegate_artifact_store",
    "frame_turns = context.runtime.frame_turns",
    "frame_edit_map_turns = context.runtime.frame_edit_map_turns",
    "llm = context.services.llm",
    "security = context.services.security",
    "tool_ctx = context.services.tool_context",
    "session_allow = context.services.session_allow",
    "agent_prompt_factory = context.services.prompt_factory",
    "model_selector = context.services.model_selector",
    "model_override = context.services.model_override",
    "thinking_budget_selector = context.services.thinking_budget_selector",
    "event_callback = context.services.event_callback",
    "cache_engine = context.services.cache_engine",
    "cache_metrics = context.services.cache_metrics",
    "max_turns = context.options.max_turns",
    "context_token_limit = context.options.context_token_limit",
    "map_worker_structured_output_enabled = context.options.structured_output_enabled",
    "map_worker_response_contract_mode = context.options.response_contract_mode",
    "map_worker_structured_correction_limit = context.options.structured_correction_limit",
    "map_worker_structured_thinking_budget = context.options.structured_thinking_budget",
]


def _bindings(node: ast.Import | ast.ImportFrom) -> set[str]:
    """返回 import 语句绑定的局部名称。"""
    if isinstance(node, ast.Import):
        return {alias.asname or alias.name.split(".")[0] for alias in node.names}
    return {alias.asname or alias.name for alias in node.names}


def render_phase(phase: str, source_path: Path) -> str:
    """生成一个阶段模块的完整新增补丁。"""
    source = source_path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = ast.parse(source)
    policy = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    run = next(
        node
        for node in policy.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    )
    advance = next(
        node
        for node in run.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "advance"
    )
    start, end = PHASES[phase]
    selected = advance.body[start:end]
    loaded = {
        item.id
        for node in selected
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load)
    }
    imports: list[tuple[int, str]] = []
    seen: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        if loaded & _bindings(node):
            text = ast.get_source_segment(source, node)
            if text is not None and text not in seen:
                imports.append((node.lineno, text))
                seen.add(text)
    doc = {
        "model_cycle": "准备 Map Frame、执行一次模型调用并返回显式阶段结果。",
        "response_routing": "分类一次 Map 模型响应并路由领域转换。",
        "tool_cycle": "执行 Map 工具守卫、服务端调用与前端挂起转换。",
    }[phase]
    content = [f'"""{doc}"""', "", "from __future__ import annotations", ""]
    for _, text in sorted(imports):
        content.extend(text.splitlines())
    content.extend(
        [
            "",
            "from app.orchestrator.map_turn.runtime import (",
            "    MapModelStep,",
            "    MapToolStep,",
            "    MapTurnContext,",
            ")",
            "",
        ]
    )
    if phase == "model_cycle":
        signature = (
            "async def run_model_cycle(\n"
            "    context: MapTurnContext,\n"
            "    loop_index: int,\n"
            ") -> ContinueModel | TurnOutcome | MapModelStep:"
        )
    elif phase == "response_routing":
        signature = (
            "async def route_model_response(\n"
            "    context: MapTurnContext,\n"
            "    step: MapModelStep,\n"
            ") -> ContinueModel | TurnOutcome | MapToolStep:"
        )
    else:
        signature = (
            "async def execute_tool_cycle(\n"
            "    context: MapTurnContext,\n"
            "    step: MapToolStep,\n"
            ") -> ContinueModel | TurnOutcome:"
        )
    content.extend(signature.splitlines())
    content.append(f'    """{doc}"""')
    for alias in ALIASES:
        content.append(f"    {alias}")
    if phase != "model_cycle":
        content.extend(
            [
                "    frame = step.frame",
                "    turn = step.turn",
                "    visible_effective_tools = step.visible_effective_tools",
                "    persistent_map_budget = step.persistent_map_budget",
            ]
        )
    if phase == "tool_cycle":
        content.append("    permission_ctx = step.permission_context")
    first = selected[0].lineno
    last = selected[-1].end_lineno
    body = textwrap.dedent("\n".join(source_lines[first - 1 : last]))
    content.extend(textwrap.indent(body, "    ").splitlines())
    if phase == "model_cycle":
        content.extend(
            [
                "    return MapModelStep(",
                "        frame=frame,",
                "        turn=turn,",
                "        visible_effective_tools=visible_effective_tools,",
                "        persistent_map_budget=persistent_map_budget,",
                "    )",
            ]
        )
    elif phase == "response_routing":
        content.extend(
            [
                "    return MapToolStep(",
                "        frame=frame,",
                "        turn=turn,",
                "        visible_effective_tools=visible_effective_tools,",
                "        persistent_map_budget=persistent_map_budget,",
                "        permission_context=permission_ctx,",
                "    )",
            ]
        )
    content.append("")
    generated = "\n".join(content)
    ast.parse(generated, filename=f"{phase}.py")
    target = source_path.parent / f"{phase}.py"
    patch = ["*** Begin Patch", f"*** Add File: {target.resolve()}"]
    patch.extend(f"+{line}" for line in generated.splitlines())
    patch.append("*** End Patch")
    return "\n".join(patch)


def main() -> int:
    """解析阶段名称并输出补丁。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=sorted(PHASES))
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(render_phase(args.phase, args.source))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
