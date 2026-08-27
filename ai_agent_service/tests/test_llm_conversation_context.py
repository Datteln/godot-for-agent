"""optimize-llm-conversation-context 测试（任务 6.1/6.2/6.3/6.4）。

覆盖：
- 协议安全分组、终结性结果、投影校验（单元）；
- Markdown 工具渲染（无按类别长度上限、未知工具有界、无结果 JSON）；
- 范围/续读记录、编辑器事实取代、整体预算压缩；
- Thought/转录专属条目不进入后续 LLM 请求（编排）；
- role=tool 为 Markdown、第 13 组保留于工具记忆、完成协议消息不进入
  下一轮、终结性前端结果、委派帧记忆转移、缓存断点兼容（集成）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.config import AppSettings
from app.context.grouping import (
    group_messages,
    is_terminal_content,
    terminal_marker,
    terminalize_pending_groups,
    validate_projection,
)
from app.context.memory import (
    apply_range_continuation,
    complete_user_turn,
    enforce_active_group_window,
    enforce_memory_budget,
    normalize_editor_context,
    render_memory_block,
    retain_recent_turns,
    strip_historical_editor_context,
    sync_current_turn_memory,
)
from app.context.models import CONVERSATION_MEMORY_BLOCK, ContextMemoryState, ToolMemoryRecord
from app.context.projection import (
    ContextProjectionSettings,
    build_context_audit,
    project_frame_messages,
)
from app.context.tool_markdown import (
    classify_tool,
    render_terminal_markdown,
    render_tool_result_markdown,
)
from app.events.store import EventStore
from app.llm.message_transformer import (
    build_stable_prefix,
    estimate_message_tokens,
    inject_cache_breakpoints,
)
from app.llm.provider import AssistantTurn, ToolCallRequest
from app.main import create_app
from app.query.engine import QueryEngine
from app.sessions.store import Session, SessionStore, session_from_dict, session_to_dict

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class _RecordingLLM:
    """按脚本返回预设响应，并记录每次实际收到的出站消息。"""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.requests: list[list[dict[str, Any]]] = []

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.requests.append(json.loads(json.dumps(messages, default=str)))
        spec = self._turns.pop(0) if self._turns else {"content": "done"}
        on_delta = kwargs.get("on_delta")
        reasoning = spec.get("reasoning", "")
        if on_delta is not None and reasoning:
            on_delta("reasoning", reasoning, None)
        content = spec.get("content", "")
        if on_delta is not None and content:
            on_delta("content", content, None)
        tool_calls = [
            ToolCallRequest(id=call["id"], name=call["name"], arguments=json.dumps(call["args"]))
            for call in spec.get("tool_calls", [])
        ]
        raw_message: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            raw_message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in tool_calls
            ]
        return AssistantTurn(raw_message=raw_message, content=content, tool_calls=tool_calls)


def _stub_llm() -> "_RecordingLLM":
    return _RecordingLLM([{"content": "ok"}])


def _make_engine(
    tmp_dir: str, *, threshold: int = 60_000, keep_recent: int = 12, min_new: int = 8
) -> QueryEngine:
    settings = AppSettings(
        llm_base_url="http://localhost",
        project_root=Path(tmp_dir),
        auto_compact_token_threshold=threshold,
        auto_compact_keep_recent=keep_recent,
        auto_compact_min_new_messages=min_new,
    )
    store = SessionStore(Path(tmp_dir) / "sessions")
    return QueryEngine(
        settings=settings,
        session_store=store,
        llm=_stub_llm(),
        event_store=EventStore(),
    )


def _tool_group(call_id: str, tool_name: str, content: str, *, args: str = "{}") -> list[dict[str, Any]]:
    """构造一个完整的 assistant+tool 协议组。"""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": args},
                }
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": content},
    ]


class GroupingTests(unittest.TestCase):
    """任务 2.1：协议组识别与终结性结果。"""

    def test_user_turn_and_tool_group_boundaries(self) -> None:
        messages = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "hi"},
            *_tool_group("c1", "read_file", '{"ok": true}'),
            {"role": "assistant", "content": "done"},
        ]
        groups = group_messages(messages)
        kinds = [group.kind for group in groups]
        self.assertEqual(kinds, ["system", "user", "tool_group", "assistant"])
        tool_group = groups[2]
        self.assertTrue(tool_group.complete)
        self.assertEqual(tool_group.call_ids, ["c1"])

    def test_pending_group_detected(self) -> None:
        messages = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}
                ],
            },
        ]
        groups = group_messages(messages)
        self.assertEqual(groups[-1].kind, "tool_group")
        self.assertFalse(groups[-1].complete)
        self.assertTrue(groups[-1].is_pending)

    def test_terminal_results_mark_group_terminal(self) -> None:
        content = render_terminal_markdown("read_scene_tree", "timeout", detail="前端超时")
        self.assertTrue(is_terminal_content(content))
        messages = [
            {"role": "system", "content": "core"},
            *_tool_group("c1", "read_scene_tree", content),
        ]
        groups = group_messages(messages)
        self.assertTrue(groups[-1].complete)
        self.assertTrue(groups[-1].terminal)

    def test_terminalize_pending_groups_adds_matching_result(self) -> None:
        messages = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "save_scene", "arguments": "{}"}},
                    {"id": "c2", "type": "function", "function": {"name": "open_scene", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": render_terminal_markdown("save_scene", "timeout")},
        ]
        appended = terminalize_pending_groups(messages, "reset")
        self.assertEqual(appended, 1)
        self.assertEqual(messages[-1]["tool_call_id"], "c2")
        self.assertTrue(is_terminal_content(messages[-1]["content"]))
        self.assertEqual(validate_projection(messages), [])

    def test_validate_projection_flags_orphans(self) -> None:
        messages = [
            {"role": "system", "content": "core"},
            {"role": "tool", "tool_call_id": "ghost", "content": "orphan"},
        ]
        violations = validate_projection(messages)
        self.assertTrue(any(item.startswith("orphan_tool_result") for item in violations))

    def test_validate_projection_allows_tail_pending_only(self) -> None:
        pending_mid = [
            {"role": "system", "content": "core"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}],
            },
            {"role": "user", "content": "later"},
        ]
        violations = validate_projection(pending_mid)
        self.assertTrue(any("pending_group_not_at_tail" in item for item in violations))


class MarkdownRenderingTests(unittest.TestCase):
    """任务 3.1/3.5：Markdown 渲染（无按类别上限、无结果 JSON）。"""

    def test_role_tool_content_is_markdown_not_json(self) -> None:
        result = {"path": "res://a.gd", "content": "extends Node\n", "offset": 1, "limit": 50,
                  "lines_returned": 1, "total_lines_scanned": 1, "has_more": False}
        markdown = render_tool_result_markdown("read_file", {"path": "res://a.gd"}, result)
        self.assertIn("### 工具结果：read_file", markdown)
        self.assertIn("res://a.gd", markdown)
        with self.assertRaises(ValueError):
            json.loads(markdown)

    def test_file_read_has_no_new_category_cap(self) -> None:
        big_content = "\n".join(f"line-{i}: print({i})" for i in range(2000))
        result = {"path": "res://big.gd", "content": big_content, "offset": 1, "limit": 2000,
                  "lines_returned": 2000, "total_lines_scanned": 2000, "has_more": False}
        markdown = render_tool_result_markdown("read_file", {"path": "res://big.gd"}, result)
        self.assertIn("line-1999: print(1999)", markdown)
        self.assertIn(big_content, markdown)

    def test_unknown_tool_uses_bounded_generic_markdown(self) -> None:
        huge_value = "x" * 50_000
        result = {"blob": huge_value, "items": list(range(500))}
        markdown = render_tool_result_markdown("mystery_tool", {}, result)
        self.assertNotIn(huge_value, markdown)
        self.assertIn("mystery_tool", markdown)
        with self.assertRaises(ValueError):
            json.loads(markdown)

    def test_class_docs_keeps_only_queried_member_names(self) -> None:
        result = {
            "ok": True,
            "class_name": "TileMap",
            "mode": "members",
            "members": [{"name": "set_cell", "signature": "set_cell(coords, source_id)"}],
        }
        markdown = render_tool_result_markdown(
            "read_class_docs", {"class_name": "TileMap", "mode": "members"}, result
        )
        self.assertIn("TileMap", markdown)
        self.assertIn("set_cell", markdown)
        self.assertNotIn("coords, source_id", markdown)

    def test_terminal_rendering_carries_marker(self) -> None:
        markdown = render_terminal_markdown("run_tests", "cancelled", detail="用户中断")
        self.assertTrue(is_terminal_content(markdown))
        self.assertIn("run_tests", markdown)

    def test_classify_tool_categories(self) -> None:
        self.assertEqual(classify_tool("read_file"), "file_read")
        self.assertEqual(classify_tool("grep_code"), "search")
        self.assertEqual(classify_tool("propose_script_edit"), "mutation")
        self.assertEqual(classify_tool("add_node"), "scene_node")
        self.assertEqual(classify_tool("run_system_command"), "system_command")
        self.assertEqual(classify_tool("read_class_docs"), "class_docs")
        self.assertEqual(classify_tool("delegate"), "delegate")
        self.assertEqual(classify_tool("never_seen"), "generic")


class RangeContinuationTests(unittest.TestCase):
    """任务 3.6：硬窗口范围/续读记录。"""

    def _record(self, markdown: str) -> ToolMemoryRecord:
        return ToolMemoryRecord(
            record_id="tm1",
            tool_name="read_file",
            identity_key="read_file::res://big.gd",
            target="res://big.gd",
            markdown=markdown,
            call_ids=["c1"],
        )

    def test_oversized_record_becomes_range_record(self) -> None:
        big = "\n".join(f"line-{i}" for i in range(5000))
        record = self._record("### 工具结果：read_file\n\n" + big)
        changed = apply_range_continuation(record, remaining_tokens=200)
        self.assertTrue(changed)
        self.assertTrue(record.has_more)
        self.assertEqual(record.range_start, 0)
        self.assertIsNotNone(record.range_end)
        self.assertIn("续", record.continuation_hint + record.markdown)

    def test_small_record_unchanged(self) -> None:
        record = self._record("### 工具结果：read_file\n\n短内容")
        self.assertFalse(apply_range_continuation(record, remaining_tokens=10_000))
        self.assertFalse(record.has_more)

    def test_enforce_memory_budget_trims_largest_records(self) -> None:
        state = ContextMemoryState()
        big = self._record("\n".join(f"filler-{i}" for i in range(3000)))
        state.tool_records.append(big)
        adjusted = enforce_memory_budget(state, budget_tokens=300, baseline_tokens=0)
        self.assertGreaterEqual(adjusted, 1)


class EditorSupersessionTests(unittest.TestCase):
    """任务 3.4：历史编辑器上下文归并为可替换的当前事实。"""

    def test_newer_fact_supersedes_older_identity(self) -> None:
        state = ContextMemoryState()
        normalize_editor_context(
            state,
            {"context": {"selection": {"node_path": "/root/A", "scene_path": "res://a.tscn"}}},
            turn_id="t1",
        )
        normalize_editor_context(
            state,
            {"context": {"selection": {"node_path": "/root/B", "scene_path": "res://b.tscn"}}},
            turn_id="t2",
        )
        fact = state.editor_facts["editor:selection"]
        self.assertIn("/root/B", fact.summary)
        self.assertNotIn("/root/A", fact.summary)
        self.assertEqual(fact.turn_id, "t2")

    def test_strip_historical_editor_context_keeps_current_payload(self) -> None:
        old_user = "旧问题\n\n[editor_context]\n" + json.dumps({"context": {"selection": {"node_path": "/root/A"}}})
        new_user = "新问题\n\n[editor_context]\n" + json.dumps({"context": {"selection": {"node_path": "/root/B"}}})
        messages = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": old_user},
            {"role": "assistant", "content": "旧回答"},
            {"role": "user", "content": new_user},
        ]
        stripped = strip_historical_editor_context(messages, protected_from=3)
        self.assertEqual(stripped, 1)
        self.assertNotIn("/root/A", str(messages[1]["content"]))
        self.assertIn("已归并", str(messages[1]["content"]))
        self.assertIn("/root/B", str(messages[3]["content"]))

    def test_editor_context_payload_becomes_editor_facts(self) -> None:
        state = ContextMemoryState()
        normalize_editor_context(
            state,
            {
                "context": {
                    "selection": {"node_path": "/root/Main/Player"},
                    "debugger_errors": ["NullReference in Player.gd"],
                    "project_files": ["a.gd", "b.tscn"],
                },
                "engine_version": "4.3",
            },
            turn_id="t1",
        )
        block = render_memory_block(state)
        self.assertIn("/root/Main/Player", block)
        self.assertIn("4.3", block)


class TurnRetentionTests(unittest.TestCase):
    """任务 2.3/3.2/3.3：完整轮保留、窗口溢出与轮次收尾。"""

    def _turn_messages(self, turn_index: int) -> list[dict[str, Any]]:
        call_id = f"call-{turn_index}"
        return [
            {"role": "user", "content": f"问题 {turn_index}"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{turn_index}.gd"})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json.dumps({"path": f"f{turn_index}.gd", "content": "extends Node", "offset": 1,
                                        "limit": 10, "lines_returned": 1, "total_lines_scanned": 1,
                                        "has_more": False}),
            },
            {"role": "assistant", "content": f"回答 {turn_index}"},
        ]

    def test_retain_recent_turns_folds_older_turns_into_memory(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": "core"}]
        for index in range(4):
            messages.extend(self._turn_messages(index))
        state = ContextMemoryState()
        removed = retain_recent_turns(messages, state, retained_turns=2)
        self.assertGreater(removed, 0)
        self.assertEqual(validate_projection(messages), [])
        remaining_text = json.dumps(messages, ensure_ascii=False)
        self.assertIn("问题 3", remaining_text)
        self.assertNotIn("问题 0", remaining_text)
        self.assertTrue(any("问题 0" in fact for fact in state.facts))
        self.assertTrue(any(record.target.endswith("f0.gd") for record in state.tool_records))

    def test_window_overflow_keeps_markdown_of_oldest_group(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": "core"}]
        messages.append({"role": "user", "content": "many"})
        for index in range(13):
            messages.extend(_tool_group(f"c{index}", "list_files", json.dumps({"pattern": "*", "files": [f"f{index}"], "truncated": False})))
        state = ContextMemoryState()
        state.current_turn_id = "t1"
        sync_current_turn_memory(
            state, messages, tool_args={f"c{i}": {"pattern": f"pattern-{i}"} for i in range(13)}
        )
        removed = enforce_active_group_window(messages, state, window=12)
        self.assertEqual(removed, 1)
        groups = [group for group in group_messages(messages) if group.kind == "tool_group"]
        self.assertEqual(len(groups), 12)
        self.assertEqual(len(state.current_turn_records), 13)
        self.assertEqual(validate_projection(messages), [])
        self.assertTrue(any(record.target or record.tool_name for record in state.current_turn_records))

    def test_complete_user_turn_removes_protocol_messages(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": "core"}]
        messages.extend(self._turn_messages(0))
        state = ContextMemoryState()
        state.current_turn_id = "t1"
        complete_user_turn(messages, state)
        self.assertEqual([m for m in messages if m.get("role") == "tool"], [])
        self.assertEqual([m for m in messages if m.get("role") == "assistant" and m.get("tool_calls")], [])
        self.assertEqual(len(state.tool_records), 1)
        self.assertIn("回答 0", json.dumps(messages, ensure_ascii=False))

    def test_pending_group_survives_complete_user_turn(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": "core"}]
        messages.extend(self._turn_messages(0))
        messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "pend", "type": "function", "function": {"name": "save_scene", "arguments": "{}"}}],
        })
        state = ContextMemoryState()
        state.current_turn_id = "t1"
        complete_user_turn(messages, state, protected_from=None)
        # 末尾挂起组保持原样（未配对结果，不能移除）
        self.assertEqual(messages[-1]["tool_calls"][0]["id"], "pend")


class ProjectionTests(unittest.TestCase):
    """任务 4.1/5.2/5.3：投影、命名记忆块与脱敏审计。"""

    def test_memory_block_is_system_layer_outside_protocol_sequence(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "hi"},
            *_tool_group("c1", "read_file", "### 工具结果：read_file\n- 目标：a.gd"),
        ]
        state = ContextMemoryState()
        state.facts.append("事实A")
        projection = project_frame_messages(messages, state, session_id="s", frame_id="f1")
        self.assertEqual(projection.messages[1]["role"], "system")
        self.assertTrue(projection.messages[1]["content"].startswith(CONVERSATION_MEMORY_BLOCK))
        self.assertIn("事实A", projection.messages[1]["content"])
        self.assertEqual(projection.violations, [])
        # 帧持久化消息不被投影改写
        self.assertEqual(len(messages), 4)

    def test_audit_is_redacted_counts_only(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "secret system prompt"},
            {"role": "user", "content": "private question"},
        ]
        state = ContextMemoryState()
        state.facts.append("secret-fact-text")
        audit = build_context_audit(messages, state, memory_injected=False)
        serialized = json.dumps(audit, ensure_ascii=False)
        self.assertNotIn("secret system prompt", serialized)
        self.assertNotIn("private question", serialized)
        self.assertNotIn("secret-fact-text", serialized)
        self.assertEqual(audit["message_count"], 2)
        self.assertIn("estimated_tokens", audit)
        self.assertIn("retained_turns", audit)
        self.assertIn("protocol_groups", audit)

    def test_prompt_cache_breakpoints_remain_valid_with_memory_block(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": [{"type": "text", "text": "L0"}, {"type": "text", "text": "L2"}]},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        state = ContextMemoryState()
        state.facts.append("事实")
        projection = project_frame_messages(messages, state)
        plan = build_stable_prefix(projection.messages)
        self.assertTrue(plan.breakpoints)
        marked = inject_cache_breakpoints(projection.messages, plan.breakpoints)
        self.assertEqual(len(marked), len(projection.messages))
        # 原始投影消息不被缓存标记污染
        first = projection.messages[0]
        for block in first["content"]:
            self.assertNotIn("cache_control", block)


class SessionSerializationTests(unittest.TestCase):
    """任务 1.2：记忆状态随会话/帧持久化与恢复。"""

    def test_memory_state_round_trip(self) -> None:
        frame = Frame(id="f1", agent=get_agent("coordinator", set()), messages=[{"role": "system", "content": "core"}])
        frame.context_memory.facts.append("事实X")
        frame.context_memory.tool_records.append(
            ToolMemoryRecord(
                record_id="tm1",
                tool_name="read_file",
                identity_key="read_file::a.gd",
                target="a.gd",
                markdown="### 工具结果：read_file",
                freshness="verified",
                verified=True,
                call_ids=["c1"],
            )
        )
        session = Session(session_id="s-rt", agent_stack=[frame])
        restored = session_from_dict(session_to_dict(session), set())
        state = restored.agent_stack[0].context_memory
        self.assertEqual(state.facts, ["事实X"])
        self.assertEqual(len(state.tool_records), 1)
        self.assertTrue(state.tool_records[0].verified)


class CompactionBudgetTests(unittest.TestCase):
    """任务 4.2：压缩按身份/新鲜度合并且保留边界对齐完整组。"""

    def test_compact_merges_removed_turns_into_memory_without_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp, threshold=1500, keep_recent=6, min_new=1)
            session = engine._store.get_or_create("s1", engine.available_tools)
            messages: list[dict[str, Any]] = [{"role": "system", "content": "core"}]
            for index in range(8):
                role = "user" if index % 2 == 0 else "assistant"
                messages.append({"role": role, "content": ("长文本-" * 200) + str(index)})
            messages.extend(_tool_group("c-old", "list_files", json.dumps({"pattern": "*", "files": ["x.gd"], "truncated": False})))
            messages.append({"role": "user", "content": "当前请求"})
            session.agent_stack = [Frame(id="f1", agent=get_agent("coordinator", set()), messages=messages)]
            result = asyncio.run(
                engine._compact_locked("s1", keep_recent=6, triggered_by="auto", use_llm=False)
            )
            frame = session.agent_stack[0]
            self.assertEqual(validate_projection(frame.messages), [])
            self.assertTrue(frame.context_memory.facts)
            self.assertIn("会话记忆", frame.compact_snapshot.summary if frame.compact_snapshot else "")
            self.assertGreaterEqual(result["compacted_frames"] + result["truncated_messages"], 0)


class TranscriptIsolationFlowTests(unittest.TestCase):
    """任务 6.2：Thought/转录专属条目不进入后续请求，展示稿保持完整。"""

    def test_thought_excluded_from_next_request_while_transcript_keeps_it(self) -> None:
        secret_reasoning = "SECRET_THOUGHT_never_in_context"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("extends Node\n", encoding="utf-8")
            llm = _RecordingLLM(
                [
                    {"reasoning": secret_reasoning, "content": "第一轮回答。"},
                    {"content": "第二轮回答。"},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    first = client.post(
                        "/chat", headers=HEADERS,
                        json={"session_id": "iso-1", "user_message": "第一轮", "client_message_id": "m-1"},
                    )
                    self.assertEqual(first.json().get("type"), "final", first.text)
                    second = client.post(
                        "/chat", headers=HEADERS,
                        json={"session_id": "iso-1", "user_message": "第二轮", "client_message_id": "m-2"},
                    )
                    self.assertEqual(second.json().get("type"), "final", second.text)

                    self.assertEqual(len(llm.requests), 2)
                    second_blob = json.dumps(llm.requests[1], ensure_ascii=False)
                    self.assertNotIn(secret_reasoning, second_blob)

                    history = client.get("/sessions/iso-1/history?limit=0", headers=HEADERS).json()["transcript"]
                    kinds = [entry["kind"] for entry in history["entries"]]
                    self.assertIn("thought", kinds)
                    thought = next(entry for entry in history["entries"] if entry["kind"] == "thought")
                    self.assertIn(secret_reasoning, json.dumps(thought, ensure_ascii=False))


class IntegrationProtocolFlowTests(unittest.TestCase):
    """任务 6.3：端到端协议与记忆行为。"""

    def test_current_role_tool_content_is_markdown_and_next_turn_has_memory_not_protocol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("extends Node\nfunc ready():\n    pass\n", encoding="utf-8")
            llm = _RecordingLLM(
                [
                    {"tool_calls": [{"id": "call-r1", "name": "read_file", "args": {"path": "player.gd"}}]},
                    {"content": "Finished."},
                    {"content": "Second turn done."},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    first = client.post(
                        "/chat", headers=HEADERS,
                        json={"session_id": "md-1", "user_message": "读文件", "client_message_id": "m-1"},
                    )
                    self.assertEqual(first.json().get("type"), "final", first.text)
                    # 第二次调用（最终回答前）携带的 role=tool 必须是 Markdown。
                    tool_messages = [m for m in llm.requests[1] if m.get("role") == "tool"]
                    self.assertTrue(tool_messages)
                    content = str(tool_messages[0].get("content"))
                    self.assertIn("### 工具结果", content)
                    with self.assertRaises(ValueError):
                        json.loads(content)
                    self.assertIn("player.gd", content)

                    second = client.post(
                        "/chat", headers=HEADERS,
                        json={"session_id": "md-1", "user_message": "下一轮", "client_message_id": "m-2"},
                    )
                    self.assertEqual(second.json().get("type"), "final", second.text)
                    third_request = llm.requests[-1]
                    self.assertEqual([m for m in third_request if m.get("role") == "tool"], [])
                    self.assertEqual(
                        [m for m in third_request if m.get("role") == "assistant" and m.get("tool_calls")], []
                    )
                    memory_blocks = [
                        m for m in third_request
                        if m.get("role") == "system"
                        and str(m.get("content", "")).startswith(CONVERSATION_MEMORY_BLOCK)
                    ]
                    self.assertTrue(memory_blocks)
                    self.assertIn("player.gd", str(memory_blocks[0]["content"]))

    def test_thirteenth_group_remains_in_tool_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.gd").write_text("extends Node\n", encoding="utf-8")
            tool_iterations = 5
            turns: list[dict[str, Any]] = [
                {"tool_calls": [{"id": f"call-{i}", "name": "list_files", "args": {"pattern": f"p{i}.gd"}}]}
                for i in range(tool_iterations)
            ]
            turns.append({"content": "All done."})
            llm = _RecordingLLM(turns)
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app = create_app(
                    AppSettings(
                        project_root=root,
                        rag_auto_build_enabled=False,
                        llm_base_url="http://localhost",
                        context_active_group_window=3,
                    ),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    response = client.post(
                        "/chat", headers=HEADERS,
                        json={"session_id": "win-1", "user_message": "列文件", "client_message_id": "m-1"},
                    )
                    self.assertEqual(response.json().get("type"), "final", response.text)

            # 最终请求之前的某次请求里，协议组不得超过窗口 3（第 4/5 组触发溢出收拢）。
            max_groups = 0
            for request in llm.requests:
                groups = [g for g in group_messages(request) if g.kind == "tool_group"]
                max_groups = max(max_groups, len(groups))
            self.assertLessEqual(max_groups, 3)

            session_files = list((root / ".ai_agent_service" / "sessions").glob("*.json"))
            self.assertTrue(session_files)
            persisted = json.loads(session_files[0].read_text(encoding="utf-8"))
            frame = persisted["agent_stack"][0]
            memory = frame["context_memory"]
            self.assertEqual(len(memory["tool_records"]), tool_iterations)
            tool_left = [m for m in frame["messages"] if m.get("role") == "tool"]
            self.assertEqual(tool_left, [])

    def test_discard_pending_produces_terminal_markdown_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine._store.get_or_create("s1", engine.available_tools)
            messages: list[dict[str, Any]] = [{"role": "system", "content": "core"}]
            messages.append({"role": "user", "content": "hi"})
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call-x", "type": "function", "function": {"name": "propose_script_edit", "arguments": "{}"}}
                ],
            })
            frame = Frame(id="f1", agent=get_agent("coordinator", set()), messages=messages)
            session.agent_stack = [frame]
            session.set_pending(
                "t1",
                ["call-x"],
                {"call-x": {"name": "propose_script_edit", "input": {}, "frame_id": "f1", "agent": "coordinator"}},
            )
            response = asyncio.run(engine.discard_pending("s1"))
            self.assertEqual(getattr(response, "type", ""), "final")
            last = frame.messages[-1]
            self.assertEqual(last["role"], "tool")
            self.assertTrue(is_terminal_content(last["content"]))
            self.assertIn("rejected", last["content"])
            self.assertEqual(validate_projection(frame.messages), [])
            # 终结结果已并入工具记忆
            self.assertTrue(frame.context_memory.current_turn_records or frame.context_memory.tool_records)

    def test_delegated_frame_memory_reaches_parent(self) -> None:
        from app.orchestrator.agent import _finish_frame

        parent = Frame(
            id="f1",
            agent=get_agent("coordinator", set()),
            messages=[
                {"role": "system", "content": "core"},
                {"role": "user", "content": "委派"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call-d", "type": "function", "function": {"name": "delegate", "arguments": "{}"}}
                    ],
                },
            ],
        )
        child = Frame(
            id="f2",
            agent=get_agent("coordinator", set()),
            messages=[{"role": "system", "content": "child"}, {"role": "user", "content": "任务"}],
            parent_id="f1",
            pending_delegate_call_id="call-d",
            depth=1,
        )
        child.context_memory.facts.append("子帧已验证事实")
        session = Session(session_id="s-del", agent_stack=[parent, child])
        result = asyncio.run(_finish_frame(session, "子任务完成", child, 1, None, None))
        self.assertIsNone(result)
        self.assertEqual(len(session.agent_stack), 1)
        delegation_message = parent.messages[-1]
        self.assertEqual(delegation_message["role"], "tool")
        content = str(delegation_message["content"])
        self.assertIn("子任务完成", content)
        self.assertIn("子帧已验证事实", content)
        self.assertIn(CONVERSATION_MEMORY_BLOCK, content)


class LongConversationAuditTests(unittest.TestCase):
    """任务 6.4：代表性长对话的 token/上下文审计断言。"""

    def test_long_conversation_stays_bounded_and_auditable(self) -> None:
        messages: list[dict[str, Any]] = [{"role": "system", "content": "core"}]
        for turn in range(20):
            messages.append({"role": "user", "content": f"长对话问题 {turn} " + "填充" * 200})
            messages.extend(
                _tool_group(
                    f"call-{turn}",
                    "read_file",
                    json.dumps(
                        {"path": f"file{turn}.gd", "content": "extends Node\n" * 50, "offset": 1,
                         "limit": 50, "lines_returned": 50, "total_lines_scanned": 50, "has_more": False}
                    ),
                )
            )
            messages.append({"role": "assistant", "content": f"长对话回答 {turn}"})
        original_tokens = estimate_message_tokens(messages)
        frame = Frame(id="f1", agent=get_agent("coordinator", set()), messages=messages)
        state = frame.context_memory
        state.current_turn_id = "t-long"
        sync_current_turn_memory(state, frame.messages)

        # 完整轮保留：20 轮收拢到 8 轮，工具组全部完成并在轮末清理。
        retain_recent_turns(frame.messages, state, retained_turns=8)
        complete_user_turn(frame.messages, state)

        settings = ContextProjectionSettings()
        projection = project_frame_messages(frame.messages, state, settings=settings)
        audit = projection.audit
        self.assertEqual(projection.violations, [])
        self.assertLessEqual(audit["retained_turns"], 9)  # 8 完整轮 + 当前请求轮上限
        self.assertLess(audit["estimated_tokens"], original_tokens)
        self.assertEqual(audit["pending_groups"], 0)
        self.assertTrue(audit["memory_injected"])
        self.assertGreaterEqual(audit["durable_tool_records"], 12)
        serialized_audit = json.dumps(audit, ensure_ascii=False)
        self.assertNotIn("填充", serialized_audit)


class SettingsDefaultsTests(unittest.TestCase):
    """任务 4.4：新增设置的默认值。"""

    def test_defaults(self) -> None:
        settings = AppSettings(llm_base_url="http://localhost")
        # 任务 7.7：默认投影预算与既有机械早清理阈值对齐。
        self.assertEqual(settings.context_budget_tokens, 200_000)
        self.assertEqual(settings.context_retained_turns, 8)
        self.assertEqual(settings.context_active_group_window, 12)
        self.assertTrue(settings.context_consolidation_use_llm)
        self.assertIsNone(settings.context_consolidation_model)


if __name__ == "__main__":
    unittest.main()
