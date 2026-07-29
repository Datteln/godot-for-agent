from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.api.schemas import ChatRequest, ToolResult
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMProvider
from app.main import create_app
from app.orchestrator.map_artifacts import StagedMapArtifactTurn
from app.query.engine import (
    QueryEngine,
    _PUBLICATION_BUFFER,
    _SubmissionPublicationBuffer,
    _submission_event_delivery,
)
from app.sessions.store import SessionStore


class _UnusedProvider(LLMProvider):
    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *args: Any, **kwargs: Any) -> AssistantTurn:
        raise AssertionError("provider is not used by these unit tests")


class _PausingStreamProvider(LLMProvider):
    def __init__(self) -> None:
        self.preview_emitted = asyncio.Event()
        self.release = asyncio.Event()

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *args: Any, **kwargs: Any) -> AssistantTurn:
        on_delta = kwargs.get("on_delta")
        assert on_delta is not None
        on_delta("text", "preview-before-commit", 3)
        self.preview_emitted.set()
        await self.release.wait()
        return AssistantTurn(
            raw_message={"role": "assistant", "content": "preview-before-commit"},
            content="preview-before-commit",
        )


def _make_engine(tmp: str) -> QueryEngine:
    return QueryEngine(
        settings=AppSettings(
            llm_base_url="http://localhost",
            project_root=Path(tmp),
        ),
        session_store=SessionStore(Path(tmp) / "sessions"),
        llm=_UnusedProvider(),
        event_store=EventStore(),
    )


def _make_pending_tool_submission(
    tmp: str,
    provider: LLMProvider,
) -> tuple[QueryEngine, SessionStore, ChatRequest]:
    store = SessionStore(Path(tmp) / "sessions")
    engine = QueryEngine(
        settings=AppSettings(
            llm_base_url="http://localhost",
            project_root=Path(tmp),
            rag_auto_build_enabled=False,
        ),
        session_store=store,
        llm=provider,
        event_store=EventStore(),
    )
    session = store.get_or_create("s1", engine.available_tools)
    session.agent_stack = [
        Frame(
            id="frame-1",
            agent=get_agent("coordinator", engine.available_tools),
            messages=[
                {"role": "system", "content": "system"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "tool-1",
                            "type": "function",
                            "function": {
                                "name": "read_scene_tree",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
            ],
        )
    ]
    session.pending_turn_id = "turn-1"
    session.pending_tool_call_ids = {"tool-1"}
    session.pending_tool_calls = {
        "tool-1": {
            "name": "read_scene_tree",
            "input": {},
            "frame_id": "frame-1",
            "needs_confirm": False,
            "authorization": "allow",
        }
    }
    store.save(session)
    request = ChatRequest(
        session_id="s1",
        request_id="request-1",
        tool_results=[
            ToolResult(
                tool_use_id="tool-1",
                frame_id="frame-1",
                turn_id="turn-1",
                status="applied",
                result={"name": "Root", "children": []},
            )
        ],
    )
    return engine, store, request


class EventStorePagingTests(unittest.TestCase):
    def test_pages_are_ordered_bounded_and_advance_only_to_returned_event(self) -> None:
        store = EventStore()
        for index in range(5):
            store.append("s1", "status", {"index": index})

        first = store.page_after("s1", 0, limit=2)
        self.assertEqual([event.seq for event in first.events], [1, 2])
        self.assertEqual(first.cursor, 2)
        self.assertTrue(first.has_more)

        second = store.page_after("s1", first.cursor, limit=2)
        self.assertEqual([event.seq for event in second.events], [3, 4])
        self.assertEqual(second.cursor, 4)
        self.assertTrue(second.has_more)

        last = store.page_after("s1", second.cursor, limit=2)
        self.assertEqual([event.seq for event in last.events], [5])
        self.assertEqual(last.cursor, 5)
        self.assertFalse(last.has_more)

    def test_coalesced_snapshot_replacement_keeps_new_sequence(self) -> None:
        store = EventStore()
        store.append(
            "s1",
            "agent_text_delta",
            {"frame_id": "f1", "loop": 1, "text": "a"},
        )
        replacement = store.append(
            "s1",
            "agent_text_delta",
            {"frame_id": "f1", "loop": 1, "text": "ab"},
        )

        page = store.page_after("s1", 0, limit=10)
        self.assertEqual(len(page.events), 1)
        self.assertEqual(page.events[0].seq, replacement.seq)
        self.assertEqual(page.events[0].payload["text"], "ab")
        self.assertEqual(page.cursor, replacement.seq)

    def test_append_only_fragments_are_never_coalesced(self) -> None:
        store = EventStore()
        for fragment in ("a", "b", "c"):
            store.append(
                "s1",
                "agent_text_delta",
                {
                    "frame_id": "f1",
                    "loop": 1,
                    "text": fragment,
                    "append_delta": True,
                },
            )

        page = store.page_after("s1", 0, limit=2)
        self.assertEqual([event.payload["text"] for event in page.events], ["a", "b"])
        self.assertTrue(page.has_more)
        tail = store.page_after("s1", page.cursor, limit=2)
        self.assertEqual([event.payload["text"] for event in tail.events], ["c"])
        self.assertFalse(tail.has_more)

    def test_invalid_page_limit_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            EventStore().page_after("s1", 0, limit=0)

    def test_overflow_retains_order_and_exposes_the_first_available_sequence(self) -> None:
        store = EventStore()
        for index in range(510):
            store.append(
                "s1",
                "agent_text_delta",
                {
                    "frame_id": "f1",
                    "message_index": 1,
                    "append_delta": True,
                    "text": str(index),
                },
            )
        page = store.page_after("s1", 0, limit=50)
        self.assertEqual(page.events[0].seq, 11)
        self.assertEqual(page.events[-1].seq, 60)
        self.assertEqual(page.cursor, 60)
        self.assertTrue(page.has_more)


class ChatEventsRouteTests(unittest.TestCase):
    def test_route_returns_bounded_pages_and_validates_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                ),
                token="test-token",
            )
            store: EventStore = app.state.event_store
            for index in range(55):
                store.append("s1", "status", {"index": index})
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(app) as client:
                response = client.get(
                    "/chat/events?session_id=s1&after=0",
                    headers=headers,
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                self.assertEqual(len(body["events"]), 50)
                self.assertEqual(body["cursor"], 50)
                self.assertTrue(body["has_more"])

                tail = client.get(
                    "/chat/events?session_id=s1&after=50&limit=10",
                    headers=headers,
                ).json()
                self.assertEqual([event["seq"] for event in tail["events"]], [51, 52, 53, 54, 55])
                self.assertEqual(tail["cursor"], 55)
                self.assertFalse(tail["has_more"])

                for query in ("limit=0", "limit=201", "after=-1"):
                    invalid = client.get(
                        f"/chat/events?session_id=s1&{query}",
                        headers=headers,
                    )
                    self.assertEqual(invalid.status_code, 422, query)

    def test_idle_progress_remains_out_of_band_when_page_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                ),
                token="test-token",
            )
            engine: QueryEngine = app.state.query_engine
            engine._set_turn_progress(
                "s1",
                owner_id=7,
                request_id="request-1",
                turn_id="turn-1",
                phase="tool_result_transaction",
            )
            with TestClient(app) as client:
                response = client.get(
                    "/chat/events?session_id=s1&after=0&limit=1",
                    headers={"Authorization": "Bearer test-token"},
                )
            body = response.json()
            self.assertEqual(body["events"], [])
            self.assertEqual(body["cursor"], 0)
            self.assertFalse(body["has_more"])
            self.assertEqual(body["progress"]["type"], "turn_progress")
            self.assertEqual(body["progress"]["request_id"], "request-1")


class SubmissionPreviewPublicationTests(unittest.TestCase):
    def test_delivery_classification_is_explicit(self) -> None:
        self.assertEqual(
            _submission_event_delivery("agent_text_delta"),
            "provisional_preview",
        )
        self.assertEqual(
            _submission_event_delivery("agent_reasoning_delta"),
            "provisional_preview",
        )
        self.assertEqual(
            _submission_event_delivery("turn_progress"),
            "out_of_band_liveness",
        )
        self.assertEqual(_submission_event_delivery("tool_calls"), "transactional")

    def test_preview_is_visible_before_flush_and_is_not_staged_twice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine._store.get_or_create("s1", engine.available_tools)
            buffer = _SubmissionPublicationBuffer(
                session=session,
                request_id="request-1",
                turn_id="turn-1",
                map_artifact_turn=StagedMapArtifactTurn(
                    session_id="s1",
                    turn_id="turn-1",
                    request_id="request-1",
                ),
            )
            token = _PUBLICATION_BUFFER.set(buffer)
            try:
                seq = engine._emit(
                    "s1",
                    "agent_text_delta",
                    {
                        "frame_id": "frame-1",
                        "message_index": 2,
                        "text": "hello",
                        "append_delta": True,
                    },
                )
                engine._emit("s1", "tool_finished", {"tool_use_id": "tool-1"})
            finally:
                _PUBLICATION_BUFFER.reset(token)

            self.assertGreater(seq, 0)
            visible = engine._events.list_after("s1", 0)
            self.assertEqual([event.type for event in visible], ["agent_text_delta"])
            preview = visible[0].payload
            self.assertTrue(preview["provisional"])
            self.assertEqual(preview["request_id"], "request-1")
            self.assertEqual(preview["turn_id"], "turn-1")
            self.assertEqual(preview["frame_id"], "frame-1")
            self.assertNotIn(
                "provisional",
                session.history_events[-1]["payload"],
            )
            self.assertEqual(len(buffer.events), 1)
            self.assertEqual(buffer.events[0][1], "tool_finished")

            engine._flush_submission_publications(buffer)
            engine._resolve_submission_previews(buffer, committed=True)
            types = [event.type for event in engine._events.list_after("s1", 0)]
            self.assertEqual(
                types,
                [
                    "agent_text_delta",
                    "tool_finished",
                    "submission_preview_committed",
                ],
            )
            self.assertEqual(types.count("agent_text_delta"), 1)

    def test_discard_boundary_does_not_publish_transactional_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            engine = _make_engine(tmp)
            session = engine._store.get_or_create("s1", engine.available_tools)
            buffer = _SubmissionPublicationBuffer(
                session=session,
                request_id="request-1",
                turn_id="turn-1",
                map_artifact_turn=StagedMapArtifactTurn(
                    session_id="s1",
                    turn_id="turn-1",
                    request_id="request-1",
                ),
            )
            token = _PUBLICATION_BUFFER.set(buffer)
            try:
                engine._emit(
                    "s1",
                    "agent_reasoning_delta",
                    {
                        "frame_id": "frame-1",
                        "message_index": 2,
                        "text": "thinking",
                        "append_delta": True,
                    },
                )
                engine._emit("s1", "grant_created", {"grant_id": "secret"})
            finally:
                _PUBLICATION_BUFFER.reset(token)

            engine._resolve_submission_previews(
                buffer,
                committed=False,
                reason="session_persistence_failed",
            )
            events = engine._events.list_after("s1", 0)
            self.assertEqual(
                [event.type for event in events],
                ["agent_reasoning_delta", "submission_preview_discarded"],
            )
            self.assertNotIn("grant_id", events[-1].payload)
            self.assertEqual(
                events[-1].payload["reason"],
                "session_persistence_failed",
            )


class AtomicSubmissionStreamingRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_preview_is_observable_before_completion_and_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PausingStreamProvider()
            store = SessionStore(Path(tmp) / "sessions")
            engine = QueryEngine(
                settings=AppSettings(
                    llm_base_url="http://localhost",
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                ),
                session_store=store,
                llm=provider,
                event_store=EventStore(),
            )
            session = store.get_or_create("s1", engine.available_tools)
            frame = Frame(
                id="frame-1",
                agent=get_agent("coordinator", engine.available_tools),
                messages=[
                    {"role": "system", "content": "system"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "tool-1",
                                "type": "function",
                                "function": {
                                    "name": "read_scene_tree",
                                    "arguments": "{}",
                                },
                            }
                        ],
                    },
                ],
            )
            session.agent_stack = [frame]
            session.pending_turn_id = "turn-1"
            session.pending_tool_call_ids = {"tool-1"}
            session.pending_tool_calls = {
                "tool-1": {
                    "name": "read_scene_tree",
                    "input": {},
                    "frame_id": "frame-1",
                    "needs_confirm": False,
                    "authorization": "allow",
                }
            }
            store.save(session)

            original_save = store.save
            saves_after_submission_started: list[str] = []

            def recording_save(candidate: Any) -> None:
                saves_after_submission_started.append(candidate.session_id)
                original_save(candidate)

            store.save = recording_save  # type: ignore[method-assign]
            request = ChatRequest(
                session_id="s1",
                request_id="request-1",
                tool_results=[
                    ToolResult(
                        tool_use_id="tool-1",
                        frame_id="frame-1",
                        turn_id="turn-1",
                        status="applied",
                        result={"name": "Root", "children": []},
                    )
                ],
            )
            submission = asyncio.create_task(engine.submit_user_turn(request))
            await asyncio.wait_for(provider.preview_emitted.wait(), timeout=5)

            self.assertFalse(submission.done())
            self.assertEqual(saves_after_submission_started, [])
            visible_before_commit = engine._events.list_after("s1", 0)
            self.assertEqual(
                [event.type for event in visible_before_commit],
                ["agent_text_delta"],
            )
            self.assertTrue(visible_before_commit[0].payload["provisional"])

            provider.release.set()
            response = await asyncio.wait_for(submission, timeout=5)
            self.assertEqual(response.type, "final")
            self.assertEqual(saves_after_submission_started, ["s1"])
            committed_types = [
                event.type for event in engine._events.list_after("s1", 0)
            ]
            self.assertEqual(committed_types.count("agent_text_delta"), 1)
            self.assertEqual(committed_types[-1], "submission_preview_committed")
            self.assertIn("tool_results_received", committed_types)

    async def test_cancellation_discards_preview_without_staged_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PausingStreamProvider()
            engine, _store, request = _make_pending_tool_submission(tmp, provider)
            submission = asyncio.create_task(engine.submit_user_turn(request))
            await asyncio.wait_for(provider.preview_emitted.wait(), timeout=5)

            submission.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await submission

            event_types = [event.type for event in engine._events.list_after("s1", 0)]
            self.assertEqual(
                event_types,
                ["agent_text_delta", "submission_preview_discarded"],
            )
            self.assertEqual(
                engine._events.list_after("s1", 0)[-1].payload["reason"],
                "cancelled",
            )

    async def test_session_save_failure_discards_preview_without_staged_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PausingStreamProvider()
            engine, store, request = _make_pending_tool_submission(tmp, provider)

            def failing_save(_candidate: Any) -> None:
                raise OSError("simulated persistence failure")

            store.save = failing_save  # type: ignore[method-assign]
            submission = asyncio.create_task(engine.submit_user_turn(request))
            await asyncio.wait_for(provider.preview_emitted.wait(), timeout=5)
            provider.release.set()
            response = await asyncio.wait_for(submission, timeout=5)

            self.assertEqual(response.type, "error")
            events = engine._events.list_after("s1", 0)
            self.assertEqual(
                [event.type for event in events],
                ["agent_text_delta", "submission_preview_discarded"],
            )
            self.assertEqual(
                events[-1].payload["reason"],
                "session_persistence_failed",
            )

    async def test_submission_reducer_failure_discards_preview_without_staged_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _UnusedProvider()
            engine, _store, request = _make_pending_tool_submission(tmp, provider)

            async def failing_submit(*_args: Any, **_kwargs: Any) -> Any:
                engine._emit(
                    "s1",
                    "agent_text_delta",
                    {
                        "frame_id": "frame-1",
                        "message_index": 2,
                        "text": "preview",
                        "append_delta": True,
                    },
                )
                engine._emit("s1", "grant_created", {"grant_id": "must-not-leak"})
                raise RuntimeError("simulated reducer failure")

            engine._submit_locked = failing_submit  # type: ignore[method-assign]
            response = await engine.submit_user_turn(request)

            self.assertEqual(response.type, "error")
            events = engine._events.list_after("s1", 0)
            self.assertEqual(
                [event.type for event in events],
                ["agent_text_delta", "submission_preview_discarded"],
            )
            self.assertEqual(events[-1].payload["reason"], "submission_failed")


if __name__ == "__main__":
    unittest.main()
