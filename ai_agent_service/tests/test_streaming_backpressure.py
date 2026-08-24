"""流式展示稿背压的回归测试（streaming-transcript-backpressure）。

覆盖：
- 有界实时表示：完整/追加增量/受限预览的版本化选择与回退（任务 1.3）。
- 长流确定性夹具：累计 Thought/assistant 增长不记录真实模型文本（任务 1.2）。
- 订阅合并与预算：条目键 latest-wins、字节/条数预算、终态顺序（任务 2.2/2.4）。
- 重放/续订：保留日志可重建完整正文（任务 2.4）。
- 脱敏诊断：只含数值，不含正文（任务 1.1）。
- `turn_keepalive`：活跃轮次的独立进展信号（任务 4.1）。
- 长流压测：载荷/队列指标有界且最终与权威展示稿一致（任务 6.1 服务端侧）。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from app.config import AppSettings
from app.events.store import EventStore, ResyncRequired
from app.llm.provider import AssistantTurn, LLMProvider
from app.query.engine import QueryEngine
from app.sessions.store import SessionStore
from app.transcript.models import (
    PATCH_FORMAT_APPEND_DELTA,
    PATCH_FORMAT_FULL,
    PATCH_FORMAT_PREVIEW,
)
from app.transcript.realtime import LastPublishedStream, build_realtime_patch

_FIXTURE_SENTINEL = "fixture-line"


def make_long_stream_fixture(steps: int = 240, line_width: int = 48) -> list[str]:
    """生成确定性的累计正文序列（任务 1.2）。

    每一步返回“到该步为止的累计文本”，内容完全由序号推导，不包含任何
    真实模型输出，日志与断言中也只出现计数/字节等脱敏数值。

    Args:
        steps: 累计更新次数。
        line_width: 每步追加行的近似宽度。

    Returns:
        长度等于 ``steps`` 的累计文本列表。
    """
    cumulative: list[str] = []
    text = ""
    for index in range(steps):
        text += f"{_FIXTURE_SENTINEL}-{index:04d}:" + "x" * line_width + "\n"
        cumulative.append(text)
    return cumulative


def _assistant_entry(revision: int, text: str, state: str = "streaming") -> dict[str, Any]:
    """构造一个助手条目的完整权威形态。"""
    return {
        "entry_id": "e1",
        "ordinal": 0,
        "kind": "assistant",
        "state": state,
        "revision": revision,
        "turn_id": "t1",
        "tool_call_id": None,
        "payload": {"text": text},
    }


def _patch(entry: dict[str, Any]) -> dict[str, Any]:
    """构造写入端发布的完整 `transcript_patch` 载荷。"""
    return {"entry": entry, "stream_key": str(entry["entry_id"])}


def _publish_no_ratelimit(store: EventStore, session_id: str, payload: dict[str, Any]) -> Any:
    """绕过 50ms 流式限速直接发布一条展示稿补丁。"""
    with mock.patch("app.events.store._STREAM_PUBLICATION_INTERVAL_S", 0.0):
        return store.append(session_id, "transcript_patch", payload)


class RealtimeRepresentationTests(unittest.TestCase):
    """验证有界实时表示的选择、校验与回退（任务 1.3）。"""

    def test_first_terminal_and_structural_patches_stay_full(self) -> None:
        """首次可见、终态与非流式条目必须使用完整补丁。"""
        store = EventStore()
        first = _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(1, "hello")))
        self.assertEqual(first.payload.get("patch_format"), PATCH_FORMAT_FULL)
        self.assertIn("entry", first.payload)
        terminal = _publish_no_ratelimit(
            store, "s1", _patch(_assistant_entry(2, "hello world", state="complete"))
        )
        self.assertEqual(terminal.payload.get("patch_format"), PATCH_FORMAT_FULL)

    def test_growing_text_uses_append_delta_with_base_revision(self) -> None:
        """增长中的正文发布为携带 base_revision 的追加增量。"""
        store = EventStore()
        _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(1, "hello")))
        delta_event = _publish_no_ratelimit(
            store, "s1", _patch(_assistant_entry(2, "hello world"))
        )
        payload = delta_event.payload
        self.assertEqual(payload.get("patch_format"), PATCH_FORMAT_APPEND_DELTA)
        self.assertEqual(payload.get("base_revision"), 1)
        self.assertEqual(payload.get("revision"), 2)
        self.assertEqual(payload.get("append_text"), " world")
        self.assertEqual(payload.get("text_field"), "text")
        self.assertNotIn("entry", payload)

    def test_non_append_growth_falls_back_to_bounded_preview_then_full(self) -> None:
        """不可追加的增长先用受限预览，其后必须回退完整补丁。"""
        store = EventStore(stream_preview_max_chars=64)
        _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(1, "Thou")))
        long_text = "visible body " + "y" * 400
        preview_event = _publish_no_ratelimit(
            store, "s1", _patch(_assistant_entry(2, long_text))
        )
        self.assertEqual(preview_event.payload.get("patch_format"), PATCH_FORMAT_PREVIEW)
        self.assertEqual(preview_event.payload.get("total_chars"), len(long_text))
        self.assertEqual(preview_event.payload.get("preview_text"), long_text[-64:])
        recovery = _publish_no_ratelimit(
            store, "s1", _patch(_assistant_entry(3, long_text + " more"))
        )
        self.assertEqual(recovery.payload.get("patch_format"), PATCH_FORMAT_FULL)

    def test_build_realtime_patch_rejects_delta_after_preview(self) -> None:
        """纯函数层面确认预览之后的下一发布不再是增量。"""
        preview_state = LastPublishedStream(
            revision=2, text="abc", representation=PATCH_FORMAT_PREVIEW, composable=False
        )
        payload, new_state = build_realtime_patch(
            _patch(_assistant_entry(3, "abcdef")), preview_state, preview_max_chars=2
        )
        self.assertEqual(payload.get("patch_format"), PATCH_FORMAT_FULL)
        self.assertTrue(new_state.composable)


class LongStreamReplayTests(unittest.TestCase):
    """长流保留日志必须可重建完整正文且总载荷有界（任务 1.2 / 2.4 / 6.1）。"""

    @staticmethod
    def _reconstruct(events: list[Any]) -> str:
        """按客户端规则从重放事件序列重建条目正文。"""
        text = ""
        accepted_revision = 0
        for event in events:
            payload = event.payload
            patch_format = str(payload.get("patch_format", ""))
            if patch_format == PATCH_FORMAT_FULL:
                entry = payload.get("entry", {})
                text = str(entry.get("payload", {}).get("text", ""))
                accepted_revision = int(entry.get("revision", 1))
            elif patch_format == PATCH_FORMAT_APPEND_DELTA:
                assert int(payload.get("base_revision", -1)) == accepted_revision
                text += str(payload.get("append_text", ""))
                accepted_revision = int(payload.get("revision", accepted_revision))
            else:
                raise AssertionError(f"unexpected patch_format in replay: {patch_format}")
        return text

    def test_cumulative_growth_stays_bounded_and_reconstructable(self) -> None:
        """累计正文重放等价于权威完整文本，且实时字节数远小于累计字节数。"""
        fixture = make_long_stream_fixture()
        store = EventStore()
        for revision, text in enumerate(fixture, start=1):
            state = "complete" if revision == len(fixture) else "streaming"
            _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(revision, text, state)))
        events = [
            event for event in store.list_after("s1") if event.type == "transcript_patch"
        ]
        self.assertEqual(self._reconstruct(events), fixture[-1])
        realtime_bytes = sum(
            len(json.dumps(event.payload, ensure_ascii=False).encode("utf-8"))
            for event in events
        )
        cumulative_bytes = sum(len(text.encode("utf-8")) for text in fixture)
        # 完整累计传输是 O(文本长度 × 更新次数)；有界表示必须显著更小。
        self.assertLess(realtime_bytes, cumulative_bytes // 5)
        diagnostics = store.diagnostics()
        self.assertGreater(diagnostics["stream_events_published"], 0)
        self.assertGreater(diagnostics["stream_payload_bytes"], 0)


class SubscriberBackpressureTests(unittest.TestCase):
    """订阅级合并、预算与终态顺序（任务 2.2 / 2.3 / 2.4）。"""

    def test_slow_subscriber_receives_coalesced_stream_then_terminal(self) -> None:
        """慢订阅者收到合并后的最新流式状态，终态仍按顺序送达。"""
        fixture = make_long_stream_fixture(steps=120)
        store = EventStore()
        _, subscription = store.subscribe("s1", 0)
        for revision, text in enumerate(fixture, start=1):
            state = "complete" if revision == len(fixture) else "streaming"
            _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(revision, text, state)))
        delivered: list[Any] = []
        while not subscription.queue.empty():
            delivered.append(subscription.queue.get_nowait())
        patches = [item for item in delivered if not isinstance(item, ResyncRequired)]
        # 完整首发 + 至多一个合并增量 + 完整终态，而不是逐条累计快照。
        self.assertLessEqual(len(patches), 3)
        self.assertEqual(patches[0].payload.get("patch_format"), PATCH_FORMAT_FULL)
        terminal = patches[-1].payload
        self.assertEqual(terminal.get("patch_format"), PATCH_FORMAT_FULL)
        self.assertEqual(terminal.get("entry", {}).get("state"), "complete")
        self.assertEqual(terminal.get("entry", {}).get("payload", {}).get("text"), fixture[-1])
        if len(patches) == 3:
            merged = patches[1].payload
            self.assertEqual(merged.get("patch_format"), PATCH_FORMAT_APPEND_DELTA)
            self.assertEqual(merged.get("base_revision"), 1)
        stats = subscription.diagnostics()
        self.assertGreater(stats["coalesced_patches"], 0)
        self.assertNotIn(_FIXTURE_SENTINEL, json.dumps(stats, ensure_ascii=False))

    def test_item_budget_overflow_issues_typed_resync(self) -> None:
        """条数预算耗尽时发出带类型原因的重同步，且不静默丢序。"""
        store = EventStore(outbound_queue_size=4)
        _, subscription = store.subscribe("s1", 0)
        for index in range(10):
            entry = _assistant_entry(1, f"t{index}", state="complete")
            entry["entry_id"] = f"e{index}"
            store.append("s1", "transcript_patch", _patch(entry))
        items: list[Any] = []
        while not subscription.queue.empty():
            items.append(subscription.queue.get_nowait())
        self.assertIsInstance(items[-1], ResyncRequired)
        self.assertEqual(items[-1].reason, "outbound_item_budget")
        self.assertGreaterEqual(subscription.diagnostics()["resync_count"], 1)

    def test_byte_budget_overflow_issues_typed_resync(self) -> None:
        """字节预算耗尽时同样转入显式重同步并保留脱敏诊断。"""
        store = EventStore(outbound_queue_size=1024, max_outbound_bytes=2048)
        _, subscription = store.subscribe("s1", 0)
        for index in range(16):
            entry = _assistant_entry(1, "z" * 512, state="complete")
            entry["entry_id"] = f"e{index}"
            store.append("s1", "transcript_patch", _patch(entry))
        items: list[Any] = []
        while not subscription.queue.empty():
            items.append(subscription.queue.get_nowait())
        self.assertIsInstance(items[-1], ResyncRequired)
        self.assertEqual(items[-1].reason, "outbound_byte_budget")
        diagnostics = subscription.diagnostics()
        self.assertGreater(diagnostics["peak_bytes"], 2048)

    def test_legacy_mode_keeps_count_only_overflow_reason(self) -> None:
        """关闭合并特性时回退纯条数队列与既有溢出原因。"""
        store = EventStore(outbound_queue_size=1, coalescing_enabled=False)
        _, subscription = store.subscribe("s1", 0)
        store.append("s1", "a", {})
        store.append("s1", "b", {})
        item = subscription.queue.get_nowait()
        self.assertIsInstance(item, ResyncRequired)
        self.assertEqual(item.reason, "outbound_queue_overflow")

    def test_resync_does_not_starve_other_subscribers(self) -> None:
        """一个订阅进入重同步不得阻塞同会话其他订阅的投递。"""
        store = EventStore(outbound_queue_size=1)
        _, slow = store.subscribe("s1", 0)
        _, fast = store.subscribe("s1", 0)
        fast_received: list[Any] = []
        # fast 每次发布后立即清空（从不越预算），slow 从不消费（会越预算）。
        for event_type in ("a", "b", "c"):
            store.append("s1", event_type, {})
            while not fast.queue.empty():
                item = fast.queue.get_nowait()
                if not isinstance(item, ResyncRequired):
                    fast_received.append(item)
        self.assertIsInstance(slow.queue.get_nowait(), ResyncRequired)
        self.assertEqual([event.seq for event in fast_received], [1, 2, 3])


class RedactedDiagnosticsTests(unittest.TestCase):
    """诊断输出必须可操作且不含正文（任务 1.1）。"""

    def test_diagnostics_are_numeric_and_redacted(self) -> None:
        """订阅与存储诊断只包含计数/字节/序号等脱敏字段。"""
        fixture = make_long_stream_fixture(steps=60)
        store = EventStore()
        _, subscription = store.subscribe("s1", 0)
        for revision, text in enumerate(fixture, start=1):
            _publish_no_ratelimit(
                store, "s1", _patch(_assistant_entry(revision, text, "streaming"))
            )
        blob = json.dumps(
            {"subscription": subscription.diagnostics(), "store": store.diagnostics()},
            ensure_ascii=False,
        )
        self.assertNotIn(_FIXTURE_SENTINEL, blob)
        self.assertNotIn("xxxx", blob)


class LongStreamStressTests(unittest.TestCase):
    """长流压测（任务 6.1 服务端侧）：队列指标有界且最终与权威展示稿一致。"""

    def test_draining_subscriber_keeps_bounded_queue_and_reaches_equivalence(self) -> None:
        """边消费边投递的长流下，待发深度有界、无重同步，且正文与快照一致。"""
        fixture = make_long_stream_fixture(steps=400)
        store = EventStore(max_outbound_bytes=64 * 1024)
        _, subscription = store.subscribe("s1", 0)
        received: list[Any] = []
        for revision, text in enumerate(fixture, start=1):
            state = "complete" if revision == len(fixture) else "streaming"
            _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(revision, text, state)))
            # 模拟正常客户端：每收到一批就立即消费，模拟及时确认。
            while not subscription.queue.empty():
                item = subscription.queue.get_nowait()
                if isinstance(item, ResyncRequired):
                    self.fail("healthy draining subscriber must not be forced to resync")
                received.append(item)
        while not subscription.queue.empty():
            item = subscription.queue.get_nowait()
            if not isinstance(item, ResyncRequired):
                received.append(item)
        diagnostics = subscription.diagnostics()
        # 有界性：峰值条数/字节都远低于预算，且不触发重同步。
        self.assertLess(diagnostics["peak_items"], 64)
        self.assertLess(diagnostics["peak_bytes"], 64 * 1024)
        self.assertEqual(diagnostics["resync_count"], 0)
        # 最终一致：重放全部保留事件可重建出与权威完整正文一致的文本。
        self.assertEqual(LongStreamReplayTests._reconstruct(received), fixture[-1])
        self.assertEqual(
            LongStreamReplayTests._reconstruct(store.list_after("s1")), fixture[-1]
        )


class SocketLossRecoveryTests(unittest.TestCase):
    """健康轮次中临时断线后的重连续订（任务 4.4 服务端侧）。"""

    def test_temporary_socket_loss_resumes_from_acknowledged_cursor(self) -> None:
        """断线期间发布的事件必须在重连后从确认游标连续重放，轮次不被取消。"""
        fixture = make_long_stream_fixture(steps=60)
        # 关闭订阅合并，保证逐条投递，便于断言确认游标与重放序号的一一对应。
        store = EventStore(coalescing_enabled=False)
        # 第一段连接：消费前 20 条并把确认游标推进到第 20 条。
        _, first = store.subscribe("s1", 0)
        for revision, text in enumerate(fixture[:20], start=1):
            _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(revision, text)))
        last_seq = 0
        while not first.queue.empty():
            item = first.queue.get_nowait()
            if not isinstance(item, ResyncRequired):
                last_seq = item.seq
        first.acknowledge(last_seq)
        store.unsubscribe(first)
        self.assertEqual(last_seq, 20)
        # 断线窗口内服务端继续健康产出（轮次不被取消）。
        for revision, text in enumerate(fixture[20:], start=21):
            state = "complete" if revision == len(fixture) else "streaming"
            _publish_no_ratelimit(store, "s1", _patch(_assistant_entry(revision, text, state)))
        # 第二段连接：从已确认游标续订，缺口事件必须完整重放。
        replay, second = store.subscribe("s1", last_seq)
        self.assertEqual(len(replay), 40)
        self.assertEqual([event.seq for event in replay], list(range(21, 61)))
        reconstructed = LongStreamReplayTests._reconstruct(
            store.list_after("s1", 0)
        )
        self.assertEqual(reconstructed, fixture[-1])
        store.unsubscribe(second)


class _BlockingLLMProvider(LLMProvider):
    """在 chat 中阻塞直到测试放行，用于构造“活跃轮次但无正文流”窗口。"""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *args: Any, **kwargs: Any) -> AssistantTurn:
        """阻塞模拟长耗时模型调用，放行后返回最终正文。"""
        self.entered.set()
        await self.release.wait()
        return AssistantTurn(raw_message={"role": "assistant", "content": "done"}, content="done")


class TurnKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    """活跃轮次进展信号独立于传输心跳（任务 4.1）。"""

    async def test_keepalive_emitted_while_turn_active_without_body_stream(self) -> None:
        """无正文流的活跃轮次应持续发布脱敏 turn_keepalive 事件。"""
        from app.api.schemas import ChatRequest

        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(llm_base_url="http://localhost", project_root=Path(tmp))
            settings.turn_keepalive_interval_s = 0.05
            store = SessionStore(Path(tmp) / "sessions")
            events = EventStore()
            engine = QueryEngine(
                settings=settings,
                session_store=store,
                llm=_BlockingLLMProvider(),
                event_store=events,
            )
            chat_task = asyncio.create_task(
                engine.submit_user_turn(
                    ChatRequest(session_id="s1", user_message="hi", request_id="r1")
                )
            )
            llm = engine._llm
            assert isinstance(llm, _BlockingLLMProvider)
            await asyncio.wait_for(llm.entered.wait(), timeout=5)
            await asyncio.sleep(0.2)
            keepalives = [
                event for event in events.list_after("s1") if event.type == "turn_keepalive"
            ]
            self.assertGreaterEqual(len(keepalives), 1)
            payload = keepalives[-1].payload
            self.assertEqual(payload.get("phase"), "active_turn")
            self.assertIn("last_event_seq", payload)
            self.assertIn("turn_id", payload)
            llm.release.set()
            await asyncio.wait_for(chat_task, timeout=5)
            # keepalive 属于实时进展信号，不进入持久化历史时间线。
            session = store.get_or_create("s1", set())
            self.assertNotIn(
                "turn_keepalive", {str(record.get("type", "")) for record in session.history_events}
            )


if __name__ == "__main__":
    unittest.main()
