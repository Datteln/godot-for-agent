"""Markdown 工具记忆的合并、窗口与归一化操作（任务 3.2–3.6 / 1.3）。

所有操作都以 `ContextMemoryState` 为中心：

- 完成的工具协议组 → 渲染为 Markdown 记录并并入当前轮记忆；
- 活跃轮协议窗口（默认 12 组）溢出时，最老完整组从 `frame.messages`
  原子移除，其 Markdown 段落保留在当前轮记忆中；
- 用户轮成功结束时，当前轮记忆 **机械** 并入长期记忆，全部完成的
  OpenAI 协议消息移出下一轮上下文——不触发任何额外摘要模型调用；
- 历史编辑器上下文 JSON 归并为按身份可替换的当前编辑器事实；
- 单个结果超出剩余硬窗口时，退化为带身份的范围/续读记录。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.context.grouping import MessageGroup, group_messages, turn_retention_boundary
from app.context.models import (
    CONVERSATION_MEMORY_BLOCK,
    ContextMemoryState,
    EditorFact,
    ToolMemoryRecord,
)
from app.context.tool_markdown import (
    derive_identity,
    parse_result_payload,
    render_tool_result_markdown,
)
from app.llm.message_transformer import estimate_message_tokens, flatten_message_text

EDITOR_CONTEXT_MARKER = "\n\n[editor_context]\n"
"""用户消息里编辑器上下文 JSON 的分隔标记。"""

_EDITOR_FACT_MAX_ITEMS = 8
"""编辑器事实列表类字段最多保留的条目数（有界规范化，非按类别长度上限）。"""


def _utc_now() -> str:
    """返回当前 UTC ISO-8601 时间。"""
    return datetime.now(timezone.utc).isoformat()


def estimate_text_tokens(text: str) -> int:
    """估算一段文本的 token 数（复用消息估算器）。"""
    return estimate_message_tokens([{"role": "user", "content": text}])


def build_record(
    state: ContextMemoryState,
    *,
    tool_name: str,
    input_args: dict[str, Any],
    content: Any,
    call_id: str,
    origin: str,
    is_error: bool = False,
    terminal: bool = False,
    source: str = "",
) -> ToolMemoryRecord:
    """渲染一条工具结果并构造记忆记录（不改变状态）。

    Args:
        state: 当前记忆状态（用于分配记录身份）。
        tool_name: 工具名。
        input_args: 工具入参。
        content: 工具消息正文（Markdown 或旧式 JSON 字符串）。
        call_id: 对应的 tool_call id。
        origin: 执行侧（server/front/delegate/system）。
        is_error: 是否错误结果。
        terminal: 是否终结性结果。
        source: 来源补充说明。

    Returns:
        构造好的 `ToolMemoryRecord`。
    """
    payload = parse_result_payload(content)
    if isinstance(content, str) and content.lstrip().startswith("###"):
        markdown = content
    else:
        markdown = render_tool_result_markdown(tool_name, input_args, payload, is_error=is_error)
    identity_key, target = derive_identity(tool_name, input_args, payload)
    now = _utc_now()
    return ToolMemoryRecord(
        record_id=state.new_record_id(),
        tool_name=tool_name,
        identity_key=identity_key,
        target=target,
        markdown=markdown,
        freshness="observed",
        verified=False,
        terminal=terminal,
        origin=origin if origin in ("server", "front", "delegate", "system") else "front",
        source=source or origin,
        turn_id=state.current_turn_id,
        created_at=now,
        updated_at=now,
        call_ids=[call_id] if call_id else [],
    )


def merge_group_records(
    state: ContextMemoryState,
    messages: list[dict[str, Any]],
    group: MessageGroup,
    *,
    tool_names: dict[str, str] | None = None,
    tool_args: dict[str, dict[str, Any]] | None = None,
    origin: str = "front",
) -> int:
    """把一个完整工具协议组的结果并入当前轮记忆（任务 3.2）。

    幂等：已合并过的 tool_call id 会跳过。

    Args:
        state: 记忆状态。
        messages: 帧消息列表。
        group: 目标协议组（必须完整）。
        tool_names: 可选的 tool_call id → 工具名映射。
        tool_args: 可选的 tool_call id → 入参映射。
        origin: 工具结果的执行来源。

    Returns:
        本次新合并的记录数。
    """
    if group.kind != "tool_group" or not group.complete:
        return 0
    assistant_message = messages[group.start]
    names: dict[str, str] = {}
    raw_calls = assistant_message.get("tool_calls")
    parsed_args: dict[str, dict[str, Any]] = {}
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if isinstance(call, dict):
                call_id = str(call.get("id", "") or "")
                function = call.get("function")
                if call_id and isinstance(function, dict):
                    names[call_id] = str(function.get("name", "") or "")
                    raw_arguments = function.get("arguments", "{}")
                    try:
                        parsed = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                    except (TypeError, ValueError):
                        parsed = {}
                    if isinstance(parsed, dict):
                        parsed_args[call_id] = parsed
    if tool_names:
        names.update(tool_names)

    merged = 0
    for index in range(group.start + 1, group.end):
        message = messages[index]
        if message.get("role") != "tool":
            continue
        call_id = str(message.get("tool_call_id", "") or "")
        if not call_id or call_id in state.merged_call_ids:
            continue
        tool_name = names.get(call_id, "") or (tool_names or {}).get(call_id, "") or "unknown"
        args = (tool_args or {}).get(call_id, parsed_args.get(call_id, {}))
        content = message.get("content", "")
        record = build_record(
            state,
            tool_name=tool_name,
            input_args=args,
            content=content,
            call_id=call_id,
            origin=origin,
            is_error=False,
            terminal=group.terminal,
        )
        state.add_current_record(record)
        state.merged_call_ids.add(call_id)
        merged += 1
    return merged


def sync_current_turn_memory(
    state: ContextMemoryState,
    messages: list[dict[str, Any]],
    *,
    tool_names: dict[str, str] | None = None,
    tool_args: dict[str, dict[str, Any]] | None = None,
    origin: str = "front",
) -> int:
    """扫描消息，把尚未合并的完整协议组全部并入当前轮记忆。"""
    merged = 0
    for group in group_messages(messages):
        if group.kind == "tool_group" and group.complete:
            if all(call_id in state.merged_call_ids for call_id in group.call_ids):
                continue
            merged += merge_group_records(
                state,
                messages,
                group,
                tool_names=tool_names,
                tool_args=tool_args,
                origin=origin,
            )
    return merged


def remove_group_messages(messages: list[dict[str, Any]], group: MessageGroup) -> None:
    """从消息列表原子移除一个协议组的全部消息（任务 3.2）。"""
    del messages[group.start : group.end]


def enforce_active_group_window(
    messages: list[dict[str, Any]],
    state: ContextMemoryState,
    *,
    window: int,
) -> int:
    """活跃轮协议窗口溢出处理（任务 3.2）。

    当消息中完整协议组数量超过 `window` 时，从最老组开始：先确认其
    Markdown 记录已在当前轮记忆中，然后把 assistant + 全部配对结果原子
    移除；assistant 消息里的用户可见文本保留为助手事实。挂起组不参与。

    Args:
        messages: 帧消息列表（原地修改）。
        state: 记忆状态。
        window: 活跃轮协议窗口大小（默认 12）。

    Returns:
        被移除的协议组数量。
    """
    removed = 0
    while True:
        groups = group_messages(messages)
        completed = [group for group in groups if group.kind == "tool_group" and group.complete]
        if len(completed) <= window:
            break
        oldest = completed[0]
        assistant_message = messages[oldest.start]
        text = flatten_message_text(assistant_message.get("content")).strip()
        if text:
            state.assistant_facts.append(text)
        # 窗口收拢必须保留 Markdown：先补齐记录再移除协议消息。
        for call_id in oldest.call_ids:
            if call_id not in state.merged_call_ids:
                merge_group_records(state, messages, oldest)
                break
        remove_group_messages(messages, oldest)
        removed += 1
    return removed


def complete_user_turn(
    messages: list[dict[str, Any]],
    state: ContextMemoryState,
    *,
    protected_from: int | None = None,
) -> int:
    """用户轮成功结束时的机械收尾（任务 3.3）。

    1. 当前轮 Markdown 记录并入长期记忆（机械、无摘要模型调用）；
    2. 消息中全部 **已完成** 的工具协议组移出下一轮上下文；挂起组与
       `protected_from` 之后的当前请求区不动；
    3. 被移除 assistant 消息里的用户可见文本保留为助手事实。

    Args:
        messages: 帧消息列表（原地修改）。
        state: 记忆状态。
        protected_from: 受保护区起点（当前用户请求），其后的组不移除。

    Returns:
        被移除的协议组数量。
    """
    sync_current_turn_memory(state, messages)
    state.merge_current_turn()
    state.current_turn_id = None

    removed = 0
    while True:
        groups = group_messages(messages)
        target: MessageGroup | None = None
        for group in groups:
            if group.kind != "tool_group" or not group.complete:
                continue
            if protected_from is not None and group.start >= protected_from:
                continue
            target = group
            break
        if target is None:
            break
        assistant_message = messages[target.start]
        text = flatten_message_text(assistant_message.get("content")).strip()
        if text:
            state.assistant_facts.append(text)
        remove_group_messages(messages, target)
        removed += 1
    if removed:
        state.revision += 1
    return removed


def extract_editor_facts(context_payload: dict[str, Any]) -> dict[str, str]:
    """从编辑器上下文载荷提取有界规范化事实（任务 3.4）。

    Args:
        context_payload: `_build_user_content` 打包的上下文载荷，
            通常含 `context`/`language_hint`/`engine_version` 等键。

    Returns:
        identity → summary 的事实字典。
    """
    facts: dict[str, str] = {}
    context = context_payload.get("context")
    engine_version = context_payload.get("engine_version")
    language_hint = context_payload.get("language_hint")
    env_bits: list[str] = []
    if engine_version:
        env_bits.append(f"engine={engine_version}")
    if language_hint:
        env_bits.append(f"lang={language_hint}")
    if env_bits:
        facts["editor:env"] = "；".join(env_bits)
    if not isinstance(context, dict):
        return facts

    selection = context.get("selection")
    if isinstance(selection, dict) and selection:
        bits: list[str] = []
        for key in ("scene_path", "node_path", "script_path", "node_type", "name"):
            value = selection.get(key)
            if isinstance(value, str) and value:
                bits.append(f"{key}={value}")
        if bits:
            facts["editor:selection"] = "；".join(bits[:6])

    scene_tree = context.get("scene_tree")
    if isinstance(scene_tree, dict) and scene_tree:
        root = scene_tree.get("root") or scene_tree.get("name") or ""
        node_count = scene_tree.get("node_count")
        summary = f"root={root}" if root else "scene_tree"
        if node_count is not None:
            summary += f"；node_count={node_count}"
        facts["editor:scene_tree"] = summary

    debugger_errors = context.get("debugger_errors")
    if isinstance(debugger_errors, list) and debugger_errors:
        first = debugger_errors[0]
        first_text = first if isinstance(first, str) else str(first)
        facts["editor:debugger_errors"] = (
            f"{len(debugger_errors)} 条；首条：{first_text[:160]}"
        )

    diagnostics = context.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        facts["editor:diagnostics"] = f"{len(diagnostics)} 条"

    files = context.get("project_files") or context.get("referenced_files")
    if isinstance(files, list) and files:
        names: list[str] = []
        for item in files[:_EDITOR_FACT_MAX_ITEMS]:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.append(str(item.get("path") or item.get("name") or ""))
        more = "…" if len(files) > _EDITOR_FACT_MAX_ITEMS else ""
        facts["editor:files"] = ", ".join(name for name in names if name) + more

    tile_catalog = context.get("tile_catalog")
    if isinstance(tile_catalog, list) and tile_catalog:
        facts["editor:tile_catalog"] = f"{len(tile_catalog)} 项"
    return facts


def normalize_editor_context(
    state: ContextMemoryState,
    context_payload: dict[str, Any],
    *,
    turn_id: str | None,
) -> int:
    """把一次用户请求的编辑器上下文归并为当前编辑器事实（任务 3.4）。

    同一身份的新事实取代旧事实；返回更新的事实数。
    """
    extracted = extract_editor_facts(context_payload)
    now = _utc_now()
    for identity, summary in extracted.items():
        state.editor_facts[identity] = EditorFact(
            identity=identity,
            kind=identity.split(":", 1)[-1],
            summary=summary,
            turn_id=turn_id,
            updated_at=now,
        )
    state.revision += 1 if extracted else 0
    return len(extracted)


def split_user_content(content: str) -> tuple[str, str]:
    """把用户消息正文拆成 (纯文本, 编辑器上下文 JSON 字符串)。"""
    if not isinstance(content, str):
        return "", ""
    index = content.find(EDITOR_CONTEXT_MARKER)
    if index < 0:
        return content, ""
    return content[:index], content[index + len(EDITOR_CONTEXT_MARKER) :]


def strip_historical_editor_context(
    messages: list[dict[str, Any]],
    *,
    protected_from: int | None = None,
) -> int:
    """把受保护区之外的历史用户消息里的编辑器 JSON 替换为归并说明。

    当前用户请求（`protected_from` 之后）保留完整载荷；更早的用户消息
    只保留纯文本加一行指向当前编辑器事实的说明（任务 3.4）。

    Returns:
        被改写的用户消息数。
    """
    stripped = 0
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        if protected_from is not None and index >= protected_from:
            continue
        content = message.get("content")
        if not isinstance(content, str) or EDITOR_CONTEXT_MARKER not in content:
            continue
        text, _ = split_user_content(content)
        message["content"] = (
            text
            + "\n\n[editor_context 已归并为当前编辑器事实，见 [conversation_memory] 块]"
        )
        stripped += 1
    return stripped


def apply_range_continuation(
    record: ToolMemoryRecord,
    *,
    remaining_tokens: int,
) -> bool:
    """把超出剩余硬窗口的记录退化为范围/续读记录（任务 3.6）。

    保留前缀至预算内，记录被裁剪的起点与续读提示；被裁剪范围只能通过
    有界的后续读取（例如 read_file 指定行范围）重新获得。

    Args:
        record: 待检查的记录（原地修改）。
        remaining_tokens: 剩余硬上下文窗口 token 数。

    Returns:
        是否发生了范围裁剪。
    """
    if remaining_tokens <= 0:
        remaining_tokens = 1
    current = estimate_text_tokens(record.markdown)
    if current <= remaining_tokens:
        return False
    # 以行为单位保留前缀，避免切断代码块/表格；至少保留一行。
    lines = record.markdown.split("\n")
    kept: list[str] = []
    used = 0
    for line in lines:
        cost = estimate_text_tokens(line + "\n")
        if kept and used + cost > remaining_tokens:
            break
        kept.append(line)
        used += cost
    if not kept:
        kept = [lines[0][:200]]
    kept_text = "\n".join(kept)
    record.range_start = 0
    record.range_end = len(kept_text)
    record.has_more = True
    record.continuation_hint = (
        f"该结果超出剩余上下文窗口，仅保留前 {len(kept_text)} 字符；"
        "其余范围需通过带行号/偏移参数的有界后续读取获取"
        "（例如 read_file 指定 offset/limit）。"
    )
    record.markdown = kept_text + "\n\n> " + record.continuation_hint
    return True


def enforce_memory_budget(
    state: ContextMemoryState,
    *,
    budget_tokens: int,
    baseline_tokens: int = 0,
) -> int:
    """在整体预算内约束记忆体积（仅预算压缩时使用）。

    依次：仅在此整体预算边界按身份合并重复记录、对最大的当前轮/长期
    记录做范围裁剪；仍超预算时按"最旧且已被取代/终结"的顺序整体移除
    长期记录，最后再动态收拢非工具记忆条目。不引入按工具类别的长度上限。

    Returns:
        被裁剪或移除的记录数。
    """
    adjusted = 0

    def _total_tokens() -> int:
        return baseline_tokens + estimate_text_tokens(render_memory_block(state))

    total = _total_tokens()
    if total <= budget_tokens:
        return 0

    # 不在普通轮次去重；只有整体预算压缩确实需要收紧时才把同一身份的旧
    # 记录标记为 superseded，并保留最新记录。
    latest_by_identity: dict[str, ToolMemoryRecord] = {}
    for record in [*state.tool_records, *state.current_turn_records]:
        latest_by_identity[record.identity_key] = record
    for records in (state.tool_records, state.current_turn_records):
        for record in records:
            if latest_by_identity.get(record.identity_key) is not record:
                record.freshness = "superseded"
    all_records = [*state.current_turn_records, *state.tool_records]
    for record in sorted(all_records, key=lambda item: estimate_text_tokens(item.markdown), reverse=True):
        if total <= budget_tokens:
            break
        before = estimate_text_tokens(record.markdown)
        if apply_range_continuation(record, remaining_tokens=max(before // 2, 64)):
            total = _total_tokens()
            adjusted += 1
    while total > budget_tokens and state.tool_records:
        victim_index = -1
        for index, record in enumerate(state.tool_records):
            if record.freshness == "superseded" or record.terminal:
                victim_index = index
                break
        if victim_index < 0:
            victim_index = 0
        state.tool_records.pop(victim_index)
        total = _total_tokens()
        adjusted += 1
    sections = (
        state.assistant_facts,
        state.completed_work,
        state.facts,
        state.pending_work,
        state.decisions,
        state.constraints,
        state.goals,
    )
    for section in sections:
        while total > budget_tokens and section:
            section.pop(0)
            total = _total_tokens()
            adjusted += 1
    while total > budget_tokens and state.editor_facts:
        oldest_identity = next(iter(state.editor_facts))
        state.editor_facts.pop(oldest_identity)
        total = _total_tokens()
        adjusted += 1
    if adjusted:
        state.revision += 1
    return adjusted


def render_memory_block(
    state: ContextMemoryState,
    *,
    exclude_call_ids: set[str] | None = None,
) -> str:
    """把记忆状态渲染为命名 system 层 Markdown 块（任务 1.3）。

    该块注入在分层 system 层之后、最近对话轮之前；它不修改展示稿条目，
    也不改写持久化的原始 prompt-cache 标记。

    Args:
        state: 记忆状态。
        exclude_call_ids: 仍保留在出站协议组里的 tool_call id；对应的当前轮
            记录不重复注入，避免同一结果出现两份模型可见副本（任务 8.6）。
    """
    if state.is_empty():
        return ""
    lines: list[str] = [CONVERSATION_MEMORY_BLOCK]
    sections: list[tuple[str, list[str]]] = [
        ("目标", state.goals),
        ("约束", state.constraints),
        ("决定", state.decisions),
        ("已验证事实", state.facts),
        ("已完成工作", state.completed_work),
        ("待办工作", state.pending_work),
        ("助手承诺/说明", state.assistant_facts),
    ]
    for title, items in sections:
        if not items:
            continue
        lines.append(f"## {title}")
        lines.extend(f"- {item}" for item in items[-24:])
    if state.editor_facts:
        lines.append("## 当前编辑器状态")
        for fact in state.editor_facts.values():
            lines.append(f"- {fact.identity}: {fact.summary}")
    if state.tool_records:
        lines.append("## 长期工具记忆")
        for record in state.tool_records:
            freshness = record.freshness
            if record.terminal:
                freshness += "/terminal"
            lines.append(f"### {record.record_id} {record.tool_name} → {record.target}（{freshness}）")
            lines.append(record.markdown)
    excluded = exclude_call_ids or set()
    visible_current = [
        record
        for record in state.current_turn_records
        if not (set(record.call_ids) & excluded)
    ]
    if visible_current:
        lines.append("## 当前轮工具记忆")
        for record in visible_current:
            lines.append(f"### {record.record_id} {record.tool_name} → {record.target}")
            lines.append(record.markdown)
    return "\n".join(lines)


def render_editor_manifest(context_payload: dict[str, Any]) -> str:
    """把当前编辑器上下文载荷渲染为 Markdown 证据清单（任务 8.3）。

    输出有界的事实与受支持的后续定位符，**不包含**原始传输 JSON：
    模型拿到的是当前选择/场景身份/诊断计数/已知文件等可直接行动的事实，
    以及获取细节的前端工具定位符。

    Args:
        context_payload: `_build_user_content` 打包的上下文载荷。

    Returns:
        Markdown 形态的编辑器证据清单。
    """
    lines: list[str] = ["## 当前编辑器证据"]
    context = context_payload.get("context")
    env_bits: list[str] = []
    engine_version = context_payload.get("engine_version")
    language_hint = context_payload.get("language_hint")
    if engine_version:
        env_bits.append(f"engine={engine_version}")
    if language_hint:
        env_bits.append(f"lang={language_hint}")
    if env_bits:
        lines.append("- 环境：" + "；".join(env_bits))
    if not isinstance(context, dict):
        lines.append("- （本次请求未携带编辑器上下文）")
        return "\n".join(lines)

    selection = context.get("selection")
    if isinstance(selection, dict) and selection:
        bits: list[str] = []
        for key in ("scene_path", "node_path", "node_type", "script_path", "name"):
            value = selection.get(key)
            if isinstance(value, str) and value:
                bits.append(f"{key}={value}")
        if bits:
            lines.append("- 当前选择：" + "；".join(bits[:6]))

    scene_tree = context.get("scene_tree")
    if isinstance(scene_tree, dict) and scene_tree:
        root = scene_tree.get("root") or scene_tree.get("name") or ""
        node_count = scene_tree.get("node_count")
        summary = f"root={root}" if root else "scene_tree"
        if node_count is not None:
            summary += f"；{node_count} 个节点"
        lines.append(f"- 场景结构：{summary}（细节用 read_scene_tree 按路径查询）")

    debugger_errors = context.get("debugger_errors")
    if isinstance(debugger_errors, list) and debugger_errors:
        lines.append(f"- 调试错误：{len(debugger_errors)} 条（细节用 read_debugger_errors）")

    diagnostics = context.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        lines.append(f"- 诊断：{len(diagnostics)} 条")

    files = context.get("project_files") or context.get("referenced_files")
    if isinstance(files, list) and files:
        names: list[str] = []
        for item in files[:_EDITOR_FACT_MAX_ITEMS]:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                names.append(str(item.get("path") or item.get("name") or ""))
        more = "…" if len(files) > _EDITOR_FACT_MAX_ITEMS else ""
        lines.append(f"- 相关文件（{len(files)}）：" + ", ".join(n for n in names if n) + more)

    tile_catalog = context.get("tile_catalog")
    if isinstance(tile_catalog, list) and tile_catalog:
        lines.append(f"- 瓦片清单：{len(tile_catalog)} 项（地图域工具据此校验）")

    lines.append("可用后续定位符：")
    lines.append("- 场景/节点细节：read_scene_tree、set_node_property、read_runtime_state")
    lines.append("- 地图区域：describe_map_region(target_path=..., 有界 x/y/width/height)")
    lines.append("- 运行错误：read_debugger_errors；ClassDB API：read_class_docs 有界查询")
    return "\n".join(lines)


def mark_verified(state: ContextMemoryState, *, tool_name: str, target: str) -> int:
    """编辑校验通过后把同身份记录升级为已验证（新鲜度提升）。

    Returns:
        被升级的记录数。
    """
    identity_key = f"{tool_name}::{target}"
    upgraded = 0
    for record in (*state.current_turn_records, *state.tool_records):
        if record.identity_key == identity_key and record.freshness != "verified":
            record.freshness = "verified"
            record.verified = True
            record.updated_at = _utc_now()
            upgraded += 1
    if upgraded:
        state.revision += 1
    return upgraded

_BRIEF_LIMIT = 200
"""机械合并里单条用户/助手消息保留的有界要点长度。"""


def _brief(text: str) -> str:
    """把一段文本压成单行有界要点。"""
    compact = " ".join(text.split())
    if len(compact) > _BRIEF_LIMIT:
        compact = compact[:_BRIEF_LIMIT] + "…"
    return compact


def mechanical_merge_removed_messages(
    state: ContextMemoryState,
    removed_messages: list[dict[str, Any]],
) -> int:
    """把被收拢出模型上下文的消息机械合并进记忆状态（确定性，任务 4.2）。

    - user 消息 → `facts` 里的"用户请求"要点（剥离编辑器 JSON）；
    - 无工具调用的 assistant 消息 → `assistant_facts` 要点；
    - 完整工具协议组 → 按身份并入长期工具记忆（Markdown，已在消息内）；
    - 不保留任何原始消息预览或工具 JSON。

    Returns:
        合并产生的记忆条目数。
    """
    merged = 0
    for group in group_messages(removed_messages):
        if group.kind == "user":
            text = flatten_message_text(removed_messages[group.start].get("content"))
            body = text.split("\n\n[editor_context]\n", 1)[0]
            if body.strip():
                state.facts.append("用户请求：" + _brief(body))
                merged += 1
            continue
        if group.kind == "assistant":
            if group.assistant_text.strip():
                state.assistant_facts.append(_brief(group.assistant_text))
                merged += 1
            continue
        if group.kind == "tool_group" and group.complete:
            if group.assistant_text.strip():
                state.assistant_facts.append(_brief(group.assistant_text))
                merged += 1
            assistant_message = removed_messages[group.start]
            names: dict[str, str] = {}
            args_by_call: dict[str, dict[str, Any]] = {}
            raw_calls = assistant_message.get("tool_calls")
            if isinstance(raw_calls, list):
                for call in raw_calls:
                    if isinstance(call, dict):
                        call_id = str(call.get("id", "") or "")
                        function = call.get("function")
                        if call_id and isinstance(function, dict):
                            names[call_id] = str(function.get("name", "") or "")
                            raw_arguments = function.get("arguments", "{}")
                            try:
                                parsed = (
                                    json.loads(raw_arguments)
                                    if isinstance(raw_arguments, str)
                                    else raw_arguments
                                )
                            except (TypeError, ValueError):
                                parsed = {}
                            if isinstance(parsed, dict):
                                args_by_call[call_id] = parsed
            for index in range(group.start + 1, group.end):
                message = removed_messages[index]
                if message.get("role") != "tool":
                    continue
                call_id = str(message.get("tool_call_id", "") or "")
                if not call_id or call_id in state.merged_call_ids:
                    continue
                tool_name = names.get(call_id, "unknown")
                record = build_record(
                    state,
                    tool_name=tool_name,
                    input_args=args_by_call.get(call_id, {}),
                    content=message.get("content", ""),
                    call_id=call_id,
                    origin="system",
                    terminal=group.terminal,
                )
                state.tool_records.append(record)
                state.merged_call_ids.add(call_id)
                merged += 1
            continue
    if merged:
        state.revision += 1
    return merged


def retain_recent_turns(
    messages: list[dict[str, Any]],
    state: ContextMemoryState,
    *,
    retained_turns: int,
    protected_from: int | None = None,
) -> int:
    """按完整用户轮保留最近历史，更早的轮次机械收拢进记忆（任务 2.3）。

    边界永远落在用户轮起点，绝不切开用户轮或工具协议组；被收拢的消息先
    机械合并进 `state` 再从列表移除。

    Returns:
        被移除的消息数量。
    """
    system_end = 0
    for index, message in enumerate(messages):
        if message.get("role") == "system":
            system_end = index + 1
        else:
            break
    boundary = turn_retention_boundary(
        messages, retained_turns=retained_turns, protected_from=protected_from
    )
    if boundary <= system_end:
        return 0
    removed_messages = messages[system_end:boundary]
    mechanical_merge_removed_messages(state, removed_messages)
    del messages[system_end:boundary]
    state.revision += 1
    return len(removed_messages)
