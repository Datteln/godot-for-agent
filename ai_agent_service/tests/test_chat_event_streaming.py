from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.api.schemas import ChatRequest, ToolResult
from app.application.publication import (
    SubmissionScope,
    _submission_event_delivery,
)
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMProvider
from app.main import create_app
from app.orchestrator.map_artifacts import StagedMapArtifactTurn
from app.sessions.store import SessionStore
from app.tools.front_tools import register_front_tools

register_front_tools(enabled=True)
from tests.application_test_support import ApplicationTestRig, build_test_application


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


def _make_engine(tmp: str) -> ApplicationTestRig:
    return build_test_application(
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
) -> tuple[ApplicationTestRig, SessionStore, ChatRequest]:
    store = SessionStore(Path(tmp) / "sessions")
    engine = build_test_application(
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

    def test_snapshot_events_keep_contiguous_authoritative_sequence(self) -> None:
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
        self.assertEqual([event.seq for event in page.events], [1, 2])
        self.assertEqual(page.events[-1].seq, replacement.seq)
        self.assertEqual(page.events[-1].payload["text"], "ab")
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


class ChatWebSocketRouteTests(unittest.TestCase):
    def test_socket_rejects_missing_bearer_during_handshake(self) -> None:
        """Authentication is decided before accepting the WebSocket session."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(project_root=Path(tmp), rag_auto_build_enabled=False),
                token="test-token",
            )
            with TestClient(app) as client:
                with self.assertRaises(WebSocketDisconnect) as raised:
                    with client.websocket_connect("/chat/socket"):
                        pass
            denial_status = getattr(raised.exception, "status_code", None)
            close_code = getattr(raised.exception, "code", None)
            self.assertTrue(denial_status == 401 or close_code == 4401)

    def test_socket_returns_bounded_batches_and_accepts_cumulative_ack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                    websocket_batch_event_limit=10,
                ),
                token="test-token",
            )
            store: EventStore = app.state.event_store
            session = app.state.session_store.get_or_create("s1", set())
            for index in range(55):
                store.append(
                    "s1",
                    "status",
                    {"index": index},
                    session_epoch=session.session_epoch,
                )
            headers = {"Authorization": "Bearer test-token"}
            with TestClient(app) as client:
                with client.websocket_connect("/chat/socket", headers=headers) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 0,
                        }
                    )
                    hello = socket.receive_json()
                    self.assertEqual(hello["type"], "hello")
                    first = socket.receive_json()
                    self.assertEqual(first["type"], "event_batch")
                    self.assertEqual([item["seq"] for item in first["events"]], list(range(1, 11)))
                    socket.send_json(
                        {
                            "type": "ack",
                            "protocol_version": 1,
                            "session_epoch": session.session_epoch,
                            "accepted_seq": 10,
                        }
                    )
                    second = socket.receive_json()
                    self.assertEqual(second["first_seq"], 11)
                    self.assertEqual(second["last_seq"], 20)

    def test_polling_route_is_absent_and_snapshot_is_bounded_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                ),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            with TestClient(app) as client:
                removed = client.get(
                    "/chat/events?session_id=s1&after=0",
                    headers={"Authorization": "Bearer test-token"},
                )
                snapshot = client.get(
                    "/chat/snapshot?session_id=s1",
                    headers={"Authorization": "Bearer test-token"},
                )
            self.assertEqual(removed.status_code, 404)
            self.assertEqual(snapshot.status_code, 200)
            self.assertEqual(snapshot.json()["session_epoch"], session.session_epoch)

    def test_socket_rejects_stale_epoch_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(project_root=Path(tmp), rag_auto_build_enabled=False),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch + "-stale",
                            "after_seq": 0,
                        }
                    )
                    problem = socket.receive_json()
            self.assertEqual(problem["type"], "snapshot_required")
            self.assertEqual(problem["reason"], "stale_epoch")

    def test_socket_rejects_ack_beyond_sent_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(project_root=Path(tmp), rag_auto_build_enabled=False),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 0,
                        }
                    )
                    self.assertEqual(socket.receive_json()["type"], "hello")
                    socket.send_json(
                        {
                            "type": "ack",
                            "protocol_version": 1,
                            "session_epoch": session.session_epoch,
                            "accepted_seq": 1,
                        }
                    )
                    problem = socket.receive_json()
            self.assertEqual(problem["type"], "close")
            self.assertEqual(problem["code"], "ack_out_of_range")

    def test_socket_notifies_old_epoch_on_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(project_root=Path(tmp), rag_auto_build_enabled=False),
                token="test-token",
            )
            session_store = app.state.session_store
            event_store: EventStore = app.state.event_store
            session = session_store.get_or_create("s1", set())
            event_store.ensure_sequence("s1", 0, session_epoch=session.session_epoch)
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 0,
                        }
                    )
                    self.assertEqual(socket.receive_json()["type"], "hello")
                    session_store.reset("s1")
                    new_epoch = session_store.current_epoch("s1")
                    event_store.reset("s1", str(new_epoch))
                    changed = socket.receive_json()
            self.assertEqual(changed["type"], "epoch_changed")
            self.assertEqual(changed["new_epoch"], new_epoch)

    def test_expired_cursor_requires_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(project_root=Path(tmp), rag_auto_build_enabled=False),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            store: EventStore = app.state.event_store
            for index in range(510):
                store.append(
                    "s1",
                    "status",
                    {"index": index},
                    session_epoch=session.session_epoch,
                )
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 0,
                        }
                    )
                    problem = socket.receive_json()
            self.assertEqual(problem["type"], "snapshot_required")
            self.assertEqual(problem["reason"], "cursor_expired")

    def test_restart_without_retained_events_requires_snapshot(self) -> None:
        """A durable high-water with an empty memory window cannot fake resume."""
        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(project_root=Path(tmp), rag_auto_build_enabled=False)
            first = create_app(settings, token="test-token")
            session = first.state.session_store.get_or_create("s1", set())
            session.history_event_counter = 8
            first.state.session_store.save(session)

            restarted = create_app(settings, token="test-token")
            with TestClient(restarted) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 3,
                        }
                    )
                    problem = socket.receive_json()
            self.assertEqual(problem["type"], "snapshot_required")
            self.assertEqual(problem["reason"], "cursor_expired")
            self.assertEqual(problem["high_water_seq"], 8)

    def test_cursor_ahead_of_high_water_requires_snapshot(self) -> None:
        """A client cannot acknowledge unseen sequence space after a reset/restart."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(project_root=Path(tmp), rag_auto_build_enabled=False),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 9,
                        }
                    )
                    problem = socket.receive_json()
            self.assertEqual(problem["type"], "snapshot_required")
            self.assertEqual(problem["reason"], "sequence_gap")

    def test_unacknowledged_client_is_closed_as_stalled(self) -> None:
        """Delivery pauses at the unacked bound and closes with a resumable cursor."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                    websocket_batch_event_limit=1,
                    websocket_unacked_event_limit=1,
                    websocket_ack_timeout_s=1.0,
                    websocket_stall_timeout_s=2.0,
                    websocket_heartbeat_interval_s=1.0,
                ),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            app.state.event_store.append(
                "s1", "status", {"index": 1}, session_epoch=session.session_epoch
            )
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 0,
                        }
                    )
                    self.assertEqual(socket.receive_json()["type"], "hello")
                    self.assertEqual(socket.receive_json()["type"], "event_batch")
                    terminal: dict[str, Any] = {}
                    for _ in range(4):
                        candidate = socket.receive_json()
                        if candidate["type"] == "close":
                            terminal = candidate
                            break
            self.assertEqual(terminal["code"], "client_stalled")
            self.assertEqual(terminal["resume_after_seq"], 0)

    def test_transport_ping_pong_does_not_advance_event_cursor(self) -> None:
        """Liveness traffic is independent from ordered application events."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                    websocket_heartbeat_interval_s=1.0,
                ),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 0,
                        }
                    )
                    hello = socket.receive_json()
                    ping = socket.receive_json()
                    self.assertEqual(ping["type"], "ping")
                    socket.send_json(
                        {
                            "type": "pong",
                            "protocol_version": 1,
                            "nonce": ping["nonce"],
                        }
                    )
            self.assertEqual(hello["accepted_seq"], 0)
            self.assertEqual(app.state.event_store.last_seq("s1"), 0)

    def test_oversized_single_event_is_never_sent_past_byte_bound(self) -> None:
        """A single oversized payload receives a typed close instead of an oversized batch."""
        with tempfile.TemporaryDirectory() as tmp:
            app = create_app(
                AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                    websocket_batch_byte_limit=1_024,
                    websocket_unacked_byte_limit=1_024,
                ),
                token="test-token",
            )
            session = app.state.session_store.get_or_create("s1", set())
            app.state.event_store.append(
                "s1",
                "final",
                {"text": "x" * 2_000},
                session_epoch=session.session_epoch,
            )
            with TestClient(app) as client:
                with client.websocket_connect(
                    "/chat/socket",
                    headers={"Authorization": "Bearer test-token"},
                ) as socket:
                    socket.send_json(
                        {
                            "type": "resume",
                            "protocol_version": 1,
                            "session_id": "s1",
                            "session_epoch": session.session_epoch,
                            "after_seq": 0,
                        }
                    )
                    self.assertEqual(socket.receive_json()["type"], "hello")
                    terminal = socket.receive_json()
            self.assertEqual(terminal["type"], "close")
            self.assertEqual(terminal["code"], "event_too_large")


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
            session = engine.store.get_or_create("s1", engine.available_tools)
            buffer = SubmissionScope(
                session=session,
                request_id="request-1",
                turn_id="turn-1",
                map_artifact_turn=StagedMapArtifactTurn(
                    session_id="s1",
                    turn_id="turn-1",
                    request_id="request-1",
                ),
            )
            seq = engine.publisher.emit(
                    "s1",
                    "agent_text_delta",
                    {
                        "frame_id": "frame-1",
                        "message_index": 2,
                        "text": "hello",
                        "append_delta": True,
                    },
                    buffer,
                )
            engine.publisher.emit(
                "s1", "tool_finished", {"tool_use_id": "tool-1"}, buffer
            )

            self.assertGreater(seq, 0)
            visible = engine.events.list_after("s1", 0)
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

            engine.publisher.flush(buffer)
            engine.publisher.resolve_previews(buffer, committed=True)
            types = [event.type for event in engine.events.list_after("s1", 0)]
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
            session = engine.store.get_or_create("s1", engine.available_tools)
            buffer = SubmissionScope(
                session=session,
                request_id="request-1",
                turn_id="turn-1",
                map_artifact_turn=StagedMapArtifactTurn(
                    session_id="s1",
                    turn_id="turn-1",
                    request_id="request-1",
                ),
            )
            engine.publisher.emit(
                    "s1",
                    "agent_reasoning_delta",
                    {
                        "frame_id": "frame-1",
                        "message_index": 2,
                        "text": "thinking",
                        "append_delta": True,
                    },
                    buffer,
                )
            engine.publisher.emit(
                "s1", "grant_created", {"grant_id": "secret"}, buffer
            )

            engine.publisher.resolve_previews(
                buffer,
                committed=False,
                reason="session_persistence_failed",
            )
            events = engine.events.list_after("s1", 0)
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
            engine = build_test_application(
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
            submission = asyncio.create_task(engine.execute(request))
            await asyncio.wait_for(provider.preview_emitted.wait(), timeout=5)

            self.assertFalse(submission.done())
            self.assertEqual(saves_after_submission_started, [])
            visible_before_commit = engine.events.list_after("s1", 0)
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
                event.type for event in engine.events.list_after("s1", 0)
            ]
            self.assertEqual(committed_types.count("agent_text_delta"), 1)
            self.assertEqual(committed_types[-1], "submission_preview_committed")
            self.assertIn("tool_results_received", committed_types)

    async def test_cancellation_discards_preview_without_staged_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PausingStreamProvider()
            engine, _store, request = _make_pending_tool_submission(tmp, provider)
            submission = asyncio.create_task(engine.execute(request))
            await asyncio.wait_for(provider.preview_emitted.wait(), timeout=5)

            submission.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await submission

            event_types = [event.type for event in engine.events.list_after("s1", 0)]
            self.assertEqual(
                event_types,
                ["agent_text_delta", "submission_preview_discarded"],
            )
            self.assertEqual(
                engine.events.list_after("s1", 0)[-1].payload["reason"],
                "cancelled",
            )

    async def test_session_save_failure_discards_preview_without_staged_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PausingStreamProvider()
            engine, store, request = _make_pending_tool_submission(tmp, provider)

            def failing_save(_candidate: Any) -> None:
                raise OSError("simulated persistence failure")

            store.save = failing_save  # type: ignore[method-assign]
            submission = asyncio.create_task(engine.execute(request))
            await asyncio.wait_for(provider.preview_emitted.wait(), timeout=5)
            provider.release.set()
            response = await asyncio.wait_for(submission, timeout=5)

            self.assertEqual(response.type, "error")
            events = engine.events.list_after("s1", 0)
            self.assertEqual(
                [event.type for event in events],
                ["agent_text_delta", "submission_preview_discarded"],
            )
            self.assertEqual(
                events[-1].payload["reason"],
                "session_persistence_failed",
            )
            rolled_back = store.get_or_create("s1", engine.available_tools)
            self.assertEqual(rolled_back.completed_turn_ledger, {})
            self.assertEqual(rolled_back.completed_response_hot_cache, {})

    async def test_submission_reducer_failure_discards_preview_without_staged_facts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = _UnusedProvider()
            engine, _store, request = _make_pending_tool_submission(tmp, provider)

            async def failing_submit(*_args: Any, **_kwargs: Any) -> Any:
                scope = _kwargs["publication_buffer"]
                engine.publisher.emit(
                    "s1",
                    "agent_text_delta",
                    {
                        "frame_id": "frame-1",
                        "message_index": 2,
                        "text": "preview",
                        "append_delta": True,
                    },
                    scope,
                )
                engine.publisher.emit(
                    "s1",
                    "grant_created",
                    {"grant_id": "must-not-leak"},
                    scope,
                )
                raise RuntimeError("simulated reducer failure")

            engine.coordinator._backend_recovery.execute = failing_submit  # type: ignore[method-assign]
            response = await engine.execute(request)

            self.assertEqual(response.type, "error")
            events = engine.events.list_after("s1", 0)
            self.assertEqual(
                [event.type for event in events],
                ["agent_text_delta", "submission_preview_discarded"],
            )
            self.assertEqual(events[-1].payload["reason"], "submission_failed")


if __name__ == "__main__":
    unittest.main()
