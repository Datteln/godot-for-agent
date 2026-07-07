"""运行时鲁棒性回归测试（见 `问题发现.md` 的 P0/P1）。

覆盖：
- 原子写入：写入过程中崩溃也不会留下半截 JSON。
- 会话/记忆持久化字段类型校验：合法 JSON 但字段为 null/错误类型时不再打穿。
- 会话文件名碰撞：不同 id 不再共用文件；非法 id 被拒绝。
- reset-vs-chat 竞态：reset 会取消仍在运行的 turn，且会话不再"复活"。
- 恢复指针按会话清理：B 会话 final 不会误删 A 会话的 pending 指针。
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.config import AppSettings
from app.agents.types import AgentDefinition, Frame
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMProvider
from app.memory.store import MemoryStore
from app.orchestrator.agent import _requires_create_plan_before_map_delegate
from app.query.engine import QueryEngine, _schedule_map_completion_continuation
from app.recovery.pointer import RecoveryPointerStore
from app.sessions.store import Session, SessionStore, _safe_filename, session_from_dict
from app.storage.atomic import atomic_write_json


class AtomicWriteTests(unittest.TestCase):
    def test_keeps_old_file_when_serialization_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            atomic_write_json(path, {"v": 1})
            # 序列化阶段抛错（不可序列化对象）必须发生在替换目标文件之前。
            with self.assertRaises(TypeError):
                atomic_write_json(path, {"v": object()})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 1})
            # 没有遗留临时文件。
            leftovers = [p for p in Path(tmp).iterdir() if p.name != "data.json"]
            self.assertEqual(leftovers, [])

    def test_overwrites_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "data.json"
            atomic_write_json(path, {"v": 1})
            atomic_write_json(path, {"v": 2})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"v": 2})


class PersistenceValidationTests(unittest.TestCase):
    def test_memory_items_null_does_not_crash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memory.json"
            path.write_text(json.dumps({"items": None}), encoding="utf-8")
            store = MemoryStore(path)
            self.assertEqual(store.list(), [])  # 旧实现这里抛 TypeError
            item = store.save("hello")
            self.assertEqual([m.id for m in store.list()], [item.id])

    def test_session_null_fields_do_not_crash(self) -> None:
        broken = {
            "session_id": "broken",
            "agent_stack": None,
            "pending_tool_call_ids": None,
            "pending_verify_candidates": None,
            "request_id_cache": None,
            "turn_counter": None,
        }
        session = session_from_dict(broken, set())
        self.assertEqual(session.session_id, "broken")
        self.assertEqual(session.agent_stack, [])
        self.assertEqual(session.pending_tool_call_ids, set())
        self.assertEqual(session.pending_verify_candidates, [])
        self.assertEqual(session.request_id_cache, {})
        self.assertEqual(session.turn_counter, 0)

    def test_session_non_object_rejected(self) -> None:
        with self.assertRaises(ValueError):
            session_from_dict([], set())  # type: ignore[arg-type]


class SessionFilenameTests(unittest.TestCase):
    def test_distinct_ids_do_not_collide(self) -> None:
        # 旧实现会把这两个 id 清洗成同一个 "abc"。
        self.assertNotEqual(_safe_filename("a-bc"), _safe_filename("ab-c"))

    def test_invalid_ids_rejected(self) -> None:
        for bad in ["a/bc", "", "../escape", "a b"]:
            with self.assertRaises(ValueError):
                _safe_filename(bad)

    def test_store_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            store.save(Session(session_id="abc-123"))
            restored = SessionStore(Path(tmp)).get_or_create("abc-123", set())
            self.assertEqual(restored.session_id, "abc-123")


class MapCompletionContinuationTests(unittest.TestCase):
    def test_blocked_final_is_converted_to_continuation_prompt(self) -> None:
        agent = AgentDefinition(
            name="map-agent",
            source="bundled",
            description="",
            prompt="",
        )
        session = Session(
            session_id="map-session",
            agent_stack=[
                Frame(
                    id="f1",
                    agent=agent,
                    messages=[
                        {"role": "system", "content": "system"},
                        {"role": "assistant", "content": "done"},
                    ],
                )
            ],
            map_completion_blockers=[
                {
                    "tool": "validate_map_region",
                    "reason": "blocking_completion",
                    "issues": ["platformer design has overly tall solid columns"],
                    "target": "TileMap",
                    "required_revision": 82,
                }
            ],
        )

        self.assertTrue(_schedule_map_completion_continuation(session))
        frame = session.top_frame()
        assert frame is not None
        self.assertEqual([message["role"] for message in frame.messages], ["system", "user"])
        self.assertIn("MAP_COMPLETION_GATE_BLOCKED", frame.messages[-1]["content"])
        self.assertIn("overly tall solid columns", frame.messages[-1]["content"])
        self.assertIn("Do not summarize or answer final yet", frame.messages[-1]["content"])


class MapPlanningProtocolTests(unittest.TestCase):
    def test_complex_map_delegate_requires_visible_plan_first(self) -> None:
        agent = AgentDefinition(
            name="coordinator",
            source="bundled",
            description="",
            prompt="",
        )
        frame = Frame(id="f1", agent=agent, messages=[])
        session = Session(session_id="map-session", agent_stack=[frame])
        args = {
            "agent": "map-agent",
            "task": (
                "请将当前关卡向右扩展约 40 格，设计一条可通关路线，"
                "包含阶梯、悬浮平台、两个陷阱坑、5 枚金币、树和终点区域。"
            ),
        }

        self.assertTrue(
            _requires_create_plan_before_map_delegate(session, frame, "delegate", args)
        )

    def test_existing_plan_allows_map_delegate(self) -> None:
        agent = AgentDefinition(
            name="coordinator",
            source="bundled",
            description="",
            prompt="",
        )
        frame = Frame(id="f1", agent=agent, messages=[])
        session = Session(
            session_id="map-session",
            agent_stack=[frame],
            pending_plan={"summary": "plan", "steps": [], "next_step_index": 0},
        )
        args = {
            "agent": "map-agent",
            "task": "扩展关卡并规划可通关路线，放置金币和终点区域。",
        }

        self.assertFalse(
            _requires_create_plan_before_map_delegate(session, frame, "delegate", args)
        )


class _BlockingLLMProvider(LLMProvider):
    """在 `chat` 里阻塞，直到测试显式放行，用于构造 reset-vs-chat 竞态。"""

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
        self.entered.set()
        await self.release.wait()
        return AssistantTurn(raw_message={"role": "assistant", "content": "done"}, content="done")


class ResetRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_cancels_active_turn_and_session_stays_gone(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = AppSettings(llm_base_url="http://localhost", project_root=Path(tmp))
            store = SessionStore(Path(tmp) / "sessions")
            llm = _BlockingLLMProvider()
            engine = QueryEngine(
                settings=settings,
                session_store=store,
                llm=llm,
                event_store=EventStore(),
            )
            from app.api.schemas import ChatRequest

            chat_task = asyncio.create_task(
                engine.submit_user_turn(
                    ChatRequest(session_id="s1", user_message="hi", request_id="r1")
                )
            )
            await asyncio.wait_for(llm.entered.wait(), timeout=5)

            # reset 必须取消正在 await LLM 的 turn，并在持锁状态下清空会话。
            await engine.reset("s1")

            with self.assertRaises(asyncio.CancelledError):
                await chat_task

            # 放行 LLM（即便旧任务还能往下走，也已被取消）。
            llm.release.set()
            await asyncio.sleep(0)

            # 会话文件不应"复活"。
            session_path = (Path(tmp) / "sessions")
            files = list(session_path.glob("*.json")) if session_path.exists() else []
            self.assertEqual(files, [])


class RecoveryPointerScopeTests(unittest.TestCase):
    def test_clear_keeps_other_sessions_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = RecoveryPointerStore(Path(tmp) / "pointer.json", Path(tmp))
            store.write(session_id="A", pending_turn_id="t1", last_event_seq=5)
            # B 会话 final 清理不应抹掉 A 的指针。
            store.clear("B")
            pointer = store.read()
            self.assertIsNotNone(pointer)
            assert pointer is not None
            self.assertEqual(pointer.session_id, "A")
            # A 自己 final 时清理才生效。
            store.clear("A")
            self.assertIsNone(store.read())


class FrontendExternalTypingTests(unittest.TestCase):
    """前端从 HTTP 响应/事件取数组/字典前必须判型，避免强类型赋值崩溃。"""

    def _chat_panel_source(self) -> str:
        repo = Path(__file__).resolve().parents[2]
        return (
            repo / "ai_agent_frontend/addons/ai_agent/ui/chat_panel.gd"
        ).read_text(encoding="utf-8")

    def test_no_unguarded_typed_external_assignment(self) -> None:
        import re as _re

        source = self._chat_panel_source()
        # 形如 `var x: Array = response.get("calls", [])`（且不带 `is Array/Dictionary`
        # 判型）的直接赋值会在字段为 null/错误类型时运行时崩溃。
        pattern = _re.compile(
            r"var\s+\w+:\s+(?:Array|Dictionary)[^=\n]*=\s+"
            r"(?:response|event|payload|parsed|data|pointer)\w*\.get\([^\n]*"
        )
        offenders = [
            line.strip()
            for line in source.splitlines()
            if pattern.search(line) and " is Array else" not in line and " is Dictionary else" not in line
        ]
        self.assertEqual(offenders, [], f"unguarded external typed assignment: {offenders}")

    def test_tool_calls_guarded(self) -> None:
        source = self._chat_panel_source()
        self.assertIn("var calls: Array = raw_calls if raw_calls is Array else []", source)


class ReadFilePaginationTests(unittest.IsolatedAsyncioTestCase):
    """`read_file` 按行分页（offset/limit），类似 Claude Code Read 工具的语义。"""

    class _Ctx:
        def __init__(self, root: Path) -> None:
            from app.security.settings import SecuritySettings

            self.security = SecuritySettings(project_root=root)
            self.session_id = "s1"

    async def test_default_reads_from_start_with_limit(self) -> None:
        from app.tools.server_tools.read_file import read_file_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [f"line{i}" for i in range(1, 11)]
            (root / "f.txt").write_text("\n".join(lines), encoding="utf-8")
            result = await read_file_handler({"path": "f.txt", "limit": 3}, self._Ctx(root))
            self.assertEqual(result["content"], "line1\nline2\nline3")
            self.assertEqual(result["offset"], 1)
            self.assertTrue(result["has_more"])
            self.assertTrue(result["truncated"])

    async def test_offset_continues_from_requested_line(self) -> None:
        from app.tools.server_tools.read_file import read_file_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = [f"line{i}" for i in range(1, 11)]
            (root / "f.txt").write_text("\n".join(lines), encoding="utf-8")
            result = await read_file_handler({"path": "f.txt", "offset": 8, "limit": 5}, self._Ctx(root))
            self.assertEqual(result["content"], "line8\nline9\nline10")
            self.assertFalse(result["has_more"])
            self.assertFalse(result["truncated"])

    async def test_reading_past_end_returns_empty_without_error(self) -> None:
        from app.tools.server_tools.read_file import read_file_handler

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "f.txt").write_text("only-line", encoding="utf-8")
            result = await read_file_handler({"path": "f.txt", "offset": 50}, self._Ctx(root))
            self.assertEqual(result["content"], "")
            self.assertFalse(result["has_more"])


class ApplyTextEditToolRegistrationTests(unittest.TestCase):
    """`apply_text_edit` 必须作为真正的局部编辑工具注册，而不是 write_file 的死别名。"""

    def test_registered_as_front_tool_with_old_new_string_params(self) -> None:
        from app.tools.front_tools import register_front_tools
        from app.tools.registry import REGISTRY

        if "apply_text_edit" not in REGISTRY:
            register_front_tools()
        tool = REGISTRY["apply_text_edit"]
        self.assertEqual(tool.side, "front")
        props = tool.schema["parameters"]["properties"]
        self.assertIn("old_string", props)
        self.assertIn("new_string", props)
        self.assertIn("path", props)

    def test_programming_agent_grants_apply_text_edit(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        agent_def = (
            repo / "ai_agent_service/app/agents/agent_defs/programming-agent.md"
        ).read_text(encoding="utf-8")
        self.assertIn("apply_text_edit", agent_def.split("---")[1])

    def test_frontend_dispatches_apply_text_edit_to_local_edit_handler(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        source = (
            repo / "ai_agent_frontend/addons/ai_agent/tools/program_tools.gd"
        ).read_text(encoding="utf-8")
        self.assertIn("static func apply_text_edit(", source)
        executor = (
            repo / "ai_agent_frontend/addons/ai_agent/tools/tool_executor.gd"
        ).read_text(encoding="utf-8")
        self.assertIn("ProgramTools.apply_text_edit(", executor)
        # write_file 的整文件覆盖别名组里不应该再混入 apply_text_edit。
        self.assertNotIn(
            '"write_file", "propose_script_edit", "propose_tests", "propose_content_file", "apply_text_edit"',
            executor,
        )

    def test_frontend_requires_prior_read_before_local_edit(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        source = (
            repo / "ai_agent_frontend/addons/ai_agent/tools/program_tools.gd"
        ).read_text(encoding="utf-8")
        self.assertIn("file_state_cache.has_state(path)", source)
        cache_source = (
            repo / "ai_agent_frontend/addons/ai_agent/context/file_state_cache.gd"
        ).read_text(encoding="utf-8")
        self.assertIn("func has_state(path: String) -> bool", cache_source)


class GrepCodeTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_pathological_pattern_aborts_instead_of_hanging(self) -> None:
        import app.tools.server_tools.grep_code as g

        class _Sec:
            def __init__(self, root: Path) -> None:
                self.project_root = root

        class _Ctx:
            def __init__(self, root: Path) -> None:
                self.security = _Sec(root)
                self.session_id = "s1"

        original_path_ok = g.path_ok
        g.path_ok = lambda rel, sec, write=False: True  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "ok.gd").write_text("print(1)\nprint(2)\n", encoding="utf-8")
                normal = await g.grep_code_handler(
                    {"pattern": "print", "include": "**/*.gd"}, _Ctx(root)
                )
                self.assertEqual(len(normal["matches"]), 2)
                self.assertFalse(normal["regex_timeout"])

                (root / "big.txt").write_text("a" * 80 + "b", encoding="utf-8")
                start = asyncio.get_event_loop().time()
                patho = await g.grep_code_handler(
                    {"pattern": r"(a|a)*$", "include": "**/*.txt"}, _Ctx(root)
                )
                elapsed = asyncio.get_event_loop().time() - start
                self.assertTrue(patho["regex_timeout"])
                # 应在 per-line 超时附近就放弃，而不是无限回溯。
                self.assertLess(elapsed, 5.0)
        finally:
            g.path_ok = original_path_ok  # type: ignore[assignment]


if __name__ == "__main__":
    unittest.main()
