"""Application-level partial-stream no-regeneration regression test."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.api.schemas import ChatRequest
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import LLMProvider, PartialStreamInterrupted
from app.sessions.store import SessionStore
from tests.application_test_support import build_test_application


class _PartialProvider(LLMProvider):
    """Emit accepted text once, then interrupt the same wire completion."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *_args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        on_delta = kwargs.get("on_delta")
        assert on_delta is not None
        on_delta("text", "accepted-prefix", 1)
        raise PartialStreamInterrupted(
            accepted_kinds=frozenset({"text"}),
            wire_attempt_count=1,
            model="primary",
        )


class PartialStreamIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the provider interruption through the atomic application use case."""

    async def test_partial_stream_never_regenerates_or_publishes_tool_effects(self) -> None:
        """A partial completion becomes one typed error; the accepted prefix is retained and marked."""
        with tempfile.TemporaryDirectory() as tmp:
            provider = _PartialProvider()
            events = EventStore()
            application = build_test_application(
                settings=AppSettings(
                    project_root=Path(tmp),
                    rag_auto_build_enabled=False,
                    llm_base_url="http://localhost",
                ),
                session_store=SessionStore(Path(tmp) / "sessions"),
                llm=provider,
                event_store=events,
            )
            response = await application.execute(
                ChatRequest(
                    session_id="s1",
                    request_id="request-1",
                    user_message="explain the scene",
                )
            )

            self.assertEqual(provider.calls, 1)
            self.assertEqual(response.type, "error")
            self.assertEqual(response.error_code, "partial_stream_interrupted")
            published = events.list_after("s1", 0)
            previews = [item for item in published if item.type == "agent_text_delta"]
            self.assertEqual(len(previews), 1)
            self.assertEqual(previews[0].payload["text"], "accepted-prefix")
            self.assertIs(previews[0].payload["provisional"], True)
            # agent 层错误（会话已提交）保留 preview 并标记失败原因，而不是 discard
            self.assertEqual(
                [item.type for item in published].count("submission_preview_committed"),
                1,
            )
            boundary = published[-1]
            self.assertEqual(boundary.payload["reason"], "partial_stream_interrupted")
            self.assertFalse(
                any(
                    item.type in {"tool_started", "tool_finished", "tool_calls"}
                    for item in published
                )
            )
