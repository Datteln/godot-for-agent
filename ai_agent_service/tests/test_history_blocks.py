from __future__ import annotations

import json
import unittest

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.events.store import Event
from app.query.engine import (
    _persisted_history_events,
    _structured_history_for_frame,
    _structured_session_history,
)
from app.sessions.store import Session, session_from_dict, session_to_dict


def _frame() -> Frame:
    return Frame(
        id="f1",
        agent=get_agent("coordinator", set()),
        messages=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "change jump height"},
            {
                "role": "assistant",
                "content": "Thought: Need to edit the player script",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "scripts/player.gd"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": json.dumps(
                    {"path": "scripts/player.gd", "content": "line one\nline two\n"}
                ),
            },
        ],
    )


class StructuredHistoryTests(unittest.TestCase):
    def test_delegated_frame_events_use_parent_timeline_anchor(self) -> None:
        frame = Frame(
            id="f1",
            agent=get_agent("coordinator", set()),
            messages=[
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "inspect project"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "delegate-1",
                            "function": {
                                "name": "delegate_many",
                                "arguments": json.dumps({"tasks": []}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "delegate-1",
                    "content": json.dumps(
                        {
                            "results": [
                                {
                                    "agent": "programming-agent",
                                    "summary": "child summary",
                                }
                            ]
                        }
                    ),
                },
                {"role": "assistant", "content": "final answer"},
            ],
        )
        events = [
            Event(
                seq=1,
                session_id="s1",
                type="agent_reasoning_delta",
                payload={
                    "frame_id": "f2",
                    "timeline_frame_id": "f1",
                    "timeline_message_index": 3,
                    "message_index": 2,
                    "loop": 1,
                    "text": "child reasoning",
                    "elapsed_ms": 1200,
                },
            ),
            Event(
                seq=2,
                session_id="s1",
                type="server_tool_result",
                payload={
                    "frame_id": "f2",
                    "agent": "programming-agent",
                    "timeline_frame_id": "f1",
                    "timeline_message_index": 3,
                    "tool": "read_file",
                    "result_summary": {
                        "kind": "read",
                        "path": "scripts/player.gd",
                        "line_start": 1,
                        "line_end": 41,
                    },
                },
            ),
        ]

        blocks = _structured_session_history([frame], events)

        self.assertEqual(
            [block.type for block in blocks],
            ["user", "thought", "log_read", "delegate_results", "log_text"],
        )

    def test_history_events_survive_session_round_trip(self) -> None:
        session = Session(session_id="s1", agent_stack=[_frame()])
        session.record_history_event(
            "agent_reasoning_delta",
            {
                "frame_id": "f1",
                "message_index": 2,
                "loop": 1,
                "text": "partial",
                "elapsed_ms": 800,
            },
        )
        session.record_history_event(
            "agent_reasoning_delta",
            {
                "frame_id": "f1",
                "message_index": 2,
                "loop": 1,
                "text": "complete reasoning",
                "elapsed_ms": 2300,
            },
        )

        restored = session_from_dict(session_to_dict(session), set())
        events = _persisted_history_events(restored)
        blocks = _structured_history_for_frame(restored.agent_stack[0], events)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["text"], "complete reasoning")
        self.assertEqual(blocks[1].type, "thought")
        self.assertEqual(blocks[1].model_dump()["header"], "Thought for 2.30s")

    def test_reasoning_is_interleaved_before_tool_result(self) -> None:
        events = [
            Event(
                seq=1,
                session_id="s1",
                type="agent_reasoning_delta",
                payload={
                    "frame_id": "f1",
                    "message_index": 2,
                    "loop": 1,
                    "text": "I should inspect the script first.",
                    "elapsed_ms": 2300,
                },
            ),
            Event(
                seq=2,
                session_id="s1",
                type="agent_text_delta",
                payload={
                    "frame_id": "f1",
                    "message_index": 2,
                    "loop": 1,
                    "text": "Thought: Need to edit the player script",
                },
            ),
        ]

        blocks = _structured_history_for_frame(_frame(), events)

        self.assertEqual(
            [block.type for block in blocks],
            ["user", "thought", "log_read"],
        )
        self.assertEqual(blocks[1].model_dump()["header"], "Thought for 2.30s")
        self.assertEqual(blocks[2].model_dump()["line_end"], 2)

    def test_persisted_thought_keeps_summary_without_swallowing_final_body(self) -> None:
        frame = _frame()
        frame.messages[2]["content"] = "Thought: Need to edit\n\nThe edit is complete."

        blocks = _structured_history_for_frame(frame, [])

        self.assertEqual(
            [block.type for block in blocks],
            ["user", "thought", "log_text", "log_text", "log_read"],
        )
        self.assertEqual(blocks[2].model_dump()["text"], "Need to edit")
        self.assertEqual(blocks[3].model_dump()["text"], "The edit is complete.")

    def test_streamed_thought_prefix_keeps_summary_before_final_body(self) -> None:
        events = [
            Event(
                seq=1,
                session_id="s1",
                type="agent_reasoning_delta",
                payload={
                    "frame_id": "f1",
                    "message_index": 2,
                    "loop": 1,
                    "text": "Reasoning detail",
                    "elapsed_ms": 1800,
                },
            ),
            Event(
                seq=2,
                session_id="s1",
                type="agent_text_delta",
                payload={
                    "frame_id": "f1",
                    "message_index": 2,
                    "loop": 1,
                    "text": "Thought: Edit is complete\n\nFinal answer body",
                },
            ),
        ]
        frame = _frame()
        frame.messages[2]["content"] = "Thought: Edit is complete\n\nFinal answer body"

        blocks = _structured_history_for_frame(frame, events)

        self.assertEqual(
            [block.type for block in blocks],
            ["user", "thought", "log_text", "log_read"],
        )
        self.assertEqual(blocks[2].model_dump()["text"], "Final answer body")

    def test_late_reasoning_delta_is_ignored_after_final_text(self) -> None:
        frame = _frame()
        frame.messages[2]["content"] = "Thought: Summary\n\nFinal answer body"
        events = [
            Event(
                seq=10,
                session_id="s1",
                type="agent_reasoning_delta",
                payload={
                    "frame_id": "f1",
                    "message_index": 2,
                    "loop": 1,
                    "text": "partial reasoning",
                    "elapsed_ms": 9000,
                },
            ),
            Event(
                seq=20,
                session_id="s1",
                type="agent_text_delta",
                payload={
                    "frame_id": "f1",
                    "message_index": 2,
                    "loop": 1,
                    "text": "Thought: Summary\n\nFinal answer body",
                },
            ),
            Event(
                seq=21,
                session_id="s1",
                type="agent_reasoning_delta",
                payload={
                    "frame_id": "f1",
                    "message_index": 2,
                    "loop": 1,
                    "text": "complete reasoning",
                    "elapsed_ms": 28000,
                },
            ),
        ]

        blocks = _structured_history_for_frame(frame, events)

        self.assertEqual(
            [block.type for block in blocks],
            ["user", "thought", "log_text", "log_read"],
        )
        self.assertEqual(blocks[1].model_dump()["header"], "Thought for 9.00s")
        self.assertEqual(blocks[1].model_dump()["detail"], "partial reasoning")
        self.assertEqual(blocks[2].model_dump()["text"], "Final answer body")


if __name__ == "__main__":
    unittest.main()
