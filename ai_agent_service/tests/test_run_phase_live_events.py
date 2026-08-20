"""运行期工具/步骤事件的节流实时推送测试（restore-delegation-tools-and-live-events）。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.api.schemas import ChatRequest
from app.application.publication import (
    LIVE_BATCH_MAX,
    LIVE_FLUSH_WINDOW_S,
    SubmissionScope,
    _submission_event_delivery,
)
from app.config import AppSettings
from app.events.store import EventStore
from app.orchestrator.map_artifacts import StagedMapArtifactTurn
from app.sessions.store import SessionStore
from tests.application_test_support import build_test_application


class _DummyProvider:
    """占位 LLM provider：live 事件测试不触碰 provider。"""

    async def chat(self, *args, **kwargs):
        raise AssertionError("provider should not be called")


def _make_engine(tmp: str):
    return build_test_application(
        settings=AppSettings(
            llm_base_url="http://localhost",
            project_root=Path(tmp),
            rag_auto_build_enabled=False,
        ),
        session_store=SessionStore(Path(tmp) / "sessions"),
        llm=_DummyProvider(),  # type: ignore[arg-type]
        event_store=EventStore(),
    )


def _make_scope(session, request_id: str = "request-1", turn_id: str = "turn-1") -> SubmissionScope:
    return SubmissionScope(
        session=session,
        request_id=request_id,
        turn_id=turn_id,
        map_artifact_turn=StagedMapArtifactTurn(
            session_id=session.session_id,
            turn_id=turn_id,
            request_id=request_id,
        ),
    )


class RunPhaseLiveEventTests(unittest.TestCase):
    def test_delivery_classification(self) -> None:
        self.assertEqual(_submission_event_delivery("agent_tool_calls"), "throttled_live")
        self.assertEqual(_submission_event_delivery("server_tool_start"), "throttled_live")
        self.assertEqual(_submission_event_delivery("server_tool_result"), "throttled_live")
        self.assertEqual(_submission_event_delivery("agent_step"), "throttled_live")
        # 非展示事件保持 transactional（不实时化）
        self.assertEqual(_submission_event_delivery("context_usage"), "transactional")
        self.assertEqual(_submission_event_delivery("cache_hit"), "transactional")

    def test_live_events_batch_flush_when_budget_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            store: SessionStore = engine.store
            session = store.get_or_create("s1", engine.available_tools)
            scope = _make_scope(session)

            def tool_event(index: int, event_type: str) -> None:
                engine.publisher.emit(
                    "s1",
                    event_type,
                    {
                        "frame_id": "frame-1",
                        "tool_use_id": f"tool-{index}",
                        "name": "tool.search",
                    },
                    scope,
                )

            # 未满批量且未到时间窗：事件只进 live_buffer，store 不可见
            tool_event(1, "server_tool_start")
            tool_event(2, "server_tool_start")
            self.assertEqual(len(scope.live_buffer), 2)
            self.assertEqual(engine.events.list_after("s1", 0), [])
            self.assertTrue(scope.preview.items)

            # 达到 LIVE_BATCH_MAX 触发批量 flush
            tool_event(3, "server_tool_start")
            tool_event(4, "server_tool_start")
            self.assertEqual(scope.live_buffer, [])
            live_events = engine.events.list_after("s1", 0)
            self.assertEqual(len(live_events), 4)
            for event in live_events:
                self.assertTrue(event.payload["provisional"])
                self.assertIn("preview_id", event.payload)

    def test_time_window_flush(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine.store.get_or_create("s1", engine.available_tools)
            scope = _make_scope(session)
            scope.last_live_flush = time.monotonic() - LIVE_FLUSH_WINDOW_S - 0.1

            engine.publisher.emit(
                "s1",
                "agent_step",
                {"frame_id": "frame-1", "loop": 2},
                scope,
            )
            self.assertEqual(scope.live_buffer, [])
            self.assertEqual(len(engine.events.list_after("s1", 0)), 1)

    def test_commit_flush_does_not_duplicate_live_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine.store.get_or_create("s1", engine.available_tools)
            scope = _make_scope(session)

            # 触发 live 批量（>= LIVE_BATCH_MAX）
            for index in range(LIVE_BATCH_MAX):
                engine.publisher.emit(
                    "s1",
                    "server_tool_result",
                    {"frame_id": "frame-1", "tool_use_id": f"tool-{index}"},
                    scope,
                )
            # 再发一条未满批量的，留在 buffer
            engine.publisher.emit(
                "s1",
                "agent_step",
                {"frame_id": "frame-1", "loop": 3},
                scope,
            )
            engine.publisher.flush(scope)
            engine.publisher.resolve_previews(scope, committed=True)

            types = [event.type for event in engine.events.list_after("s1", 0)]
            self.assertEqual(types.count("server_tool_result"), LIVE_BATCH_MAX)
            self.assertEqual(types.count("agent_step"), 1)
            self.assertEqual(types.count("submission_preview_committed"), 1)
            # scope.events 不含任何 live 事件（live 走独立通道，不进事务缓冲）
            self.assertEqual(scope.events, [])

    def test_preview_boundary_covers_live_tool_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine.store.get_or_create("s1", engine.available_tools)
            scope = _make_scope(session)

            for index in range(LIVE_BATCH_MAX):
                engine.publisher.emit(
                    "s1",
                    "agent_tool_calls",
                    {
                        "frame_id": "frame-1",
                        "calls": [
                            {
                                "id": f"tool-{index}",
                                "name": "tool.search",
                                "arguments": "{}",
                            }
                        ],
                    },
                    scope,
                )
            engine.publisher.flush(scope)
            engine.publisher.resolve_previews(scope, committed=True)

            latest = engine.events.list_after("s1", 0)[-1]
            self.assertEqual(latest.type, "submission_preview_committed")
            preview_ids = latest.payload["preview_ids"]
            # 每条 agent_tool_calls 事件对应一个 preview_id（事件级粒度）
            self.assertEqual(len(preview_ids), LIVE_BATCH_MAX)
            self.assertEqual(len(set(preview_ids)), LIVE_BATCH_MAX)
            for preview_id in preview_ids:
                self.assertIn("agent_tool_calls", preview_id)

    def test_discard_boundary_removes_live_event_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine.store.get_or_create("s1", engine.available_tools)
            scope = _make_scope(session)

            engine.publisher.emit(
                "s1",
                "server_tool_start",
                {"frame_id": "frame-1", "tool_use_id": "tool-x"},
                scope,
            )
            engine.publisher.flush(scope)
            engine.publisher.resolve_previews(scope, committed=False, reason="session_persistence_failed")

            latest = engine.events.list_after("s1", 0)[-1]
            self.assertEqual(latest.type, "submission_preview_discarded")
            self.assertEqual(latest.payload["reason"], "session_persistence_failed")
            self.assertEqual(len(latest.payload["preview_ids"]), 1)

    def test_agent_error_retains_previews_with_failure_reason(self) -> None:
        """失败轮次（会话已提交的 agent 层错误）保留 preview 并标记错误码。"""
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine.store.get_or_create("s1", engine.available_tools)
            scope = _make_scope(session)
            request_id, turn_id = "request-1", "turn-1"

            engine.publisher.emit(
                "s1",
                "agent_reasoning_delta",
                {"frame_id": "frame-1", "message_index": 2, "text": "thinking", "append_delta": True},
                scope,
            )
            for index in range(LIVE_BATCH_MAX):
                engine.publisher.emit(
                    "s1",
                    "server_tool_start",
                    {"frame_id": "frame-1", "tool_use_id": f"tool-{index}"},
                    scope,
                )
            engine.publisher.flush(scope)
            engine.publisher.resolve_previews(
                scope,
                committed=True,
                reason="agent_turn_budget_exhausted",
            )

            events = engine.events.list_after("s1", 0)
            boundary = events[-1]
            self.assertEqual(boundary.type, "submission_preview_committed")
            self.assertEqual(boundary.payload["reason"], "agent_turn_budget_exhausted")
            # 思考 preview 与全部 live 工具事件都被保留（preview_ids 覆盖）
            all_preview_ids = set(boundary.payload["preview_ids"])
            self.assertIn(
                f"{request_id}:{turn_id}:agent_reasoning_delta:frame-1:2",
                all_preview_ids,
            )
            tool_ids = {pid for pid in all_preview_ids if "server_tool_start" in pid}
            self.assertEqual(len(tool_ids), LIVE_BATCH_MAX)


if __name__ == "__main__":
    unittest.main()