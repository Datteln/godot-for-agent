"""provider 流式 reasoning 段结束信号测试（agent_reasoning_complete）。"""

from __future__ import annotations

import unittest

from app.orchestrator.turn.streaming import make_delta_callback
from app.orchestrator.map_turn.events import _delta_callback


class ReasoningBoundaryCallbackTests(unittest.TestCase):
    def test_reasoning_done_emitted_after_reasoning_then_content(self) -> None:
        events: list[tuple[str, dict]] = []

        def collect(kind: str, payload: dict) -> None:
            events.append((kind, payload))

        callback = make_delta_callback(
            collect,
            frame_id="frame-1",
            loop=1,
            message_index=2,
            timeline_frame_id="frame-1",
            timeline_message_index=2,
        )
        self.assertIsNotNone(callback)
        assert callback is not None

        callback("reasoning", "第一步思考", None)
        callback("reasoning", "继续推理", 25)
        callback("reasoning_done", "", None)
        callback("content", "正文回答", None)

        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[-2], "agent_reasoning_complete")
        self.assertEqual(kinds[-1], "agent_text_delta")
        complete_payload = dict(events[-2][1])
        self.assertEqual(complete_payload["frame_id"], "frame-1")
        self.assertGreater(complete_payload["elapsed_ms"], 0)
        self.assertEqual(complete_payload["message_index"], 2)
        # 与 delta 发布层注入的 message_id 同构，前端才能解析到同一 item
        self.assertEqual(complete_payload["message_id"], "frame-1:2")
        self.assertNotIn("text", complete_payload)

    def test_reasoning_only_turn_emits_complete_before_decoration(self) -> None:
        events: list[tuple[str, dict]] = []

        def collect(kind: str, payload: dict) -> None:
            events.append((kind, payload))

        callback = make_delta_callback(
            collect,
            frame_id="f2",
            loop=2,
            message_index=0,
            timeline_frame_id="f2",
            timeline_message_index=0,
        )
        assert callback is not None

        callback("reasoning", "纯推理段", None)
        callback("reasoning_done", "", None)

        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[-1], "agent_reasoning_complete")
        self.assertEqual(kinds.count("agent_reasoning_complete"), 1)


class MapDeltaCallbackBoundaryTests(unittest.TestCase):
    def test_map_delta_callback_handles_reasoning_done(self) -> None:
        """map 专用回调（map_turn/events.py）同样转发 reasoning_done 完成信号。"""
        events: list[tuple[str, dict]] = []

        def collect(kind: str, payload: dict) -> None:
            events.append((kind, payload))

        callback = _delta_callback(
            collect,
            frame_id="map-f1",
            loop=3,
            message_index=1,
            timeline_frame_id="map-f1",
            timeline_message_index=1,
        )
        self.assertIsNotNone(callback)
        assert callback is not None

        callback("reasoning", "地图思考", None)
        callback("reasoning", "继续", 12)
        callback("reasoning_done", "", None)
        callback("content", "正文", None)

        kinds = [kind for kind, _ in events]
        self.assertEqual(kinds[-2], "agent_reasoning_complete")
        complete_payload = dict(events[-2][1])
        self.assertGreater(complete_payload["elapsed_ms"], 0)
        self.assertEqual(complete_payload["frame_id"], "map-f1")
        self.assertEqual(kinds[-1], "agent_text_delta")
        self.assertFalse(
            any(kind == "agent_reasoning_delta" and payload.get("text") == "" for kind, payload in events)
        )


if __name__ == "__main__":
    unittest.main()