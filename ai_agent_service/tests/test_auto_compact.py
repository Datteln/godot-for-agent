from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.message_transformer import estimate_message_tokens
from app.llm.provider import AssistantTurn, LLMProvider
from app.query.engine import QueryEngine
from app.sessions.store import Session, SessionStore


class _StubLLMProvider(LLMProvider):
    """`run_turn` 不会被这些测试触发，仅满足构造函数类型要求。"""

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *args: Any, **kwargs: Any) -> AssistantTurn:
        raise AssertionError("not expected to be called in these tests")


def _make_engine(tmp_dir: str, *, threshold: int = 60_000, keep_recent: int = 12) -> QueryEngine:
    settings = AppSettings(
        llm_base_url="http://localhost",
        project_root=Path(tmp_dir),
        auto_compact_token_threshold=threshold,
        auto_compact_keep_recent=keep_recent,
    )
    store = SessionStore(Path(tmp_dir) / "sessions")
    return QueryEngine(
        settings=settings,
        session_store=store,
        llm=_StubLLMProvider(),
        event_store=EventStore(),
    )


def _frame_with_messages(message_count: int, *, big: bool = False) -> Frame:
    content_unit = "x" * 4000 if big else "hi"
    messages: list[dict[str, Any]] = [{"role": "system", "content": "system prompt"}]
    for i in range(message_count):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": f"{content_unit}-{i}"})
    return Frame(id="f1", agent=get_agent("coordinator", set()), messages=messages)


class NeedsAutoCompactTests(unittest.TestCase):
    def test_false_for_small_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp, threshold=60_000)
            session = Session(session_id="s1", agent_stack=[_frame_with_messages(4)])
            self.assertFalse(engine._needs_auto_compact(session))

    def test_true_when_any_frame_exceeds_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp, threshold=1000)
            session = Session(session_id="s1", agent_stack=[_frame_with_messages(20, big=True)])
            self.assertTrue(engine._needs_auto_compact(session))

    def test_false_when_disabled_threshold_not_checked_by_caller(self) -> None:
        # `_needs_auto_compact` itself doesn't read `auto_compact_enabled` — that
        # gate lives at the call site in `_submit_locked`; this test documents
        # that the helper is a pure size check, independent of the feature flag.
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp, threshold=1000)
            session = Session(session_id="s1", agent_stack=[_frame_with_messages(20, big=True)])
            engine._settings.auto_compact_enabled = False
            self.assertTrue(engine._needs_auto_compact(session))

    def test_checks_all_frames_not_just_top(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp, threshold=1000)
            small = _frame_with_messages(2)
            big = _frame_with_messages(20, big=True)
            session = Session(session_id="s1", agent_stack=[big, small])
            self.assertTrue(engine._needs_auto_compact(session))


class CompactTriggeredByTests(unittest.TestCase):
    def test_manual_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine._store.get_or_create("s1", engine.available_tools)
            session.agent_stack = [_frame_with_messages(20)]
            result = engine.compact("s1")
            self.assertIsInstance(result, dict)
            events = engine._events.list_after("s1", 0) if engine._events else []
            payload = next(e.payload for e in events if e.type == "compact_boundary")
            self.assertEqual(payload["triggered_by"], "manual")

    def test_auto_label_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine._store.get_or_create("s1", engine.available_tools)
            session.agent_stack = [_frame_with_messages(20)]
            engine.compact("s1", triggered_by="auto")
            events = engine._events.list_after("s1", 0) if engine._events else []
            payload = next(e.payload for e in events if e.type == "compact_boundary")
            self.assertEqual(payload["triggered_by"], "auto")

    def test_started_event_precedes_boundary_event(self) -> None:
        # 前端依赖这个顺序渲染"正在压缩…"→"已压缩…"两条消息块；颠倒顺序会让
        # "正在压缩"显示在压缩完成之后，体验上是反的。
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine._store.get_or_create("s1", engine.available_tools)
            session.agent_stack = [_frame_with_messages(20)]
            engine.compact("s1", triggered_by="auto")
            events = engine._events.list_after("s1", 0)
            types = [e.type for e in events if e.type in ("compact_started", "compact_boundary")]
            self.assertEqual(types, ["compact_started", "compact_boundary"])
            started_payload = next(e.payload for e in events if e.type == "compact_started")
            self.assertEqual(started_payload["triggered_by"], "auto")
            self.assertEqual(started_payload["keep_recent"], 12)


class OversizedSingleMessageCompactionTests(unittest.TestCase):
    """复现并验证修复：消息数 < keep_recent+2 但单条消息超大时不再是空压缩。"""

    def _session_with_oversized_message(self, engine: QueryEngine, *, oversized_at: int, total: int) -> Session:
        session = engine._store.get_or_create("s1", engine.available_tools)
        messages: list[dict[str, Any]] = [{"role": "system", "content": "system prompt"}]
        for i in range(total):
            role = "user" if i % 2 == 0 else "assistant"
            content = "x" * 20000 if i == oversized_at else f"hi-{i}"
            messages.append({"role": role, "content": content})
        session.agent_stack = [Frame(id="f1", agent=get_agent("coordinator", set()), messages=messages)]
        return session

    def test_few_messages_with_one_oversized_gets_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp, threshold=1000)
            # 总共 6 条消息（远低于 keep_recent=12 的 14 条门槛），中间一条 20000 字符。
            session = self._session_with_oversized_message(engine, oversized_at=2, total=6)
            before = estimate_message_tokens(session.agent_stack[0].messages)
            self.assertTrue(engine._needs_auto_compact(session))

            result = engine.compact("s1", triggered_by="auto")

            self.assertEqual(result["compacted_frames"], 0)  # too few messages for history-summary path
            self.assertGreater(result["truncated_messages"], 0)
            after = estimate_message_tokens(session.agent_stack[0].messages)
            self.assertLess(after, before)

    def test_repeated_compact_does_not_keep_truncating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            self._session_with_oversized_message(engine, oversized_at=2, total=6)
            first = engine.compact("s1", triggered_by="auto")
            second = engine.compact("s1", triggered_by="auto")
            self.assertGreater(first["truncated_messages"], 0)
            self.assertEqual(second["truncated_messages"], 0)  # already shrunk, idempotent

    def test_last_message_is_never_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = self._session_with_oversized_message(engine, oversized_at=5, total=6)
            frame = session.agent_stack[0]
            original_last = frame.messages[-1]["content"]
            engine.compact("s1", triggered_by="auto")
            self.assertEqual(frame.messages[-1]["content"], original_last)


class AutoCompactSettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        settings = AppSettings(llm_base_url="http://localhost")
        self.assertTrue(settings.auto_compact_enabled)
        self.assertEqual(settings.auto_compact_token_threshold, 200_000)
        self.assertEqual(settings.auto_compact_keep_recent, 12)


if __name__ == "__main__":
    unittest.main()
