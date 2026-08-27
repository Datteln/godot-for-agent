"""fix-transcript-sync-recovery 服务端测试（任务 3.1 / 3.3 后端侧 / 4.3 / 4.8）。

覆盖：
- 可见进度水位只被用户可见条目的展示稿补丁推进（任务 1.2）；
- 订阅确认与心跳携带无正文的可见进度字段（任务 1.2）；
- 保留缺口/领先游标的类型化 history_gap 原因与背压 resync 原因（任务 1.3）；
- 历史快照原子包含全部可见条目类型且游标与事件序号一致（任务 1.4）；
- 丢失实时补丁后由重放或快照恢复完整可见转录（任务 3.3 服务端侧）；
- read_class_docs 事实仅存在于消费它的模型步骤，持久化帧/转录/历史/
  WebSocket 均不含完整 ClassDB/API 文本（任务 4.3）；
- legacy TileMap 作者请求的 ClassInfo 回归：Thought/审批按序可见（任务 4.8）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.config import AppSettings
from app.events.store import REASON_ITEM_BUDGET, EventStore, ResyncRequired
from app.llm.class_docs import EPHEMERAL_MARK, sanitize_class_docs_messages
from app.main import create_app
from app.security.settings import SecuritySettings
from app.tools.context import ToolContext
from app.tools.server_tools.grep_code import MATCH_EXCERPT_MAX_CHARS, grep_code_handler

TOKEN = "test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


def _full_patch(entry_id: str, ordinal: int, kind: str, state: str, revision: int) -> dict[str, Any]:
    """构造完整表示的展示稿补丁载荷。"""
    return {
        "patch_format": "full",
        "stream_key": entry_id,
        "entry": {
            "entry_id": entry_id,
            "ordinal": ordinal,
            "kind": kind,
            "state": state,
            "revision": revision,
            "turn_id": "t1",
            "tool_call_id": None,
            "payload": {},
        },
    }


def _delta_patch(entry_id: str, kind: str, revision: int, base_revision: int) -> dict[str, Any]:
    """构造追加增量表示的载荷（顶层携带 kind）。"""
    return {
        "patch_format": "append_delta",
        "stream_key": entry_id,
        "entry_id": entry_id,
        "kind": kind,
        "state": "thinking",
        "revision": revision,
        "base_revision": base_revision,
        "text_field": "content",
        "append_text": "...",
    }


class VisibleProgressWatermarkTests(unittest.TestCase):
    """可见进度水位的语义（任务 1.2）。"""

    def test_watermark_advances_only_for_visible_transcript_patches(self) -> None:
        """只有用户可见条目的展示稿补丁推进水位；system/log 与传输事件不参与。"""
        store = EventStore()
        seq, updated_at = store.visible_progress("s1")
        self.assertEqual((seq, updated_at), (0, 0.0))

        visible = store.append("s1", "transcript_patch", _full_patch("e1", 1, "thought", "thinking", 1))
        seq, updated_at = store.visible_progress("s1")
        self.assertEqual(seq, visible.seq)
        self.assertGreater(updated_at, 0.0)

        # 增量表示（顶层携带 kind）同样推进水位。
        # 等待超过流式发布限速间隔，确保增量立即获得独立序号。
        time.sleep(0.06)
        delta = store.append("s1", "transcript_patch", _delta_patch("e1", "thought", 2, 1))
        self.assertEqual(store.visible_progress("s1")[0], delta.seq)

        # system/log 属遗留内部条目，不算可见进度。
        store.append("s1", "transcript_patch", _full_patch("e2", 2, "system", "complete", 1))
        self.assertEqual(store.visible_progress("s1")[0], delta.seq)

        # 非展示稿传输事件不算可见进度。
        store.append("s1", "context_usage", {"used_tokens": 1})
        self.assertEqual(store.visible_progress("s1")[0], delta.seq)

    def test_seed_lifts_watermark_to_persisted_cursor(self) -> None:
        """重启后水位抬到持久化游标，避免水合客户端误判停滞。"""
        store = EventStore()
        store.seed("s1", 42)
        seq, updated_at = store.visible_progress("s1")
        self.assertEqual(seq, 42)
        self.assertGreater(updated_at, 0.0)
        # 仅抬不降。
        store.seed("s1", 10)
        self.assertEqual(store.visible_progress("s1")[0], 42)

    def test_overflow_resync_carries_typed_reason(self) -> None:
        """背压溢出产生带类型化原因的 ResyncRequired（任务 1.3）。"""
        store = EventStore(outbound_queue_size=1, coalescing_enabled=True)
        _, subscription = store.subscribe("s1", 0)
        store.append("s1", "tool_progress", {"step": 1})
        store.append("s1", "tool_progress", {"step": 2})
        store.append("s1", "tool_progress", {"step": 3})
        item = subscription.queue.get_nowait()
        self.assertIsInstance(item, ResyncRequired)
        self.assertEqual(item.session_id, "s1")
        self.assertEqual(item.reason, REASON_ITEM_BUDGET)
        diagnostics = subscription.diagnostics()
        self.assertIn("resync_count", diagnostics)
        self.assertIn("pending_items", diagnostics)


class SyncRecoveryProtocolTests(unittest.TestCase):
    """WebSocket 协议中的可见进度与类型化缺口信号。"""

    def _client(self, root: Path) -> TestClient:
        """创建关闭后台索引的短生命周期协议测试客户端。"""
        app = create_app(
            AppSettings(project_root=root, rag_auto_build_enabled=False, event_heartbeat_interval_s=0.1),
            token=TOKEN,
        )
        return TestClient(app)

    def test_subscribed_and_heartbeat_carry_visible_progress(self) -> None:
        """订阅确认与心跳携带无正文的可见进度字段（任务 1.2）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                store: EventStore = client.app.state.event_store
                visible = store.append("s1", "transcript_patch", _full_patch("e1", 1, "assistant", "complete", 1))
                store.append("s1", "transcript_patch", _full_patch("e2", 2, "system", "complete", 1))
                with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                    socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 0})
                    socket.receive_json()  # replay e1
                    socket.receive_json()  # replay e2
                    subscribed = socket.receive_json()
                    self.assertEqual(subscribed["type"], "subscribed")
                    self.assertEqual(subscribed["visible_seq"], visible.seq)
                    self.assertGreaterEqual(subscribed["visible_updated_at"], 0.0)
                    heartbeat = socket.receive_json()
                    self.assertEqual(heartbeat["type"], "heartbeat")
                    self.assertEqual(heartbeat["visible_seq"], visible.seq)
                    self.assertEqual(heartbeat["last_seq"], store.last_seq("s1"))
                    self.assertNotIn("payload", heartbeat)

    def test_history_gap_reasons_are_typed(self) -> None:
        """领先游标与保留缺口给出不同的类型化原因（任务 1.3）。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                store: EventStore = client.app.state.event_store
                store.append("s1", "status", {})
                store.append("s1", "status", {})
                with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                    socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 9})
                    gap = socket.receive_json()
                    self.assertEqual(gap["type"], "history_gap")
                    self.assertEqual(gap["reason"], "ahead_of_last")
                store._max_events_per_session = 1
                store.append("s1", "status", {})
                store.append("s1", "status", {})
                with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                    socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 1})
                    gap = socket.receive_json()
                    self.assertEqual(gap["type"], "history_gap")
                    self.assertEqual(gap["reason"], "retention_gap")


class _ScriptedLLM:
    """按脚本逐轮返回预设响应（与契约测试同款的最小实现）。"""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        self._turns = list(turns)

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        from app.llm.provider import AssistantTurn, ToolCallRequest

        spec = self._turns.pop(0) if self._turns else {"content": "done"}
        on_delta = kwargs.get("on_delta")
        reasoning = spec.get("reasoning", "")
        if on_delta is not None and reasoning:
            on_delta("reasoning", reasoning, None)
        content = spec.get("content", "")
        if on_delta is not None and content:
            on_delta("content", content, None)
        tool_calls = [
            ToolCallRequest(id=call["id"], name=call["name"], arguments=__import__("json").dumps(call["args"]))
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


class HistorySnapshotAtomicityTests(unittest.TestCase):
    """历史快照的原子性、完整类型覆盖与恢复游标（任务 1.4）。"""

    def _run_tool_turn(self, client: TestClient) -> dict[str, Any]:
        """跑一个带 Thought、服务端工具与前端审批的完整轮次。"""
        response = client.post(
            "/chat",
            headers=HEADERS,
            json={"session_id": "s1", "user_message": "edit jump", "client_message_id": "m-1"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body.get("type") == "tool_calls", body
        front_call = body["calls"][0]
        result = client.post(
            "/chat",
            headers=HEADERS,
            json={
                "session_id": "s1",
                "tool_results": [
                    {
                        "tool_use_id": front_call["id"],
                        "frame_id": front_call["frame_id"],
                        "turn_id": body["turn_id"],
                        "status": "applied",
                        "result": {"ok": True},
                    }
                ],
            },
        )
        assert result.status_code == 200, result.text
        return body

    def test_snapshot_contains_every_visible_kind_with_atomic_cursor(self) -> None:
        """快照必须包含所有已持久化可见条目类型，游标与事件序号一致。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("func jump():\n    pass\n", encoding="utf-8")
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": "need to inspect the file first",
                        "tool_calls": [
                            {"id": "call-r1", "name": "read_file", "args": {"path": "player.gd"}},
                            {
                                "id": "call-e1",
                                "name": "apply_text_edit",
                                "args": {"path": "player.gd", "old_string": "pass", "new_string": "return"},
                            },
                        ],
                    },
                    {"reasoning": "edit applied", "content": "Finished."},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    self._run_tool_turn(client)
                    response = client.get("/sessions/s1/history", headers=HEADERS)
                    self.assertEqual(response.status_code, 200)
                    body = response.json()
                    transcript = body["transcript"]
                    self.assertEqual(transcript["upto_event_seq"], body["last_event_seq"])
                    kinds = {entry["kind"] for entry in transcript["entries"]}
                    self.assertTrue(
                        {"user", "assistant", "thought", "tool_activity", "approval"} <= kinds,
                        kinds,
                    )
                    ordinals = [entry["ordinal"] for entry in transcript["entries"]]
                    self.assertEqual(ordinals, sorted(ordinals))

                    # limit=0 返回不裁剪的完整快照（恢复路径需求）。
                    full = client.get("/sessions/s1/history?limit=0", headers=HEADERS).json()["transcript"]
                    self.assertEqual(len(full["entries"]), len(transcript["entries"]))
                    self.assertFalse(full["has_more"])
                    # 分页裁剪不得改变原子游标。
                    paged = client.get("/sessions/s1/history?limit=1", headers=HEADERS).json()["transcript"]
                    self.assertEqual(len(paged["entries"]), 1)
                    self.assertTrue(paged["has_more"])
                    self.assertEqual(paged["upto_event_seq"], transcript["upto_event_seq"])

    def test_dropped_live_patches_recover_via_replay_then_snapshot(self) -> None:
        """丢失实时补丁后：重放可恢复完整转录；保留窗口之外由快照兜底（任务 3.3）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "player.gd").write_text("func jump():\n    pass\n", encoding="utf-8")
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": "inspect then edit",
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
                app = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    self._run_tool_turn(client)
                    store: EventStore = client.app.state.event_store

                    # 重放路径：从游标 0 重连应取回全部可见补丁。
                    replayed: list[dict[str, Any]] = []
                    with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                        socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 0})
                        while True:
                            message = socket.receive_json()
                            if message["type"] == "subscribed":
                                break
                            self.assertEqual(message["type"], "event")
                            replayed.append(message["event"])
                    replay_entries = _reduce_patches(replayed)
                    history = client.get("/sessions/s1/history?limit=0", headers=HEADERS).json()["transcript"]
                    self.assertEqual(
                        {entry["entry_id"] for entry in history["entries"]},
                        set(replay_entries.keys()),
                    )
                    for entry in history["entries"]:
                        self.assertEqual(
                            replay_entries[entry["entry_id"]]["kind"],
                            entry["kind"],
                        )

                    # 快照兜底：保留窗口被裁剪后重放报 retention_gap，快照仍完整。
                    store._max_events_per_session = 1
                    store.append("s1", "status", {})
                    store.append("s1", "status", {})
                    with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                        socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": 1})
                        gap = socket.receive_json()
                        self.assertEqual(gap["type"], "history_gap")
                        self.assertEqual(gap["reason"], "retention_gap")
                    fallback = client.get("/sessions/s1/history?limit=0", headers=HEADERS).json()["transcript"]
                    self.assertEqual(len(fallback["entries"]), len(history["entries"]))
                    self.assertGreaterEqual(fallback["upto_event_seq"], history["upto_event_seq"])


class ClassDocsEphemeralFactsTests(unittest.TestCase):
    """read_class_docs 事实短暂化与全链路脱敏（任务 4.3）。"""

    def test_sanitize_replaces_only_consumed_class_docs_results(self) -> None:
        """已被模型消费的 read_class_docs 结果替换为占位符，其余工具不受影响。"""
        docs_payload = {
            "status": "applied",
            "result": {
                "ok": True,
                "class_name": "TileMap",
                "mode": "members",
                "members": [{"name": "set_cell", "signature": "set_cell(coords, source_id)"}],
            },
        }
        other_payload = {"status": "applied", "result": {"ok": True, "text": "kept"}}
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": "core"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-docs",
                        "type": "function",
                        "function": {"name": "read_class_docs", "arguments": '{"class_name": "TileMap"}'},
                    },
                    {
                        "id": "call-other",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.gd"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call-docs", "content": json.dumps(docs_payload)},
            {"role": "tool", "tool_call_id": "call-other", "content": json.dumps(other_payload)},
        ]
        replaced = sanitize_class_docs_messages(messages)
        self.assertEqual(replaced, 1)
        docs_content = json.loads(str(messages[2]["content"]))
        self.assertTrue(docs_content[EPHEMERAL_MARK])
        self.assertEqual(docs_content["class_name"], "TileMap")
        self.assertEqual(docs_content["mode"], "members")
        self.assertNotIn("set_cell", str(messages[2]["content"]))
        self.assertEqual(json.loads(str(messages[3]["content"])), other_payload)
        # 幂等：再次调用不再替换。
        self.assertEqual(sanitize_class_docs_messages(messages), 0)

    def test_full_turn_keeps_no_classdb_text_in_frames_history_or_events(self) -> None:
        """一轮消费后：持久化帧、历史快照与事件流都只含受限元数据。"""
        marker = "FULL_CLASSDB_DOC_MARKER_set_cell_v2_signature"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": "check TileMap api first",
                        "tool_calls": [
                            {
                                "id": "call-docs",
                                "name": "read_class_docs",
                                "args": {"class_name": "TileMap", "mode": "members", "members": ["set_cell"]},
                            }
                        ],
                    },
                    {"content": "Done."},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    response = client.post(
                        "/chat",
                        headers=HEADERS,
                        json={"session_id": "docs-1", "user_message": "query TileMap api", "client_message_id": "m-1"},
                    )
                    body = response.json()
                    self.assertEqual(body.get("type"), "tool_calls", body)
                    result = client.post(
                        "/chat",
                        headers=HEADERS,
                        json={
                            "session_id": "docs-1",
                            "tool_results": [
                                {
                                    "tool_use_id": "call-docs",
                                    "frame_id": body["calls"][0]["frame_id"],
                                    "turn_id": body["turn_id"],
                                    "status": "applied",
                                    "result": {
                                        "ok": True,
                                        "class_name": "TileMap",
                                        "mode": "members",
                                        "members": [{"name": "set_cell", "signature": marker}],
                                    },
                                }
                            ],
                        },
                    )
                    self.assertEqual(result.json().get("type"), "final", result.text)

                    # 历史快照：可见工具活动只保留受限元数据，不含 API 正文。
                    history = client.get("/sessions/docs-1/history?limit=0", headers=HEADERS).json()["transcript"]
                    history_text = json.dumps(history, ensure_ascii=False)
                    self.assertNotIn(marker, history_text)
                    docs_activities = [
                        entry
                        for entry in history["entries"]
                        if entry["kind"] == "tool_activity"
                        and entry.get("payload", {}).get("tool") == "read_class_docs"
                    ]
                    self.assertTrue(docs_activities)
                    summary = docs_activities[0]["payload"].get("result_summary", {})
                    self.assertEqual(summary.get("class_name"), "TileMap")
                    self.assertEqual(summary.get("mode"), "members")
                    self.assertNotIn("members", summary)

                    # WebSocket 重放：事件流中没有完整 ClassDB 文本。
                    with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                        socket.send_json({"version": 1, "type": "subscribe", "session_id": "docs-1", "after_seq": 0})
                        while True:
                            message = socket.receive_json()
                            if message["type"] == "subscribed":
                                break
                            self.assertNotIn(marker, json.dumps(message, ensure_ascii=False))

                    # 持久化会话帧：结果以有界 Markdown 工具记忆保留（类名/
                    # 模式/被查询成员名），完整 ClassDB 文本不落盘，也不再使用
                    # 不透明的过期占位符（optimize-llm-conversation-context 任务 3.5）。
                    session_files = list((root / ".ai_agent_service" / "sessions").glob("*.json"))
                    self.assertTrue(session_files)
                    persisted = "\n".join(path.read_text(encoding="utf-8") for path in session_files)
                    self.assertNotIn(marker, persisted)
                    self.assertIn("TileMap", persisted)
                    self.assertIn("set_cell", persisted)
                    self.assertIn("tool_records", persisted)


class ClassInfoTileMapRegressionTests(unittest.TestCase):
    """legacy TileMap 作者请求的 ClassInfo 回归（任务 4.8）。"""

    def test_classinfo_then_approval_and_tool_activity_stay_ordered(self) -> None:
        """ClassInfo 之后的 Thought/审批/工具活动按序可见，ClassDB 文本不外泄。"""
        docs_marker = "CLASSDB_MEMBER_SIGNATURE_MARKER"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "maps" / "generator.gd").parent.mkdir(parents=True, exist_ok=True)
            (root / "maps" / "generator.gd").write_text("func build():\n    pass\n", encoding="utf-8")
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": "need TileMap api before bootstrap authoring",
                        "tool_calls": [
                            {
                                "id": "call-docs",
                                "name": "read_class_docs",
                                "args": {"class_name": "TileMap", "mode": "members", "members": ["set_cell", "clear_layer"]},
                            },
                            {
                                "id": "call-edit",
                                "name": "apply_text_edit",
                                "args": {
                                    "path": "maps/generator.gd",
                                    "old_string": "pass",
                                    "new_string": "return",
                                    "workflow": "code_driven_map",
                                },
                            },
                        ],
                    },
                    {"reasoning": "authoring applied", "content": "TileMap authoring finished."},
                ]
            )
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
                            "session_id": "map-reg",
                            "user_message": "用 legacy TileMap 建一张新地图",
                            "client_message_id": "m-map",
                        },
                    )
                    body = response.json()
                    self.assertEqual(body.get("type"), "tool_calls", body)
                    calls = {call["id"]: call for call in body["calls"]}
                    self.assertIn("call-docs", calls)
                    self.assertIn("call-edit", calls)
                    result = client.post(
                        "/chat",
                        headers=HEADERS,
                        json={
                            "session_id": "map-reg",
                            "tool_results": [
                                {
                                    "tool_use_id": "call-docs",
                                    "frame_id": calls["call-docs"]["frame_id"],
                                    "turn_id": body["turn_id"],
                                    "status": "applied",
                                    "result": {
                                        "ok": True,
                                        "class_name": "TileMap",
                                        "mode": "members",
                                        "members": [{"name": "set_cell", "signature": docs_marker}],
                                    },
                                },
                                {
                                    "tool_use_id": "call-edit",
                                    "frame_id": calls["call-edit"]["frame_id"],
                                    "turn_id": body["turn_id"],
                                    "status": "applied",
                                    "result": {"ok": True, "path": "maps/generator.gd"},
                                },
                            ],
                        },
                    )
                    self.assertEqual(result.json().get("type"), "final", result.text)

                    history = client.get("/sessions/map-reg/history?limit=0", headers=HEADERS).json()["transcript"]
                    entries = history["entries"]
                    ordinals = [entry["ordinal"] for entry in entries]
                    self.assertEqual(ordinals, sorted(ordinals))

                    def _ordinal(kind: str, tool: str | None = None, state: str | None = None) -> int:
                        """按类型/工具/状态定位条目的最小 ordinal。"""
                        matches = [
                            entry
                            for entry in entries
                            if entry["kind"] == kind
                            and (tool is None or entry.get("payload", {}).get("tool") == tool)
                            and (state is None or entry.get("state") == state)
                        ]
                        assert matches, f"missing entry kind={kind} tool={tool} state={state}"
                        return min(entry["ordinal"] for entry in matches)

                    user_ordinal = _ordinal("user")
                    thought_ordinal = _ordinal("thought")
                    classinfo_ordinal = _ordinal("tool_activity", tool="read_class_docs", state="resolved")
                    approval_ordinal = _ordinal("approval", tool="apply_text_edit", state="approved")
                    post_approval_ordinal = _ordinal("verification")
                    final_ordinal = _ordinal("assistant", state="complete")
                    self.assertLess(user_ordinal, thought_ordinal)
                    self.assertLessEqual(thought_ordinal, classinfo_ordinal)
                    self.assertLess(classinfo_ordinal, approval_ordinal)
                    self.assertLess(approval_ordinal, post_approval_ordinal)
                    self.assertLess(post_approval_ordinal, final_ordinal)

                    # ClassInfo 卡片仅保留受限元数据：无成员数、无 API 正文。
                    classinfo = next(
                        entry
                        for entry in entries
                        if entry["kind"] == "tool_activity"
                        and entry.get("payload", {}).get("tool") == "read_class_docs"
                    )
                    payload_text = json.dumps(classinfo["payload"], ensure_ascii=False)
                    self.assertNotIn(docs_marker, payload_text)
                    self.assertNotIn("members", classinfo["payload"].get("result_summary", {}))
                    self.assertNotIn(docs_marker, json.dumps(history, ensure_ascii=False))


class SearchBoundaryRegressionTests(unittest.TestCase):
    """grep_code 检索语料边界与规范化摘录回归（任务 5.5）。"""

    LOG_FILLER = "GIANT_LOG_FILLER_"

    def _fixture(self, root: Path) -> None:
        """构造含巨型运行日志与源码/配置命中项的工程夹具。

        日志行包含通用命中词 `NEEDLE`（若被扫描必然命中）与巨型填充；
        填充串是日志独有的泄露标识，不应出现在任何模型/转录/事件载荷中。
        """
        giant = "NEEDLE " + self.LOG_FILLER * 20_000
        (root / "logs").mkdir(parents=True, exist_ok=True)
        (root / "logs" / "service.log").write_text(giant + "\n", encoding="utf-8")
        (root / "scripts").mkdir(parents=True, exist_ok=True)
        (root / "scripts" / "player.gd").write_text('func _ready():\n\tvar token := "NEEDLE"\n', encoding="utf-8")
        (root / "project.godot").write_text("; NEEDLE config\n", encoding="utf-8")

    def test_broad_grep_excludes_logs_at_tool_boundary(self) -> None:
        """宽 include 也不得扫描运行日志；摘录在工具边界即受限。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            ctx = ToolContext(security=SecuritySettings(project_root=root), session_id="grep-unit")
            result = asyncio.run(grep_code_handler({"pattern": "NEEDLE", "include": "**/*"}, ctx))
            paths = {match["path"] for match in result["matches"]}
            self.assertNotIn("logs/service.log", paths)
            self.assertIn("scripts/player.gd", paths)
            self.assertIn("project.godot", paths)
            self.assertGreaterEqual(result["skipped_runtime_paths"], 1)
            self.assertGreaterEqual(result["scanned_files"], 2)
            for match in result["matches"]:
                self.assertIn("excerpt", match)
                self.assertIn("line_truncated", match)
                self.assertLessEqual(len(match["excerpt"]), MATCH_EXCERPT_MAX_CHARS)
            # 模型只收到规范化结果：巨型日志正文绝不出现。
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(self.LOG_FILLER, serialized)

    def test_broad_grep_turn_keeps_log_lines_out_of_model_transcript_and_events(self) -> None:
        """完整轮次后：巨型日志正文不进入模型帧、历史快照与事件保留区。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            llm = _ScriptedLLM(
                [
                    {
                        "reasoning": "search the needle",
                        "tool_calls": [
                            {
                                "id": "call-g1",
                                "name": "grep_code",
                                "args": {"pattern": "NEEDLE", "include": "**/*"},
                            }
                        ],
                    },
                    {"content": "Search completed."},
                ]
            )
            with patch("app.main.OpenAICompatibleProvider", lambda **kwargs: llm):
                app = create_app(
                    AppSettings(project_root=root, rag_auto_build_enabled=False, llm_base_url="http://localhost"),
                    token=TOKEN,
                )
                with TestClient(app) as client:
                    response = client.post(
                        "/chat",
                        headers=HEADERS,
                        json={"session_id": "grep-e2e", "user_message": "search needle", "client_message_id": "m-g"},
                    )
                    self.assertEqual(response.json().get("type"), "final", response.text)

                    # 历史快照：无日志正文；检索条目只带规范化摘录。
                    transcript = client.get("/sessions/grep-e2e/history?limit=0", headers=HEADERS).json()["transcript"]
                    transcript_text = json.dumps(transcript, ensure_ascii=False)
                    self.assertNotIn(self.LOG_FILLER, transcript_text)
                    self.assertNotIn("logs/service.log", transcript_text)
                    self.assertIn("scripts/player.gd", transcript_text)
                    grep_activities = [
                        entry
                        for entry in transcript["entries"]
                        if entry["kind"] == "tool_activity"
                        and entry.get("payload", {}).get("tool") == "grep_code"
                    ]
                    self.assertTrue(grep_activities)
                    summary = grep_activities[0]["payload"].get("result_summary", {})
                    self.assertEqual(summary.get("match_count"), 2)
                    for item in summary.get("matches", []):
                        self.assertLessEqual(len(str(item.get("excerpt", ""))), MATCH_EXCERPT_MAX_CHARS)
                        self.assertNotEqual(item.get("path"), "logs/service.log")

                    # 事件保留区：全部线上载荷不含日志正文。
                    store: EventStore = client.app.state.event_store
                    for event in store.list_after("grep-e2e", 0):
                        self.assertNotIn(self.LOG_FILLER, json.dumps(event.payload, ensure_ascii=False))

                    # 持久化模型帧：巨型日志正文不进入会话。
                    session_files = list((root / ".ai_agent_service" / "sessions").glob("*.json"))
                    self.assertTrue(session_files)
                    persisted = "\n".join(path.read_text(encoding="utf-8") for path in session_files)
                    self.assertNotIn(self.LOG_FILLER, persisted)


class TerminalPatchByteBudgetTests(unittest.TestCase):
    """终态展示稿补丁入站字节预算（任务 5.2 / 5.6 服务端侧）。"""

    def test_oversized_terminal_patch_replaced_with_safe_summary(self) -> None:
        """超预算终态补丁绝不入流：订阅者收到无正文安全摘要。"""
        store = EventStore(terminal_patch_max_bytes=2048)
        _, subscription = store.subscribe("s-term", 0)
        raw_marker = "z" * 4096
        oversized_entry = {
            "entry_id": "e-tool",
            "ordinal": 1,
            "kind": "tool_activity",
            "state": "resolved",
            "revision": 2,
            "turn_id": "t1",
            "tool_call_id": "c-term",
            "payload": {
                "tool": "grep_code",
                "is_error": False,
                "result_count": 1,
                "result_summary": {"matches": [{"path": "a.gd", "line": 1, "excerpt": raw_marker}]},
            },
        }
        store.append("s-term", "transcript_patch", {"entry": oversized_entry, "stream_key": "e-tool"})
        item = subscription.queue.get_nowait()
        self.assertEqual(item.type, "transcript_patch")
        wire = item.payload
        self.assertNotIn(raw_marker, json.dumps(wire, ensure_ascii=False))
        safe_entry = wire["entry"]
        self.assertEqual(safe_entry["entry_id"], "e-tool")
        self.assertEqual(safe_entry["state"], "resolved")
        self.assertTrue(safe_entry["payload"].get("oversized"))
        self.assertEqual(safe_entry["payload"].get("reason"), "terminal_patch_byte_budget")
        self.assertEqual(safe_entry["payload"].get("tool"), "grep_code")
        self.assertEqual(store.diagnostics()["terminal_patches_over_budget"], 1)

    def test_budget_leaves_streaming_and_compliant_terminal_patches_alone(self) -> None:
        """预算只约束非流式终态补丁：流式条目与合规终态补丁原样发布。"""
        store = EventStore(terminal_patch_max_bytes=2048)
        compliant = {
            "entry_id": "e-small",
            "ordinal": 1,
            "kind": "tool_activity",
            "state": "resolved",
            "revision": 1,
            "turn_id": "t1",
            "tool_call_id": "c-small",
            "payload": {"tool": "read_file"},
        }
        event = store.append("s-term", "transcript_patch", {"entry": compliant, "stream_key": "e-small"})
        self.assertEqual(event.payload["entry"]["payload"], {"tool": "read_file"})

        thought = {
            "entry_id": "e-thought",
            "ordinal": 2,
            "kind": "thought",
            "state": "complete",
            "revision": 1,
            "turn_id": "t1",
            "tool_call_id": None,
            "payload": {"content": "w" * 4096},
        }
        streamed = store.append("s-term", "transcript_patch", {"entry": thought, "stream_key": "e-thought"})
        self.assertEqual(len(streamed.payload["entry"]["payload"]["content"]), 4096)

    def test_websocket_delivery_of_oversized_terminal_patch_is_safe_summary(self) -> None:
        """经 WebSocket 投递时，超预算终态补丁只以安全摘要上线。"""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                    event_terminal_patch_max_bytes=4096,
                ),
                token=TOKEN,
            )
            with TestClient(app) as client:
                store: EventStore = client.app.state.event_store
                raw_marker = "q" * 8192
                entry = {
                    "entry_id": "e-ws",
                    "ordinal": 1,
                    "kind": "tool_activity",
                    "state": "failed",
                    "revision": 1,
                    "turn_id": "t1",
                    "tool_call_id": "c-ws",
                    "payload": {"tool": "grep_code", "is_error": True, "result_summary": {"blob": raw_marker}},
                }
                store.append("ws-budget", "transcript_patch", {"entry": entry, "stream_key": "e-ws"})
                with client.websocket_connect("/chat/events/ws", headers=HEADERS) as socket:
                    socket.send_json({"version": 1, "type": "subscribe", "session_id": "ws-budget", "after_seq": 0})
                    message = socket.receive_json()
                    self.assertEqual(message["type"], "event")
                    wire = message["event"]["payload"]
                    self.assertNotIn(raw_marker, json.dumps(wire, ensure_ascii=False))
                    self.assertTrue(wire["entry"]["payload"].get("oversized"))
                    subscribed = socket.receive_json()
                    self.assertEqual(subscribed["type"], "subscribed")


if __name__ == "__main__":
    unittest.main()