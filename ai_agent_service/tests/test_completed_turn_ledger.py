"""完成工具回合持久身份、重建与隔离的回归测试。"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.api.schemas import ChatRequest, ToolResult
from app.application.completed_turns import (
    CompletedTurnConflictError,
    CompletedTurnIntegrityError,
    CompletedTurnLedger,
)
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMProvider
from app.sessions.store import Session, SessionStore
from app.tools.front_tools import register_front_tools
from tests.application_test_support import build_test_application


class _CountingFinalProvider(LLMProvider):
    """记录模型调用次数并返回确定性完成响应。"""

    def __init__(self) -> None:
        """初始化零次调用计数。"""
        self.calls = 0

    @property
    def supports_tool_calling(self) -> bool:
        """声明测试 provider 支持工具调用。"""
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        """声明测试 provider 不支持提示缓存。"""
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> AssistantTurn:
        """增加调用计数并返回固定最终消息。"""
        self.calls += 1
        return AssistantTurn(
            raw_message={"role": "assistant", "content": "committed once"},
            content="committed once",
            model="test",
        )


class CompletedTurnLedgerTests(unittest.TestCase):
    """验证紧凑账本始终独立于有界完整响应缓存。"""

    def test_evicted_response_reconstructs_after_restart(self) -> None:
        """热缓存淘汰并重启后仍从持久定位文件还原原响应。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = CompletedTurnLedger(root, hot_cache_size=1)
            session_store = SessionStore(root / "sessions", project_root=root)
            session = session_store.get_or_create("s1", set())
            ledger.record(
                session,
                turn_id="turn-1",
                fingerprint="fingerprint-1",
                response={"type": "final", "text": "first"},
            )
            ledger.record(
                session,
                turn_id="turn-2",
                fingerprint="fingerprint-2",
                response={"type": "final", "text": "second"},
            )

            self.assertEqual(set(session.completed_turn_ledger), {"turn-1", "turn-2"})
            self.assertNotIn("turn-1", session.completed_response_hot_cache)
            session.completed_response_hot_cache.clear()
            session_store.save(session)
            restarted = SessionStore(root / "sessions", project_root=root).get_or_create(
                "s1", set()
            )

            resolution = ledger.resolve(
                restarted,
                turn_id="turn-1",
                fingerprint="fingerprint-1",
            )

            self.assertIsNotNone(resolution)
            assert resolution is not None
            self.assertEqual(resolution.source, "durable_locator")
            self.assertEqual(resolution.response, {"type": "final", "text": "first"})
            self.assertIn("turn-1", restarted.completed_response_hot_cache)

    def test_conflict_preserves_original_identity_and_response(self) -> None:
        """同一 turn 的不同指纹被拒绝且不改写首次提交。"""
        with tempfile.TemporaryDirectory() as tmp:
            ledger = CompletedTurnLedger(Path(tmp), hot_cache_size=2)
            session = Session(session_id="s1", session_epoch="epoch-1")
            ledger.record(
                session,
                turn_id="turn-1",
                fingerprint="original",
                response={"type": "final", "text": "original"},
            )
            original = dict(session.completed_turn_ledger["turn-1"])

            with self.assertRaises(CompletedTurnConflictError):
                ledger.record(
                    session,
                    turn_id="turn-1",
                    fingerprint="different",
                    response={"type": "final", "text": "replacement"},
                )

            self.assertEqual(session.completed_turn_ledger["turn-1"], original)
            self.assertEqual(
                session.completed_response_hot_cache["turn-1"]["text"],
                "original",
            )

    def test_corrupt_locator_fails_closed(self) -> None:
        """损坏的持久定位文件返回完整性错误而非重新执行。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = CompletedTurnLedger(root, hot_cache_size=1)
            session = Session(session_id="s1", session_epoch="epoch-1")
            entry = ledger.record(
                session,
                turn_id="turn-1",
                fingerprint="fingerprint-1",
                response={"type": "final", "text": "first"},
            )
            session.completed_response_hot_cache.clear()
            locator = root / ".ai_agent_service" / "completed_turns" / str(
                entry["response_locator"]
            )
            locator.write_text("{broken", encoding="utf-8")

            with self.assertRaises(CompletedTurnIntegrityError):
                ledger.resolve(
                    session,
                    turn_id="turn-1",
                    fingerprint="fingerprint-1",
                )

    def test_working_copy_publication_and_reset_are_isolated(self) -> None:
        """身份仅随工作副本发布，并且新 epoch 从空账本开始。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session_dir = root / "sessions"
            store = SessionStore(session_dir, project_root=root)
            live = store.get_or_create("s1", set())
            working = copy.deepcopy(live)
            ledger = CompletedTurnLedger(root, hot_cache_size=2)
            ledger.record(
                working,
                turn_id="turn-1",
                fingerprint="fingerprint-1",
                response={"type": "final", "text": "committed"},
            )

            self.assertEqual(live.completed_turn_ledger, {})
            store.save(working)
            restarted = SessionStore(session_dir, project_root=root).get_or_create("s1", set())
            self.assertIn("turn-1", restarted.completed_turn_ledger)

            reset_epoch = "epoch-after-reset"
            fresh = Session(session_id="s1", session_epoch=reset_epoch)
            self.assertEqual(fresh.completed_turn_ledger, {})
            self.assertEqual(fresh.completed_response_hot_cache, {})


class CompletedTurnSubmissionTests(unittest.IsolatedAsyncioTestCase):
    """验证 AgentApplication 在幂等命中前不会重复模型或会话副作用。"""

    async def test_identical_retry_returns_original_without_side_effects(self) -> None:
        """更换 request_id 的相同结果批次直接返回首次响应。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            register_front_tools()
            provider = _CountingFinalProvider()
            store = SessionStore(root / "sessions", project_root=root)
            engine = build_test_application(
                settings=AppSettings(
                    llm_base_url="http://localhost",
                    project_root=root,
                    rag_auto_build_enabled=False,
                    completed_response_hot_cache_size=1,
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
            result = ToolResult(
                tool_use_id="tool-1",
                frame_id="frame-1",
                turn_id="turn-1",
                status="applied",
                result={"name": "Root", "children": []},
            )
            first = await engine.execute(
                ChatRequest(session_id="s1", request_id="request-1", tool_results=[result])
            )
            committed = store.get_or_create("s1", engine.available_tools)
            history_after_first = list(committed.history_events)
            messages_after_first = [list(frame.messages) for frame in committed.agent_stack]

            second = await engine.execute(
                ChatRequest(session_id="s1", request_id="request-2", tool_results=[result])
            )

            self.assertEqual(first.model_dump(), second.model_dump())
            self.assertEqual(provider.calls, 1)
            self.assertEqual(committed.history_events, history_after_first)
            self.assertEqual(
                [frame.messages for frame in committed.agent_stack],
                messages_after_first,
            )
            self.assertIn("turn-1", committed.completed_turn_ledger)


if __name__ == "__main__":
    unittest.main()
