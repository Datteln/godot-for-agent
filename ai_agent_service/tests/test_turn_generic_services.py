"""Tests for generic turn-core services extracted from map_turn_pipeline.

Task 7.4 requires that generic model/effort/thinking/tool visibility,
cache, permission, protocol parsing, concurrent/sequential tool execution,
and event behavior live behind turn-core services rather than embedded
in a domain pipeline.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

from app.orchestrator.turn.event_projection import (
    emit_event,
    match_items_for_event,
    result_count_for_event,
    result_summary_for_event,
    tool_args_for_event,
)
from app.orchestrator.turn.streaming import (
    estimate_stream_token_count,
    make_delta_callback,
    make_fallback_callback,
)
from app.orchestrator.turn.tool_protocol import (
    PendingServerCall,
    PendingToolMessage,
    load_tool_args,
    tool_message,
)


class StreamingCallbackTests(unittest.TestCase):
    """Generic streaming delta and fallback callbacks."""

    def test_estimate_stream_token_count_empty(self) -> None:
        self.assertEqual(estimate_stream_token_count(""), 0)

    def test_estimate_stream_token_count_ascii(self) -> None:
        count = estimate_stream_token_count("hello world")
        self.assertGreater(count, 0)

    def test_estimate_stream_token_count_cjk(self) -> None:
        count = estimate_stream_token_count("你好世界")
        self.assertGreater(count, 0)

    def test_delta_callback_emits_text_delta(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        cb = make_delta_callback(
            lambda et, p: events.append((et, p)),
            frame_id="f1",
            loop=1,
            message_index=5,
            timeline_frame_id="f1",
            timeline_message_index=5,
        )
        assert cb is not None
        cb("content", "hello", None)
        self.assertEqual(events[0][0], "agent_text_delta")
        self.assertEqual(events[0][1]["text"], "hello")
        self.assertTrue(events[0][1]["provider_first_chunk"])

    def test_delta_callback_emits_reasoning_delta(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        cb = make_delta_callback(
            lambda et, p: events.append((et, p)),
            frame_id="f1",
            loop=1,
            message_index=5,
            timeline_frame_id="f1",
            timeline_message_index=5,
        )
        assert cb is not None
        cb("reasoning", "thinking...", 42)
        self.assertEqual(events[0][0], "agent_reasoning_delta")
        self.assertEqual(events[0][1]["token_count"], 42)

    def test_delta_callback_none_when_no_event_callback(self) -> None:
        cb = make_delta_callback(None, "f1", 1, 0, "f1", 0)
        self.assertIsNone(cb)

    def test_fallback_callback_emits_event(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        cb = make_fallback_callback(
            lambda et, p: events.append((et, p)),
            frame_id="f1",
            loop=1,
        )
        assert cb is not None
        cb("gpt-4", "gpt-3.5")
        self.assertEqual(events[0][0], "agent_model_fallback")
        self.assertEqual(events[0][1]["primary_model"], "gpt-4")
        self.assertEqual(events[0][1]["fallback_model"], "gpt-3.5")

    def test_fallback_callback_none_when_no_event_callback(self) -> None:
        cb = make_fallback_callback(None, "f1", 1)
        self.assertIsNone(cb)


class EventProjectionTests(unittest.TestCase):
    """Generic event-projection helpers."""

    def test_emit_event_with_callback(self) -> None:
        events: list[tuple[str, dict[str, Any]]] = []
        emit_event(lambda et, p: events.append((et, p)), "test", {"a": 1})
        self.assertEqual(events, [("test", {"a": 1})])

    def test_emit_event_without_callback(self) -> None:
        emit_event(None, "test", {"a": 1})  # should not raise

    def test_tool_args_for_event_truncates_long_strings(self) -> None:
        args = {"path": "x" * 300}
        result = tool_args_for_event(args)
        self.assertTrue(result["path"].endswith("..."))
        self.assertEqual(len(result["path"]), 183)

    def test_tool_args_for_event_selects_known_keys(self) -> None:
        args = {"path": "/a/b", "secret": "key", "command": "ls"}
        result = tool_args_for_event(args)
        self.assertIn("path", result)
        self.assertIn("command", result)
        self.assertNotIn("secret", result)

    def test_result_count_for_event_matches_key(self) -> None:
        result = {"matches": [{"a": 1}, {"b": 2}]}
        self.assertEqual(result_count_for_event(result, False), 2)

    def test_result_count_for_event_error_returns_none(self) -> None:
        self.assertIsNone(result_count_for_event({"error": "x"}, True))

    def test_result_summary_for_event_read_file(self) -> None:
        result = {
            "content": "line1\nline2",
            "path": "/a/b.gd",
            "offset": 1,
            "truncated": False,
        }
        summary = result_summary_for_event("read_file", result, False)
        assert summary is not None
        self.assertEqual(summary["kind"], "read")
        self.assertEqual(summary["path"], "/a/b.gd")
        self.assertEqual(summary["line_start"], 1)
        self.assertEqual(summary["line_end"], 2)

    def test_result_summary_for_event_grep(self) -> None:
        result = {
            "pattern": "foo",
            "matches": [{"path": "/a", "line": 1, "text": "foo"}],
        }
        summary = result_summary_for_event("grep_code", result, False)
        assert summary is not None
        self.assertEqual(summary["kind"], "grep")
        self.assertEqual(summary["match_count"], 1)

    def test_match_items_for_event_normalizes_rows(self) -> None:
        result = {"matches": [{"path": "/a", "line": 1, "text": "x"}]}
        items = match_items_for_event(result)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["path"], "/a")


class ToolProtocolTests(unittest.TestCase):
    """Generic tool-call classification and parsing."""

    def test_tool_message_string_result(self) -> None:
        msg = tool_message("call-1", "done", is_error=False)
        self.assertEqual(msg["role"], "tool")
        self.assertEqual(msg["tool_call_id"], "call-1")
        self.assertEqual(msg["content"], "done")

    def test_tool_message_dict_result(self) -> None:
        msg = tool_message("call-1", {"key": "val"}, is_error=False)
        parsed = json.loads(msg["content"])
        self.assertEqual(parsed, {"key": "val"})

    def test_tool_message_error(self) -> None:
        msg = tool_message("call-1", "bad args", is_error=True)
        self.assertEqual(msg["content"], "bad args")

    def test_load_tool_args_valid_json(self) -> None:
        args, err = load_tool_args("c1", '{"path": "/a"}')
        self.assertIsNotNone(args)
        self.assertIsNone(err)
        self.assertEqual(args["path"], "/a")

    def test_load_tool_args_invalid_json(self) -> None:
        args, err = load_tool_args("c1", "not json")
        self.assertIsNone(args)
        self.assertIsNotNone(err)
        self.assertEqual(err["role"], "tool")
        self.assertIn("JSON", err["content"])

    def test_load_tool_args_non_object(self) -> None:
        args, err = load_tool_args("c1", "[1, 2]")
        self.assertIsNone(args)
        self.assertIsNotNone(err)

    def test_load_tool_args_empty_string(self) -> None:
        args, err = load_tool_args("c1", "")
        self.assertIsNotNone(args)
        self.assertIsNone(err)

    def test_pending_tool_message_is_frozen(self) -> None:
        msg = PendingToolMessage(message={"role": "tool"})
        with self.assertRaises(Exception):
            msg.message = {}  # type: ignore

    def test_pending_server_call_is_frozen(self) -> None:
        from app.tools.registry import REGISTRY

        # Use a real registered tool if available, else skip the mutation test
        tool = next(iter(REGISTRY.values())) if REGISTRY else None
        if tool is None:
            self.skipTest("no registered tools")
        call = PendingServerCall(call_id="c1", tool=tool, args={})
        with self.assertRaises(Exception):
            call.call_id = "c2"  # type: ignore


if __name__ == "__main__":
    unittest.main()
