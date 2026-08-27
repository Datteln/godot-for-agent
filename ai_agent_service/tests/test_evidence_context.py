"""Focused tests for evidence-controlled retrieval and hybrid storage (task 8.7)."""

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
from app.context.evidence import (
    EvidenceSidecarStore,
    classify_source_kind,
    record_evidence,
    reduce_tool_record_detail,
    register_tool_evidence,
)
from app.context.grouping import group_messages
from app.context.memory import complete_user_turn, sync_current_turn_memory
from app.context.models import CONVERSATION_MEMORY_BLOCK, ContextMemoryState, ToolMemoryRecord
from app.context.projection import (
    ContextProjectionSettings,
    apply_hard_budget,
    project_frame_messages,
)
from app.context.tool_markdown import render_tool_result_markdown
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMProvider, ToolCallRequest
from app.main import create_app
from app.query.engine import QueryEngine
from app.security.settings import SecuritySettings
from app.sessions.store import Session, SessionStore
from app.tools.context import ToolContext
from app.tools.server_tools.read_file import read_file_handler

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class _RecordingLLM:
    """Scripted LLM that records every outgoing message list."""

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


class SidecarLifecycleTests(unittest.TestCase):
    """任务 8.2：易失证据 sidecar 的生命周期与去重。"""

    def test_volatile_evidence_writes_sidecar_and_dedupes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceSidecarStore(Path(tmp) / "sessions")
            state = ContextMemoryState()
            first = record_evidence(
                state,
                source_kind="runtime",
                tool_name="read_runtime_state",
                locator="read_runtime_state(node_path=...)",
                target="/root/Main",
                facts="- 运行时快照 A",
                body_markdown="## 运行时证据 A\n- position=(1,2)",
                sidecars=store,
                session_id="s-ev",
            )
            self.assertIsNotNone(first.sidecar_ref)
            sidecar_path = store.session_dir("s-ev") / first.sidecar_ref
            self.assertTrue(sidecar_path.exists())
            self.assertIn("运行时证据 A", sidecar_path.read_text(encoding="utf-8"))

            duplicate = record_evidence(
                state,
                source_kind="runtime",
                tool_name="read_runtime_state",
                locator="read_runtime_state(node_path=...)",
                target="/root/Main",
                facts="- 运行时快照 A",
                body_markdown="## 运行时证据 A\n- position=(1,2)",
                sidecars=store,
                session_id="s-ev",
            )
            self.assertEqual(duplicate.evidence_id, first.evidence_id)
            files = list(store.session_dir("s-ev").iterdir())
            self.assertEqual(len(files), 1)

    def test_reproducible_source_keeps_locator_and_fingerprint_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = EvidenceSidecarStore(Path(tmp) / "sessions")
            state = ContextMemoryState()
            record = record_evidence(
                state,
                source_kind="project_file",
                tool_name="read_file",
                locator="read_file(path='a.gd', offset=1, limit=...)",
                target="a.gd",
                facts="- 文件事实",
                body_markdown="完整文件正文……",
                fingerprint="fp-1",
                sidecars=store,
                session_id="s-ev",
            )
            self.assertIsNone(record.sidecar_ref)
            self.assertEqual(record.fingerprint, "fp-1")
            self.assertFalse(store.session_dir("s-ev").exists())

    def test_session_reset_removes_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "sessions"
            engine_store = SessionStore(storage)
            sidecars = EvidenceSidecarStore(storage)
            session = engine_store.get_or_create("s-reset", set())
            state = session.agent_stack[0].context_memory if session.agent_stack else None
            record_evidence(
                ContextMemoryState(),
                source_kind="command",
                tool_name="run_system_command",
                locator="run_system_command(...)",
                target="cmd",
                facts="- 输出事实",
                body_markdown="## 命令输出\nexit=0",
                sidecars=sidecars,
                session_id="s-reset",
            )
            self.assertTrue(any(sidecars.session_dir("s-reset").iterdir()))
            engine_store.reset("s-reset")
            self.assertFalse(sidecars.session_dir("s-reset").exists())


class EditorJsonExclusionTests(unittest.TestCase):
    """任务 8.3：模型上下文不收原始编辑器 JSON；清单含定位符。"""

    def test_editor_manifest_replaces_raw_json_in_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = _RecordingLLM([{"content": "OK."}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    response = client.post(
                        "/chat",
                        headers=HEADERS,
                        json={
                            "session_id": "ev-ed-1",
                            "user_message": "帮我看看场景",
                            "client_message_id": "m-1",
                            "context": {
                                "selection": {"node_path": "/root/Main/Player", "scene_path": "res://main.tscn"},
                                "scene_tree": {"root": "Main", "node_count": 42},
                                "debugger_errors": ["boom"],
                            },
                        },
                    )
                    self.assertEqual(response.json().get("type"), "final", response.text)

            user_messages = [
                message
                for message in llm.requests[0]
                if message.get("role") == "user"
            ]
            self.assertTrue(user_messages)
            content = str(user_messages[0].get("content"))
            self.assertIn("当前编辑器证据", content)
            self.assertIn("/root/Main/Player", content)
            self.assertIn("42", content)
            self.assertIn("describe_map_region", content)
            self.assertNotIn('"node_count"', content)
            self.assertNotIn('"scene_tree"', content)

            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app2 = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app2) as client:
                    history = client.get("/sessions/ev-ed-1/history?limit=0", headers=HEADERS).json()
                    kinds = [entry["kind"] for entry in history["transcript"]["entries"]]
                    self.assertIn("user", kinds)


class ReadFileProtectionTests(unittest.TestCase):
    """任务 8.4：生成/超长物理行以定位符提示返回。"""

    def test_long_generated_line_returns_locator_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            huge_line = "x" * 6000
            (root / "huge.min.js").write_text(f"var a=1;\n{huge_line}\nvar b=2;\n", encoding="utf-8")
            (root / "scene.tscn").write_text(
                '[gd_scene]\nnode_data = PackedIntArray(' + "1," * 3000 + "0)\n",
                encoding="utf-8",
            )
            security = SecuritySettings(project_root=root)
            ctx = ToolContext(security=security, session_id="t-rf")

            result = asyncio.run(read_file_handler({"path": "huge.min.js"}, ctx))
            self.assertGreaterEqual(result["protected_line_count"], 1)
            self.assertNotIn(huge_line, result["content"])
            self.assertIn("未展开", result["content"])
            self.assertIn("grep_code", result["content"])

            result_scene = asyncio.run(read_file_handler({"path": "scene.tscn"}, ctx))
            self.assertGreaterEqual(result_scene["protected_line_count"], 1)
            self.assertNotIn("1," * 100, result_scene["content"])

    def test_ordinary_text_is_not_affected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.gd").write_text("extends Node\nfunc _ready():\n    pass\n", encoding="utf-8")
            security = SecuritySettings(project_root=root)
            ctx = ToolContext(security=security, session_id="t-rf")
            result = asyncio.run(read_file_handler({"path": "a.gd"}, ctx))
            self.assertEqual(result["protected_line_count"], 0)
            self.assertIn("func _ready():", result["content"])


class MapEvidenceTests(unittest.TestCase):
    """任务 8.5/7.8：地图区域语义证据与选择回退。"""

    def test_map_region_renders_semantic_evidence(self) -> None:
        payload = {
            "ok": True,
            "target": "World/Map",
            "type": "TileMapLayer",
            "dimension": 2,
            "map_layer": None,
            "cells": [],
            "row_runs": [
                {"source_id": 2, "atlas_coords": {"x": 1, "y": 5}, "alternative_tile": 0, "x_start": 0, "x_end": 7, "y": 3},
                {"source_id": -1, "x_start": 8, "x_end": 9, "y": 3},
            ],
            "requested_cells": 100,
            "observed_cells": 100,
            "requested_bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
            "observed_bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
            "truncated": False,
            "node_position": {"x": 0, "y": 0},
            "tile_size": {"x": 16, "y": 16},
        }
        markdown = render_tool_result_markdown("describe_map_region", {"target_path": "World/Map"}, payload)
        self.assertIn("World/Map", markdown)
        self.assertIn("TileMapLayer", markdown)
        self.assertIn("请求范围", markdown)
        self.assertIn("观察范围", markdown)
        self.assertIn("row_runs", markdown)
        self.assertIn("source=2 atlas=(1,5)", markdown)
        self.assertIn("empty", markdown)
        self.assertIn("tile_size", markdown)
        with self.assertRaises(ValueError):
            json.loads(markdown)
        self.assertNotIn('"cells"', markdown)

    def test_selection_error_gives_target_path_fallback(self) -> None:
        markdown = render_tool_result_markdown(
            "describe_tilemap_selection", {}, {"ok": False, "message": "Select a TileMapLayer first"}
        )
        self.assertIn("describe_map_region", markdown)
        self.assertIn("target_path", markdown)
        self.assertIn("不要重复无参调用", markdown)

    def test_selection_success_points_to_region_tool(self) -> None:
        markdown = render_tool_result_markdown(
            "describe_tilemap_selection", {}, {"ok": True, "path": "Map/Ground", "type": "TileMapLayer"}
        )
        self.assertIn("Map/Ground", markdown)
        self.assertIn("describe_map_region", markdown)


class DuplicateFreeProjectionTests(unittest.TestCase):
    """任务 8.6：保留协议组的结果不经记忆块二次注入。"""

    def _frame_with_group(self) -> tuple[Frame, str]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-dup",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": '{"pattern": "*.gd"}'},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-dup",
                "content": render_tool_result_markdown(
                    "list_files", {"pattern": "*.gd"}, {"pattern": "*.gd", "files": ["a.gd"], "truncated": False}
                ),
            },
        ]
        frame = Frame(id="f1", agent=get_agent("coordinator", set()), messages=messages)
        return frame, "call-dup"

    def test_retained_group_not_duplicated_in_memory_block(self) -> None:
        frame, call_id = self._frame_with_group()
        state = frame.context_memory
        state.current_turn_id = "t1"
        sync_current_turn_memory(state, frame.messages, origin="server")
        self.assertTrue(state.current_turn_records)

        projection = project_frame_messages(frame.messages, state)
        memory_messages = [
            message
            for message in projection.messages
            if message.get("role") == "system"
            and str(message.get("content", "")).startswith(CONVERSATION_MEMORY_BLOCK)
        ]
        self.assertTrue(memory_messages)
        block = str(memory_messages[0]["content"])
        record = state.current_turn_records[0]
        self.assertNotIn(record.markdown, block)

    def test_turn_completion_moves_record_into_memory_block(self) -> None:
        frame, _ = self._frame_with_group()
        state = frame.context_memory
        state.current_turn_id = "t1"
        complete_user_turn(frame.messages, state)
        projection = project_frame_messages(frame.messages, state)
        block = str(projection.messages[1]["content"])
        self.assertIn("list_files", block)
        tool_messages = [message for message in projection.messages if message.get("role") == "tool"]
        self.assertEqual(tool_messages, [])


class HardBudgetGateTests(unittest.TestCase):
    """任务 7.1：含工具 schema 的出站硬性预算门。"""

    def test_tool_schemas_count_toward_budget(self) -> None:
        state = ContextMemoryState()
        big_facts = "事实行。" * 5000
        record = ToolMemoryRecord(
            record_id="tm1",
            tool_name="read_file",
            identity_key="read_file::big.gd",
            target="big.gd",
            markdown="### 工具结果：read_file\n\n" + big_facts,
            call_ids=["c1"],
        )
        state.tool_records.append(record)
        record_evidence(
            state,
            source_kind="project_file",
            tool_name="read_file",
            locator="read_file(path='big.gd')",
            target="big.gd",
            facts="- 事实卡",
            fingerprint="fp",
            call_id="c1",
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "core"},
            {"role": "system", "content": CONVERSATION_MEMORY_BLOCK + "\n" + record.markdown},
            {"role": "user", "content": "问题 " + "长" * 200},
        ]
        big_tools = [
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": "工具说明。" * 300,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for i in range(6)
        ]
        messages_only_tokens = apply_hard_budget(
            list(messages), [], state, budget_tokens=10**9, memory_index=1
        ).estimated_tokens
        with_tools = apply_hard_budget(
            list(messages), big_tools, state, budget_tokens=10**9, memory_index=1
        ).estimated_tokens
        self.assertGreater(with_tools, messages_only_tokens)

        budget = messages_only_tokens + (with_tools - messages_only_tokens) // 2
        result = apply_hard_budget(
            list(messages), big_tools, state, budget_tokens=budget, memory_index=1
        )
        self.assertTrue(result.passed, result)
        self.assertTrue(any(action.startswith("reduced_locator_detail") for action in result.actions))
        self.assertLessEqual(result.estimated_tokens, budget)

    def test_gate_fails_safely_when_nothing_can_be_reduced(self) -> None:
        state = ContextMemoryState()
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "巨大的用户消息 " + "长" * 5000},
        ]
        result = apply_hard_budget(
            list(messages), [], state, budget_tokens=50, memory_index=None
        )
        self.assertFalse(result.passed)


class EngineEvidenceIntegrationTests(unittest.TestCase):
    """任务 8.2：服务流程中的证据登记与 sidecar。"""

    def test_server_tool_evidence_registered_with_sidecar_for_volatile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = QueryEngine(
                settings=AppSettings(llm_base_url="http://localhost", project_root=Path(tmp)),
                session_store=SessionStore(Path(tmp) / "sessions"),
                llm=_RecordingLLM([{"content": "ok"}]),
                event_store=EventStore(),
            )
            session = engine._store.get_or_create("s-int", engine.available_tools)
            session.ensure_root_frame(get_agent("coordinator", engine.available_tools))
            frame = session.agent_stack[0]
            frame.messages = [{"role": "system", "content": "core"}, {"role": "user", "content": "hi"}]
            frame.messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-cmd",
                            "type": "function",
                            "function": {"name": "run_system_command", "arguments": "{}"},
                        }
                    ],
                }
            )
            command_markdown = render_tool_result_markdown(
                "run_system_command", {"command": "ls"}, {"exit_code": 0, "output": "a.gd"}
            )
            frame.messages.append(
                {"role": "tool", "tool_call_id": "call-cmd", "content": command_markdown}
            )
            sync_current_turn_memory(frame.context_memory, frame.messages, origin="server")
            register_tool_evidence(
                frame.context_memory,
                tool_name="run_system_command",
                input_args={"command": "ls"},
                payload={"exit_code": 0, "output": "a.gd"},
                markdown=command_markdown,
                call_id="call-cmd",
                sidecars=engine._evidence_sidecars,
                session_id="s-int",
            )
            evidence = list(frame.context_memory.evidence_index.values())
            self.assertEqual(len(evidence), 1)
            self.assertEqual(evidence[0].source_kind, "command")
            self.assertIsNotNone(evidence[0].sidecar_ref)
            sidecar_file = (
                engine._evidence_sidecars.session_dir("s-int") / evidence[0].sidecar_ref
            )
            self.assertTrue(sidecar_file.exists())
            self.assertIn("exit", sidecar_file.read_text(encoding="utf-8"))


class SourceKindClassificationTests(unittest.TestCase):
    """任务 8.1/8.2：来源类别分类。"""

    def test_classification(self) -> None:
        self.assertEqual(classify_source_kind("read_file"), "project_file")
        self.assertEqual(classify_source_kind("grep_code"), "search")
        self.assertEqual(classify_source_kind("read_class_docs"), "class_docs")
        self.assertEqual(classify_source_kind("read_runtime_state"), "runtime")
        self.assertEqual(classify_source_kind("read_debugger_errors"), "diagnostic")
        self.assertEqual(classify_source_kind("run_system_command"), "command")
        self.assertEqual(classify_source_kind("describe_map_region"), "map")
        self.assertEqual(classify_source_kind("add_node"), "editor")
        self.assertIsNone(classify_source_kind("delegate"))


if __name__ == "__main__":
    unittest.main()
