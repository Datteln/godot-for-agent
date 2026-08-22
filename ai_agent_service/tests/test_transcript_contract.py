"""权威展示稿契约测试（任务 5.1 / 3.4 后端侧）。

覆盖：
- 实时补丁 → 历史快照等价（live-to-history equivalence）；
- 一个助手响应只产生一条助手条目（流式与完成共用身份，Thought 剥离）；
- 两条相同正文是两条不同条目；
- 工具/审批条目的原地更新与持久化；
- writer 层计划/进度/校验/错误迁移与状态合法性；
- 旧会话一次性兼容转换的稳定性（不重复推断、不泄露 Thought）；
- 快照游标与 WebSocket 序号空间一致、保留间隙、重连重放、重启后续号。
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
from app.llm.provider import AssistantTurn, LLMError, ToolCallRequest
from app.main import create_app
from app.config import AppSettings
from app.sessions.store import Session, SessionStore
from app.transcript.models import VALID_ENTRY_STATES
from app.transcript.writer import TranscriptWriter, visible_assistant_text

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
ANSWER_WITH_THOUGHT = "Thought: need to answer\nThe answer is 42."
VISIBLE_ANSWER = "The answer is 42."


class _ScriptedLLM:
    """按脚本逐轮返回预设响应；可选择先经 on_delta 流式发送累计正文。"""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)
        self.calls: list[list[dict[str, Any]]] = []
        self.chat_kwargs: list[dict[str, Any]] = []

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> AssistantTurn:
        self.calls.append(list(messages))
        self.chat_kwargs.append({"tools": list(tools), **kwargs})
        if not self._turns:
            return AssistantTurn(raw_message={"role": "assistant", "content": "done"}, content="done")
        spec = self._turns.pop(0)
        if spec.get("raise"):
            raise LLMError("simulated provider failure", status_code=500)
        on_delta = kwargs.get("on_delta")
        reasoning = spec.get("reasoning", "")
        if on_delta is not None and reasoning:
            partial = spec.get("reasoning_partial")
            if partial:
                on_delta("reasoning", partial, None)
            on_delta("reasoning", reasoning, spec.get("reasoning_tokens"))
            await asyncio.sleep(0.05)  # 让 Thought 完成耗时可测量
        content = spec.get("content", "")
        if on_delta is not None and content:
            partial = spec.get("partial")
            if partial:
                on_delta("content", partial, None)
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


def _build_client(root: Path, llm: Any) -> TestClient:
    """构造关闭后台索引、注入脚本 LLM 的测试客户端。"""
    app = create_app(
        AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
        token=TOKEN,
    )
    return TestClient(app)


def _chat(client: TestClient, session_id: str, user_message: str, client_message_id: str) -> dict[str, Any]:
    response = client.post(
        "/chat",
        headers=HEADERS,
        json={"session_id": session_id, "user_message": user_message, "client_message_id": client_message_id},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _history(client: TestClient, session_id: str) -> dict[str, Any]:
    response = client.get(f"/sessions/{session_id}/history", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("transcript") is not None, body
    return body


def _reduce_patches(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按客户端契约应用补丁序列：event_id 去重 + revision 比较。"""
    seen: set[str] = set()
    revisions: dict[str, int] = {}
    entries: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("type") != "transcript_patch":
            continue
        event_id = str(event.get("event_id", ""))
        if event_id in seen:
            continue
        seen.add(event_id)
        entry = event.get("payload", {}).get("entry", {})
        entry_id = str(entry.get("entry_id", ""))
        revision = int(entry.get("revision", 1))
        if revision <= revisions.get(entry_id, 0):
            continue
        revisions[entry_id] = revision
        entries[entry_id] = entry
    return entries


class LiveHistoryEquivalenceTests(unittest.TestCase):
    def test_streamed_answer_is_one_entry_equal_to_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM([{"content": ANSWER_WITH_THOUGHT, "partial": "Thought: need to answer\nThe ans"}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    body = _chat(client, "s1", "what is 6x7?", "m-1")
                    self.assertEqual(body.get("type"), "final")

                    hist = _history(client, "s1")
                    transcript = hist["transcript"]
                    entries = transcript["entries"]
                    self.assertFalse(transcript["legacy"])
                    self.assertEqual([entry["kind"] for entry in entries], ["user", "assistant"])
                    user_entry, assistant_entry = entries
                    self.assertEqual(user_entry["payload"].get("client_message_id"), "m-1")
                    self.assertEqual(assistant_entry["state"], "complete")
                    self.assertEqual(assistant_entry["payload"]["text"], VISIBLE_ANSWER)
                    self.assertNotIn("Thought", json.dumps(entries, ensure_ascii=False))

                    # 快照游标与 WebSocket 序号空间一致。
                    event_store = client.app.state.event_store
                    self.assertEqual(transcript["upto_event_seq"], event_store.last_seq("s1"))

                    # WS 重放全部事件并按客户端规则归约，结果与历史快照一致。
                    with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                        socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 0})
                        replayed: list[dict[str, Any]] = []
                        while True:
                            message = socket.receive_json()
                            if message.get("type") == "subscribed":
                                break
                            replayed.append(message["event"])
                    reduced = _reduce_patches(replayed)
                    self.assertEqual(set(reduced), {entry["entry_id"] for entry in entries})
                    for entry in entries:
                        live = reduced[entry["entry_id"]]
                        self.assertEqual(live["kind"], entry["kind"])
                        self.assertEqual(live["state"], entry["state"])
                        self.assertEqual(live["revision"], entry["revision"])
                        self.assertEqual(live["payload"], entry["payload"])

    def test_two_identical_answers_are_two_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM([{"content": "DONE"}, {"content": "DONE"}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    _chat(client, "s1", "say DONE", "m-1")
                    _chat(client, "s1", "say DONE again", "m-2")
                    hist = _history(client, "s1")
                    entries = hist["transcript"]["entries"]
                    assistants = [entry for entry in entries if entry["kind"] == "assistant"]
                    self.assertEqual(len(assistants), 2)
                    self.assertNotEqual(assistants[0]["entry_id"], assistants[1]["entry_id"])
                    self.assertEqual([entry["payload"]["text"] for entry in assistants], ["DONE", "DONE"])
                    self.assertEqual([entry["ordinal"] for entry in entries], list(range(len(entries))))


class ThoughtEntryTests(unittest.TestCase):
    def test_visible_thought_becomes_durable_completed_entry(self) -> None:
        """用户可见 Thought 应成为一个完成态条目：内容/计数/耗时齐全，先于助手条目。"""
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": "需要先确认重力参数，再给出建议。",
                        "reasoning_partial": "需要先确认重力参数",
                        "reasoning_tokens": 21,
                        "content": "建议把重力降到 900。",
                    }
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    body = _chat(client, "s1", "跳跃手感怎么调？", "m-1")
                    self.assertEqual(body.get("type"), "final")
                    hist = _history(client, "s1")
                    entries = hist["transcript"]["entries"]
                    self.assertEqual(
                        [entry["kind"] for entry in entries], ["user", "thought", "assistant"]
                    )
                    thought = entries[1]
                    self.assertEqual(thought["state"], "complete")
                    self.assertEqual(
                        thought["payload"]["content"], "需要先确认重力参数，再给出建议。"
                    )
                    self.assertEqual(thought["payload"]["token_count"], 21)
                    self.assertIsInstance(thought["payload"]["duration_seconds"], float)
                    self.assertGreater(thought["payload"]["duration_seconds"], 0.0)
                    self.assertIsNotNone(thought["payload"]["started_at"])
                    # Thought 与其后的助手条目是相互独立的身份。
                    self.assertNotEqual(thought["entry_id"], entries[2]["entry_id"])

    def test_thought_live_patches_equivalent_to_history(self) -> None:
        """Thought 的实时补丁（含 thinking 与 complete）重放后应与历史快照一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": "思考内容",
                        "reasoning_tokens": 7,
                        "content": "回答内容",
                    }
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    _chat(client, "s1", "hi", "m-1")
                    hist = _history(client, "s1")
                    entries = hist["transcript"]["entries"]
                    with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                        socket.send_json(
                            {"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 0}
                        )
                        replayed: list[dict[str, Any]] = []
                        while True:
                            message = socket.receive_json()
                            if message.get("type") == "subscribed":
                                break
                            replayed.append(message["event"])
                    reduced = _reduce_patches(replayed)
                    # 重放里必须出现同一 thought 条目的 thinking 与 complete 两个修订。
                    thought_patches = [
                        event
                        for event in replayed
                        if event.get("type") == "transcript_patch"
                        and event.get("payload", {}).get("entry", {}).get("kind") == "thought"
                    ]
                    thought_states = [
                        event["payload"]["entry"]["state"] for event in thought_patches
                    ]
                    self.assertIn("thinking", thought_states)
                    self.assertIn("complete", thought_states)
                    for entry in entries:
                        live = reduced.get(entry["entry_id"])
                        self.assertIsNotNone(live, entry["entry_id"])
                        self.assertEqual(live["state"], entry["state"])
                        self.assertEqual(live["revision"], entry["revision"])
                        self.assertEqual(live["payload"], entry["payload"])


class ThoughtRegressionTests(unittest.TestCase):
    """任务 1.5 / 5.6：迟到增量、思考预算边界与空正文挽救的回归验证。"""

    def test_late_reasoning_delta_does_not_regress_completed_thought(self) -> None:
        session = Session(session_id="s1")
        emitted: list[tuple[str, dict[str, Any]]] = []
        writer = TranscriptWriter(
            lambda sid, etype, payload: emitted.append((etype, payload)) or len(emitted)
        )
        writer.update_thought_stream(
            session, frame_id="f1", message_index=1, cumulative_text="先观察", token_count=3
        )
        writer.update_thought_stream(
            session, frame_id="f1", message_index=1, cumulative_text="先观察，再检查", token_count=7
        )
        writer.complete_thought(session, frame_id="f1", message_index=1)
        entry = session.transcript_entries[0]
        self.assertEqual(entry["state"], "complete")
        completed_revision = entry["revision"]
        completed_patch_count = len(emitted)

        # 迟到的更短增量：条目保持原样（状态、修订、补丁数都不变）。
        writer.update_thought_stream(
            session, frame_id="f1", message_index=1, cumulative_text="先观察", token_count=3
        )
        entry = session.transcript_entries[0]
        self.assertEqual(entry["state"], "complete")
        self.assertEqual(entry["revision"], completed_revision)
        self.assertEqual(len(emitted), completed_patch_count)

        # 迟到的更长增量：只保留更完整的推理内容，绝不退回 thinking。
        writer.update_thought_stream(
            session,
            frame_id="f1",
            message_index=1,
            cumulative_text="先观察，再检查，最后确认信号",
            token_count=12,
        )
        entry = session.transcript_entries[0]
        self.assertEqual(entry["state"], "complete")
        self.assertGreater(entry["revision"], completed_revision)
        self.assertEqual(entry["payload"]["content"], "先观察，再检查，最后确认信号")
        self.assertIsNotNone(entry["payload"]["duration_seconds"])

    def test_thinking_budget_boundary_followed_by_tool_calls_and_content(self) -> None:
        """思考预算边界后继续产出的工具调用/正文必须被正常接受，且累计推理不丢失。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("func jump():\n    pass\n", encoding="utf-8")
            long_reasoning = "长推理内容，超过常规预算边界。" * 8
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": long_reasoning,
                        "reasoning_tokens": 4096,
                        "content": "",
                        "tool_calls": [
                            {"id": "call-r1", "name": "read_file", "args": {"path": "player.gd"}}
                        ],
                    },
                    {"reasoning": "二轮思考", "content": "最终答案。"},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(root, llm) as client:
                    body = _chat(client, "s1", "读取并总结", "m-1")
                    self.assertEqual(body.get("type"), "final")
                    hist = _history(client, "s1")
                    entries = hist["transcript"]["entries"]
                    kinds = [entry["kind"] for entry in entries]
                    self.assertEqual(
                        kinds, ["user", "thought", "tool_activity", "thought", "assistant"]
                    )
                    first_thought = entries[1]
                    self.assertEqual(first_thought["state"], "complete")
                    self.assertEqual(first_thought["payload"]["content"], long_reasoning)
                    self.assertEqual(entries[2]["state"], "resolved")
                    self.assertEqual(entries[3]["state"], "complete")
                    self.assertEqual(entries[3]["payload"]["content"], "二轮思考")
                    self.assertEqual(entries[4]["payload"]["text"], "最终答案。")

    def test_empty_content_recovered_by_one_no_thinking_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM(
                [
                    {"reasoning": "推理耗尽后没有正文", "reasoning_tokens": 4096, "content": ""},
                    {"content": "挽救后的答复。"},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    body = _chat(client, "s1", "给我一个答复", "m-1")
                    self.assertEqual(body.get("type"), "final")
                    self.assertEqual(body.get("text"), "挽救后的答复。")
                    self.assertEqual(len(llm.chat_kwargs), 2)
                    recovery_kwargs = llm.chat_kwargs[1]
                    self.assertEqual(recovery_kwargs["tools"], [])
                    self.assertEqual(recovery_kwargs["thinking_budget"], 0)
                    hist = _history(client, "s1")
                    entries = hist["transcript"]["entries"]
                    kinds = [entry["kind"] for entry in entries]
                    self.assertEqual(kinds, ["user", "thought", "assistant"])
                    assistants = [entry for entry in entries if entry["kind"] == "assistant"]
                    self.assertEqual(len(assistants), 1)
                    self.assertEqual(assistants[0]["state"], "complete")
                    self.assertEqual(assistants[0]["payload"]["text"], "挽救后的答复。")
                    thought = entries[1]
                    self.assertEqual(thought["state"], "complete")
                    self.assertEqual(thought["payload"]["content"], "推理耗尽后没有正文")

    def test_empty_content_twice_yields_typed_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM([{"content": ""}, {"content": ""}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    body = _chat(client, "s1", "给我一个答复", "m-1")
                    self.assertEqual(body.get("type"), "error")
                    self.assertEqual(len(llm.chat_kwargs), 2)
                    hist = _history(client, "s1")
                    entries = hist["transcript"]["entries"]
                    kinds = [entry["kind"] for entry in entries]
                    self.assertEqual(kinds, ["user", "error"])
                    self.assertNotIn("assistant", kinds)
                    self.assertNotEqual(entries[1]["payload"].get("text", ""), "")


class ToolAndApprovalEntryTests(unittest.TestCase):
    def test_server_tool_and_approval_entries_persist_transitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("func jump():\n    pass\n", encoding="utf-8")
            llm = _ScriptedLLM(
                [
                    {
                        "content": "I will read and edit.",
                        "tool_calls": [
                            {"id": "call-r1", "name": "read_file", "args": {"path": "player.gd"}},
                            {
                                "id": "call-e1",
                                "name": "apply_text_edit",
                                "args": {"path": "player.gd", "old_string": "pass", "new_string": "return"},
                            },
                        ],
                    },
                    {"content": "Finished."},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(root, llm) as client:
                    body = _chat(client, "s1", "change jump", "m-1")
                    self.assertEqual(body.get("type"), "tool_calls")
                    front_calls = body.get("calls", [])
                    self.assertEqual(len(front_calls), 1)
                    self.assertTrue(front_calls[0]["needs_confirm"])
                    self.assertEqual(front_calls[0]["id"], "call-e1")

                    hist = _history(client, "s1")
                    by_kind: dict[str, list[dict[str, Any]]] = {}
                    for entry in hist["transcript"]["entries"]:
                        by_kind.setdefault(entry["kind"], []).append(entry)
                    tool_entries = by_kind.get("tool_activity", [])
                    approval_entries = by_kind.get("approval", [])
                    self.assertEqual(len(tool_entries), 1)
                    self.assertEqual(tool_entries[0]["state"], "resolved")
                    self.assertEqual(tool_entries[0]["tool_call_id"], "call-r1")
                    self.assertEqual(len(approval_entries), 1)
                    self.assertEqual(approval_entries[0]["state"], "pending")
                    self.assertEqual(approval_entries[0]["tool_call_id"], "call-e1")

                    result_response = client.post(
                        "/chat",
                        headers=HEADERS,
                        json={
                            "session_id": "s1",
                            "tool_results": [
                                {
                                    "tool_use_id": "call-e1",
                                    "frame_id": front_calls[0]["frame_id"],
                                    "turn_id": body["turn_id"],
                                    "status": "applied",
                                    "result": {"ok": True},
                                }
                            ],
                        },
                    )
                    self.assertEqual(result_response.status_code, 200, result_response.text)

                    final_hist = _history(client, "s1")
                    final_by_kind: dict[str, list[dict[str, Any]]] = {}
                    for entry in final_hist["transcript"]["entries"]:
                        final_by_kind.setdefault(entry["kind"], []).append(entry)
                    approvals = final_by_kind.get("approval", [])
                    self.assertEqual(approvals[0]["state"], "approved")
                    self.assertEqual(approvals[0]["revision"], 2)
                    self.assertEqual(approvals[0]["entry_id"], approval_entries[0]["entry_id"])


    def test_error_turn_records_error_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM([{"raise": True}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    response = client.post(
                        "/chat",
                        headers=HEADERS,
                        json={"session_id": "s1", "user_message": "hello", "client_message_id": "m-1"},
                    )
                    self.assertEqual(response.status_code, 200)
                    hist = _history(client, "s1")
                    kinds = [entry["kind"] for entry in hist["transcript"]["entries"]]
                    self.assertIn("error", kinds)
                    error_entry = next(
                        entry for entry in hist["transcript"]["entries"] if entry["kind"] == "error"
                    )
                    self.assertEqual(error_entry["state"], "complete")
                    self.assertNotEqual(error_entry["payload"].get("text", ""), "")


class WriterTransitionTests(unittest.TestCase):
    def test_writer_entries_follow_legal_state_machines(self) -> None:
        session = Session(session_id="s1")
        emitted: list[tuple[str, dict[str, Any]]] = []
        writer = TranscriptWriter(lambda sid, etype, payload: emitted.append((etype, payload)) or len(emitted))

        writer.record_user_message(session, "hello", client_message_id="m1", has_context=False)
        writer.record_plan_created(session, summary="plan", steps=[{"title": "step one"}, {"title": "step two"}])
        writer.record_plan_step(session, step_index=1, total_steps=2, title="step one", completed=False)
        writer.record_plan_step(session, step_index=1, total_steps=2, title=None, completed=True, summary="done")
        writer.start_verification(session, tool_use_id="c1", file_path="a.gd", phase="syntax")
        writer.finish_verification(session, tool_use_id="c1", phase="syntax", passed=True, issues_count=0, summary="ok")
        writer.record_error(session, "boom")

        for entry in session.transcript_entries:
            allowed = VALID_ENTRY_STATES[entry["kind"]]
            self.assertIn(entry["state"], allowed, entry)
        kinds = [entry["kind"] for entry in session.transcript_entries]
        self.assertEqual(kinds, ["user", "plan", "progress", "verification", "error"])
        progress = session.transcript_entries[2]
        self.assertEqual(progress["state"], "complete")
        self.assertEqual(progress["payload"]["title"], "step one")
        self.assertEqual(progress["payload"]["summary"], "done")
        verification = session.transcript_entries[3]
        self.assertEqual(verification["state"], "passed")
        self.assertTrue(all(etype == "transcript_patch" for etype, _ in emitted))

    def test_visible_text_strips_only_thought_prefix_line(self) -> None:
        self.assertEqual(visible_assistant_text("Thought: x\nbody"), "body")
        self.assertEqual(visible_assistant_text("no thought here"), "no thought here")
        self.assertEqual(visible_assistant_text("Thought: only"), "")


class LegacyConversionTests(unittest.TestCase):
    def test_legacy_session_converts_once_and_stably(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = AppSettings(project_root=root, rag_auto_build_enabled=False)
            store = SessionStore(settings.resolved_session_store_dir())
            session = store.get_or_create("legacy1", set())
            session.agent_stack = [
                Frame(
                    id="f1",
                    agent=get_agent("coordinator", set()),
                    messages=[
                        {"role": "system", "content": "sys"},
                        {"role": "user", "content": "old question"},
                        {"role": "assistant", "content": "Thought: legacy thinking\nold answer"},
                    ],
                )
            ]
            session.record_history_event("user_submitted", {"has_context": False})
            session.record_history_event(
                "agent_reasoning_delta",
                {
                    "frame_id": "f1",
                    "loop": 1,
                    "message_index": 2,
                    "text": "legacy reasoning content",
                    "elapsed_ms": 1500,
                    "token_count": 42,
                },
            )
            store.save(session)

            with _build_client(root, _ScriptedLLM([])) as client:
                first = _history(client, "legacy1")
                second = _history(client, "legacy1")
            self.assertTrue(first["transcript"]["legacy"])
            first_ids = [entry["entry_id"] for entry in first["transcript"]["entries"]]
            second_ids = [entry["entry_id"] for entry in second["transcript"]["entries"]]
            self.assertEqual(first_ids, second_ids)
            serialized = json.dumps(first["transcript"]["entries"], ensure_ascii=False)
            self.assertNotIn("legacy thinking", serialized)
            self.assertIn("old answer", serialized)
            # 历史推理增量应转换为完成态 Thought 条目，保留内容与尽力解析的计数/耗时。
            thoughts = [
                entry for entry in first["transcript"]["entries"] if entry["kind"] == "thought"
            ]
            self.assertEqual(len(thoughts), 1)
            self.assertEqual(thoughts[0]["state"], "complete")
            self.assertEqual(thoughts[0]["payload"]["content"], "legacy reasoning content")
            self.assertEqual(thoughts[0]["payload"]["token_count"], 42)
            self.assertAlmostEqual(thoughts[0]["payload"]["duration_seconds"], 1.5, places=2)


class CursorAndReplayTests(unittest.TestCase):
    def test_retention_gap_reports_and_snapshot_cursor_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM([{"content": ANSWER_WITH_THOUGHT}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    _chat(client, "s1", "hello", "m-1")
                    hist = _history(client, "s1")
                    cursor = hist["transcript"]["upto_event_seq"]

                    # 制造保留窗口缺口：压缩保留上限，并经由引擎发布更多事件
                    # （保证会话游标与传输序号同步推进，与生产路径一致）。
                    event_store = client.app.state.event_store
                    event_store._max_events_per_session = 2
                    for _ in range(4):
                        interrupt_response = client.post(
                            "/chat/interrupt", headers=HEADERS, json={"session_id": "s1"}
                        )
                        self.assertEqual(interrupt_response.status_code, 200)

                    with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                        socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 1})
                        gap = socket.receive_json()
                        self.assertEqual(gap["type"], "history_gap")
                        self.assertEqual(gap["session_id"], "s1")

                    # 以快照游标重新订阅不得再报缺口。
                    refreshed = _history(client, "s1")
                    resume_cursor = refreshed["transcript"]["upto_event_seq"]
                    self.assertGreaterEqual(resume_cursor, cursor)
                    with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                        socket.send_json(
                            {"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": resume_cursor}
                        )
                        message = socket.receive_json()
                        while message.get("type") == "event":
                            message = socket.receive_json()
                        self.assertEqual(message["type"], "subscribed")

    def test_event_ids_stable_across_reconnect_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            llm = _ScriptedLLM([{"content": ANSWER_WITH_THOUGHT}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(Path(tmp), llm) as client:
                    _chat(client, "s1", "hello", "m-1")
                    first_replay = self._replay_all(client)
                    second_replay = self._replay_all(client)
                    self.assertEqual(
                        [event["event_id"] for event in first_replay],
                        [event["event_id"] for event in second_replay],
                    )
                    self.assertTrue(any(event["type"] == "transcript_patch" for event in first_replay))

    @staticmethod
    def _replay_all(client: TestClient) -> list[dict[str, Any]]:
        with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
            socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 0})
            replayed: list[dict[str, Any]] = []
            while True:
                message = socket.receive_json()
                if message.get("type") == "subscribed":
                    return replayed
                replayed.append(message["event"])

    def test_cursor_survives_process_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = _ScriptedLLM([{"content": ANSWER_WITH_THOUGHT}, {"content": "Second answer."}])
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(root, llm) as client:
                    _chat(client, "s1", "hello", "m-1")
                    before = _history(client, "s1")["transcript"]["upto_event_seq"]
            # 模拟重启：新的进程/事件存储读取同一持久化目录。
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                with _build_client(root, llm) as client:
                    after = _history(client, "s1")["transcript"]["upto_event_seq"]
                    self.assertEqual(after, before)
                    _chat(client, "s1", "again", "m-2")
                    continued = _history(client, "s1")
                    self.assertGreater(continued["transcript"]["upto_event_seq"], before)
                    kinds = [entry["kind"] for entry in continued["transcript"]["entries"]]
                    self.assertEqual(kinds.count("assistant"), 2)


if __name__ == "__main__":
    unittest.main()
