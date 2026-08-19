"""Session epoch reset 与 durable attempt recovery 回归测试。"""

from __future__ import annotations

import copy
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.api.schemas import ChatFinalResponse, ChatRequest, ToolResult
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMProvider
from app.orchestrator.delegate_artifacts import DelegateArtifactStore
from app.orchestrator.map_artifacts import MapArtifactStore, StagedMapArtifactTurn
from app.recovery.pointer import RecoveryPointerStore
from app.recovery.supervisor import (
    FAILURE_POLICIES,
    RecoverySupervisor,
    RecoveryTokenError,
)
from app.sessions.resource_registry import (
    BACKEND_RESET_STEPS,
    SESSION_RESOURCE_BY_ID,
    SESSION_RESOURCE_CONTRACTS,
)
from app.sessions.store import SessionStore
from app.tools.front_tools import register_front_tools
from tests.application_test_support import build_test_application


class _UnusedProvider(LLMProvider):
    """这些测试不应调用模型。"""

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("provider must not be called")


class _FinalProvider(LLMProvider):
    """返回确定性最终回答的 provider。"""

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> AssistantTurn:
        """返回无工具调用的最终回答。"""
        return AssistantTurn(
            raw_message={"role": "assistant", "content": "fresh"},
            content="fresh",
            model="test",
        )


class SessionEpochResetTests(unittest.IsolatedAsyncioTestCase):
    async def test_reset_isolates_every_session_owned_resource(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            register_front_tools()
            sessions = SessionStore(root / "sessions", project_root=root)
            events = EventStore()
            recovery = RecoveryPointerStore(root / "recovery.json", root)
            engine = build_test_application(
                settings=AppSettings(
                    llm_base_url="http://localhost",
                    project_root=root,
                    rag_auto_build_enabled=False,
                ),
                session_store=sessions,
                llm=_UnusedProvider(),
                event_store=events,
                recovery_store=recovery,
            )
            session = sessions.get_or_create("s1", engine.available_tools)
            old_epoch = session.session_epoch
            session.turn_counter = 4
            session.request_id_cache["old"] = {"type": "final", "text": "old"}
            session.completed_turn_ledger["t4"] = {"fingerprint": "old"}
            session.completed_response_hot_cache["t4"] = {
                "type": "final",
                "text": "old",
            }
            session.history_event_counter = 7
            sessions.save(session)
            events.ensure_sequence("s1", 7, session_epoch=old_epoch)
            events.append("s1", "old_event", {}, session_epoch=old_epoch)
            recovery.write("s1", "t4", 8, session_epoch=old_epoch)

            staged = StagedMapArtifactTurn(
                session_id="s1",
                session_epoch=old_epoch,
                turn_id="t4",
                request_id="r1",
            )
            staged.add_entry(
                tool_use_id="tool-1",
                tool_name="describe_map_region",
                tool_args={},
                result={"cells": [{"x": 1, "y": 2}]},
            )
            map_store = MapArtifactStore(root, "s1", session_epoch=old_epoch)
            map_store.merge_turn(staged)
            old_map_fingerprint = str(staged.entries["tool-1"]["fingerprint"])
            delegate_store = DelegateArtifactStore(root, "s1", old_epoch)
            old_delegate_ref = delegate_store.store(
                frame_id="f1",
                agent_name="map-reader-agent",
                result_schema="test",
                result={"summary": "old"},
            )
            engine.lifecycle._history_blocks_cache[("s1", old_epoch)] = (
                (1, 1, 1),
                ["old projection"],
            )
            old_copy = copy.deepcopy(session)

            response = await engine.use_cases.reset.execute("s1")

            self.assertTrue(response.ok)
            self.assertNotEqual(response.session_epoch, old_epoch)
            self.assertGreater(response.last_event_seq, 8)
            self.assertFalse(map_store.session_root.exists())
            self.assertFalse(delegate_store.session_root.exists())
            self.assertIsNone(recovery.read("s1"))
            self.assertNotIn(("s1", old_epoch), engine.lifecycle._history_blocks_cache)
            with self.assertRaises(ValueError):
                MapArtifactStore(
                    root,
                    "s1",
                    session_epoch=response.session_epoch,
                ).read_page(
                    map_store.relative_ref,
                    turn_id="t4",
                    entry_id="tool-1",
                    fingerprint=old_map_fingerprint,
                )
            with self.assertRaises(ValueError):
                DelegateArtifactStore(
                    root,
                    "s1",
                    response.session_epoch,
                ).read_page(old_delegate_ref)
            page = events.page_after(
                "s1",
                0,
                limit=10,
                session_epoch=response.session_epoch,
            )
            self.assertEqual([event.type for event in page.events], ["session_reset"])
            self.assertEqual(page.session_epoch, response.session_epoch)

            fresh = sessions.get_or_create("s1", engine.available_tools)
            self.assertEqual(fresh.session_epoch, response.session_epoch)
            self.assertEqual(fresh.turn_counter, 0)
            self.assertEqual(fresh.request_id_cache, {})
            self.assertEqual(fresh.completed_turn_ledger, {})
            self.assertEqual(fresh.completed_response_hot_cache, {})
            self.assertIsNone(fresh.task_run)
            with self.assertRaises(ValueError):
                sessions.save(old_copy)

    async def test_repeated_reset_keeps_sequence_monotonic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = EventStore()
            engine = build_test_application(
                settings=AppSettings(
                    llm_base_url="http://localhost",
                    project_root=root,
                    rag_auto_build_enabled=False,
                ),
                session_store=SessionStore(root / "sessions", project_root=root),
                llm=_UnusedProvider(),
                event_store=events,
            )
            first = await engine.use_cases.reset.execute("s1")
            second = await engine.use_cases.reset.execute("s1")
            self.assertTrue(first.ok and second.ok)
            self.assertNotEqual(first.session_epoch, second.session_epoch)
            self.assertGreater(second.last_event_seq, first.last_event_seq)

    async def test_first_post_reset_submission_succeeds_without_manual_cleanup(
        self,
    ) -> None:
        """复用 session_id 的新 epoch 可立即提交，不需要删除旧 t4。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "sessions", project_root=root)
            old = store.get_or_create("s1", set())
            old.turn_counter = 4
            old.completed_turn_ledger["t4"] = {
                "fingerprint": "old",
                "outcome_kind": "final",
                "commit_digest": "old",
                "response_locator": "old.json",
            }
            old.completed_response_hot_cache["t4"] = {
                "type": "final",
                "text": "old",
            }
            store.save(old)
            engine = build_test_application(
                settings=AppSettings(
                    llm_base_url="http://localhost",
                    project_root=root,
                    rag_auto_build_enabled=False,
                ),
                session_store=store,
                llm=_FinalProvider(),
                event_store=EventStore(),
            )
            reset = await engine.use_cases.reset.execute("s1")
            response = await engine.execute(
                ChatRequest(
                    session_id="s1",
                    session_epoch=reset.session_epoch,
                    request_id="fresh-r1",
                    user_message="hello",
                )
            )
            self.assertIsInstance(response, ChatFinalResponse)
            self.assertEqual(response.text, "fresh")
            fresh = store.get_or_create("s1", engine.available_tools)
            self.assertEqual(fresh.session_epoch, reset.session_epoch)
            self.assertNotIn("t4", fresh.completed_turn_ledger)
            self.assertNotIn("t4", fresh.completed_response_hot_cache)

    async def test_committed_turn_conflict_recovers_inside_backend(self) -> None:
        """原 t4 冲突不结束任务；后端在 t5 重新发布并返回原成功响应。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            register_front_tools()
            sessions = SessionStore(root / "sessions", project_root=root)
            engine = build_test_application(
                settings=AppSettings(
                    llm_base_url="http://localhost",
                    project_root=root,
                    rag_auto_build_enabled=False,
                ),
                session_store=sessions,
                llm=_UnusedProvider(),
                event_store=EventStore(),
            )
            session = sessions.get_or_create("s1", engine.available_tools)
            session.turn_counter = 4
            frame = Frame(
                id="f1",
                agent=get_agent("coordinator", engine.available_tools),
                messages=[],
            )
            session.agent_stack = [frame]
            session.pending_turn_id = "t4"
            session.pending_tool_call_ids = {"tool-front"}
            session.pending_tool_calls = {
                "tool-front": {
                    "name": "read_scene_tree",
                    "input": {},
                    "frame_id": "f1",
                    "needs_confirm": False,
                    "authorization": "allow",
                }
            }
            sessions.save(session)

            artifact_store = MapArtifactStore(
                root,
                "s1",
                session_epoch=session.session_epoch,
            )
            committed = StagedMapArtifactTurn(
                session_id="s1",
                session_epoch=session.session_epoch,
                turn_id="t4",
                request_id="old",
            )
            committed.add_entry(
                tool_use_id="artifact-entry",
                tool_name="describe_map_region",
                tool_args={},
                result={"cells": [{"x": 1}], "revision": 1},
            )
            artifact_store.merge_turn(committed)

            async def successful_submission(
                working: Any,
                _request: Any,
                _batch: Any,
                publication: Any,
            ) -> ChatFinalResponse:
                publication.map_artifact_turn.add_entry(
                    tool_use_id="artifact-entry",
                    tool_name="describe_map_region",
                    tool_args={},
                    result={"cells": [{"x": 2}], "revision": 2},
                )
                working.clear_pending()
                return ChatFinalResponse(text="continued")

            engine.coordinator._backend_recovery._turn_service.execute = successful_submission  # type: ignore[method-assign]
            response = await engine.execute(
                ChatRequest(
                    session_id="s1",
                    session_epoch=session.session_epoch,
                    request_id="r1",
                    tool_results=[
                        ToolResult(
                            tool_use_id="tool-front",
                            frame_id="f1",
                            turn_id="t4",
                            status="applied",
                            result={"name": "Root"},
                        )
                    ],
                )
            )

            self.assertIsInstance(response, ChatFinalResponse)
            self.assertEqual(response.text, "continued")
            restored = sessions.get_or_create("s1", engine.available_tools)
            self.assertEqual(restored.turn_counter, 5)
            self.assertIsNotNone(restored.task_run)
            assert restored.task_run is not None
            self.assertEqual(restored.task_run["status"], "succeeded")
            self.assertEqual(len(restored.task_run["attempt_history"]), 2)
            recovered = artifact_store.read_page(
                artifact_store.relative_ref,
                turn_id="t5",
                entry_id="artifact-entry",
                fingerprint=str(
                    artifact_store._load_document()["turns"]["t5"]["entries"]["artifact-entry"][
                        "fingerprint"
                    ]
                ),
                field="revision",
            )
            self.assertEqual(recovered["value"], 2)


class RecoverySupervisorTests(unittest.TestCase):
    def test_every_declared_policy_has_bounded_owner_contract(self) -> None:
        repo = Path(__file__).resolve().parents[1] / "app"
        discovered: set[str] = set()
        pattern = re.compile(r'error_code\s*=\s*["\']([a-z0-9_.:-]+)["\']')
        for path in repo.rglob("*.py"):
            discovered.update(pattern.findall(path.read_text(encoding="utf-8")))
        self.assertEqual(discovered - set(FAILURE_POLICIES), set())
        self.assertEqual(
            {policy.scope for policy in FAILURE_POLICIES.values()},
            {
                "request",
                "provider",
                "server_tool",
                "front_tool",
                "plan_step",
                "transaction",
                "publication",
                "persistence",
                "transport",
                "task",
            },
        )
        self.assertEqual(
            {policy.disposition for policy in FAILURE_POLICIES.values()},
            {
                "continue_agent",
                "retry_same_attempt",
                "retry_new_attempt",
                "retry_new_turn",
                "refresh_and_replan",
                "wait_frontend",
                "pause_for_user",
                "terminal",
            },
        )
        for policy in FAILURE_POLICIES.values():
            self.assertGreaterEqual(policy.budget, 0)
            self.assertIn(policy.retry_owner, {"backend", "frontend", "user", "none"})
            self.assertTrue(policy.budget_key)
            self.assertTrue(policy.terminal_condition)

    def test_retry_token_is_bound_and_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.get_or_create("s1", set())
            supervisor = RecoverySupervisor()
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit map"),
            )
            problem = supervisor.problem(
                session,
                error_code="map_artifact_turn_identity_conflict",
                text="conflict",
                next_action={"action": "resubmit_tool_results", "turn_id": "t5"},
            )
            token = str(problem["retry_token"])
            self.assertTrue(token)
            self.assertEqual(problem["disposition"], "retry_new_turn")
            self.assertEqual(problem["side_effect_state"], "committed")

            supervisor.begin_attempt(
                session,
                ChatRequest(
                    session_id="s1",
                    tool_results=[],
                    recovery_token=token,
                ),
            )
            with self.assertRaises(RecoveryTokenError):
                supervisor.begin_attempt(
                    session,
                    ChatRequest(
                        session_id="s1",
                        tool_results=[],
                        recovery_token=token,
                    ),
                )


class FrontendResetContractTests(unittest.TestCase):
    def test_frontend_uses_ack_and_structured_disposition(self) -> None:
        repo = Path(__file__).resolve().parents[2]
        panel = (repo / "ai_agent_frontend/addons/ai_agent/ui/chat_panel.gd").read_text(
            encoding="utf-8"
        )
        client = (
            repo / "ai_agent_frontend/addons/ai_agent/service/agent_http_client.gd"
        ).read_text(encoding="utf-8")
        cache = (repo / "ai_agent_frontend/addons/ai_agent/context/file_state_cache.gd").read_text(
            encoding="utf-8"
        )

        self.assertIn("RESETTING", panel)
        self.assertIn("func _handle_reset_response", panel)
        self.assertIn("func _on_problem(problem: Dictionary)", panel)
        self.assertIn('problem.get("disposition"', panel)
        self.assertIn("_recovery_prompt.hide()", panel)
        self.assertIn("RECOVERY_FRONTEND_FAILPOINTS", client)
        self.assertIn('"response_transport_lost"', client)
        self.assertIn("Blocked retired frontend tool-result forwarding", client)
        self.assertNotIn('"tool_results": valid_results', client)
        self.assertNotIn('contains("会话状态已回滚")', client)
        self.assertIn("generate_random_bytes(16).hex_encode()", panel)
        self.assertIn("func clear() -> void:", cache)


class SessionResourceInventoryTests(unittest.TestCase):
    def test_every_resource_is_uniquely_classified_and_backend_steps_are_owned(self) -> None:
        ids = [contract.resource_id for contract in SESSION_RESOURCE_CONTRACTS]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(set(BACKEND_RESET_STEPS) - set(ids), set())
        for resource_id in BACKEND_RESET_STEPS:
            self.assertEqual(
                SESSION_RESOURCE_BY_ID[resource_id].ownership,
                "reset_owned",
            )
        preserved = {
            contract.resource_id
            for contract in SESSION_RESOURCE_CONTRACTS
            if contract.ownership == "preserved"
        }
        self.assertTrue(
            {
                "godot_project_content",
                "authoritative_revisions",
                "transaction_journals",
                "registries_indexes_and_blueprints",
                "global_configuration_memory_and_rag",
            }.issubset(preserved)
        )
