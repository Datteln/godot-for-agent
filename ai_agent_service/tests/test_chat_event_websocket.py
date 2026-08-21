"""聊天事件 WebSocket 传输的回归测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import AppSettings
from app.events.store import EventStore, ResyncRequired
from app.main import create_app


class EventStoreTests(unittest.TestCase):
    """验证可恢复事件日志的身份、顺序、限速与保留范围。"""

    def test_event_identity_sequence_and_immutable_payload(self) -> None:
        """事件 ID、序号和载荷副本必须稳定且不可被调用方改写。"""
        store = EventStore()
        payload = {"turn_id": "t1", "text": "hello"}
        first = store.append("s1", "agent_text_delta", payload)
        payload["text"] = "changed"
        second = store.append("s1", "final", {"task_id": "task-1"})
        self.assertEqual(first.event_id, "s1:1")
        self.assertEqual(first.payload["text"], "hello")
        self.assertEqual(second.seq, 2)
        self.assertEqual(second.task_id, "task-1")

    def test_rate_limited_stream_flushes_before_boundary(self) -> None:
        """被限速的最新流式快照应在下一非流式边界前获得连续序号。"""
        store = EventStore()
        store.append("s1", "agent_text_delta", {"frame_id": "f", "loop": 1, "text": "a"})
        store.append("s1", "agent_text_delta", {"frame_id": "f", "loop": 1, "text": "ab"})
        store.append("s1", "final", {})
        events = store.list_after("s1")
        self.assertEqual([event.seq for event in events], [1, 2, 3])
        self.assertEqual(events[1].payload["text"], "ab")
        self.assertEqual(events[2].type, "final")

    def test_retention_and_overflow_are_explicit(self) -> None:
        """保留范围和慢订阅者均应给出可恢复的明确状态。"""
        store = EventStore(max_events_per_session=2, outbound_queue_size=1)
        _, subscription = store.subscribe("s1", 0)
        store.append("s1", "a", {})
        store.append("s1", "b", {})
        item = subscription.queue.get_nowait()
        self.assertIsInstance(item, ResyncRequired)
        store.unsubscribe(subscription)
        store.append("s1", "c", {})
        self.assertEqual(store.retained_range("s1"), (2, 3))


class WebSocketProtocolTests(unittest.TestCase):
    """验证认证、重放、实时投递、断线续传和缺口协议。"""

    def _client(self, root: Path) -> TestClient:
        """创建关闭后台索引的短生命周期协议测试客户端。"""
        app = create_app(
            AppSettings(
                project_root=root,
                rag_auto_build_enabled=False,
                event_heartbeat_interval_s=0.1,
            ),
            token="test-token",
        )
        return TestClient(app)

    @staticmethod
    def _subscribe(socket: object, after_seq: int = 0) -> None:
        """发送版本化订阅消息。"""
        socket.send_json({"version": 1, "type": "subscribe", "session_id": "s1", "after_seq": after_seq})

    def test_authorized_replay_live_resume_and_endpoint_removal(self) -> None:
        """授权客户端应获得稳定重放和实时事件，旧 HTTP 路由必须不存在。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                self.assertEqual(client.get("/chat/events", headers={"Authorization": "Bearer test-token"}).status_code, 404)
                store: EventStore = client.app.state.event_store
                first = store.append("s1", "agent_text_delta", {"text": "one"})
                with client.websocket_connect("/chat/events/ws", headers={"Authorization": "Bearer test-token"}) as socket:
                    self._subscribe(socket)
                    replay = socket.receive_json()
                    self.assertEqual(replay["event"]["event_id"], first.event_id)
                    self.assertEqual(socket.receive_json()["type"], "subscribed")
                    second = store.append("s1", "tool_progress", {"step": 1})
                    live = socket.receive_json()
                    self.assertEqual(live["event"]["seq"], second.seq)
                    socket.send_json({"version": 1, "type": "ack", "seq": second.seq})
                with client.websocket_connect("/chat/events/ws", headers={"Authorization": "Bearer test-token"}) as socket:
                    self._subscribe(socket, first.seq)
                    resumed = socket.receive_json()
                    self.assertEqual(resumed["event"]["event_id"], second.event_id)

    def test_unauthorized_and_retention_gap_are_rejected(self) -> None:
        """未授权访问与无法重放的游标不得泄露或静默跳过事件。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                with self.assertRaises(WebSocketDisconnect):
                    with client.websocket_connect("/chat/events/ws"):
                        pass
                store: EventStore = client.app.state.event_store
                store._max_events_per_session = 2
                for _ in range(4):
                    store.append("s1", "status", {})
                with client.websocket_connect("/chat/events/ws", headers={"Authorization": "Bearer test-token"}) as socket:
                    self._subscribe(socket, 1)
                    gap = socket.receive_json()
                    self.assertEqual(gap["type"], "history_gap")
                    self.assertEqual(gap["earliest_seq"], 3)

    def test_idle_subscription_receives_heartbeat(self) -> None:
        """空闲连接应收到只反映传输存活性的心跳。"""
        with tempfile.TemporaryDirectory() as tmp:
            with self._client(Path(tmp)) as client:
                with client.websocket_connect("/chat/events/ws", headers={"Authorization": "Bearer test-token"}) as socket:
                    self._subscribe(socket)
                    self.assertEqual(socket.receive_json()["type"], "subscribed")
                    self.assertEqual(socket.receive_json()["type"], "heartbeat")


if __name__ == "__main__":
    unittest.main()
