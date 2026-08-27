"""模型上下文记忆包（optimize-llm-conversation-context）。

把"完整可见展示稿"与"发给模型的上下文"分离：帧级 Markdown 会话/工具
记忆、协议安全分组、Markdown 工具渲染、出站投影与脱敏审计。
"""

from app.context.consolidation import (
    mechanical_merge_removed_messages,
    render_memory_for_summary,
    semantic_consolidate,
)
from app.context.grouping import (
    TERMINAL_REASONS,
    MessageGroup,
    extract_tool_call_ids,
    group_messages,
    is_terminal_content,
    terminal_marker,
    terminal_reason_of,
    terminalize_pending_groups,
    tool_call_names,
    turn_retention_boundary,
    validate_projection,
)
from app.context.memory import (
    EDITOR_CONTEXT_MARKER,
    apply_range_continuation,
    build_record,
    complete_user_turn,
    enforce_active_group_window,
    enforce_memory_budget,
    extract_editor_facts,
    mark_verified,
    merge_group_records,
    normalize_editor_context,
    render_memory_block,
    retain_recent_turns,
    split_user_content,
    strip_historical_editor_context,
    sync_current_turn_memory,
)
from app.context.models import (
    CONVERSATION_MEMORY_BLOCK,
    ContextMemoryState,
    EditorFact,
    ToolMemoryRecord,
)
from app.context.projection import (
    ContextProjection,
    ContextProjectionSettings,
    build_context_audit,
    project_frame_messages,
)
from app.context.tool_markdown import (
    CLASS_DOCS_TOOL,
    classify_tool,
    derive_identity,
    parse_result_payload,
    render_terminal_markdown,
    render_tool_result_markdown,
)

__all__ = [
    "CLASS_DOCS_TOOL",
    "CONVERSATION_MEMORY_BLOCK",
    "EDITOR_CONTEXT_MARKER",
    "TERMINAL_REASONS",
    "ContextMemoryState",
    "ContextProjection",
    "ContextProjectionSettings",
    "EditorFact",
    "MessageGroup",
    "ToolMemoryRecord",
    "apply_range_continuation",
    "build_context_audit",
    "build_record",
    "classify_tool",
    "complete_user_turn",
    "derive_identity",
    "enforce_active_group_window",
    "enforce_memory_budget",
    "extract_editor_facts",
    "extract_tool_call_ids",
    "group_messages",
    "is_terminal_content",
    "mark_verified",
    "mechanical_merge_removed_messages",
    "merge_group_records",
    "normalize_editor_context",
    "parse_result_payload",
    "project_frame_messages",
    "render_memory_block",
    "render_terminal_markdown",
    "render_memory_for_summary",
    "render_tool_result_markdown",
    "retain_recent_turns",
    "semantic_consolidate",
    "split_user_content",
    "strip_historical_editor_context",
    "sync_current_turn_memory",
    "terminal_marker",
    "terminal_reason_of",
    "terminalize_pending_groups",
    "tool_call_names",
    "turn_retention_boundary",
    "validate_projection",
]
