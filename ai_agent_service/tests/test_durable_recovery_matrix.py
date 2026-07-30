"""会话 reset 与 durable recovery 的确定性故障及端到端矩阵。"""

from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from app.api.schemas import ChatRequest
from app.config import AppSettings
from app.events.store import EventStore
from app.llm.provider import AssistantTurn, LLMError, LLMProvider, OpenAICompatibleProvider
from app.orchestrator.agent import _invoke_server_tool, _tool_message
from app.orchestrator.map_artifacts import StagedMapArtifactTurn
from app.query.engine import QueryEngine, _SubmissionPublicationBuffer
from app.recovery.supervisor import (
    RECOVERY_FAILPOINTS,
    RecoverySupervisor,
)
from app.security.settings import SecuritySettings
from app.sessions.resource_registry import RESET_FAILPOINTS
from app.sessions.store import SessionStore
from app.tools.context import ToolContext
from app.tools.registry import ToolDef


class _UnusedProvider(LLMProvider):
    """不允许实际模型调用的 provider。"""

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> Any:
        """拒绝意外模型调用。"""
        raise AssertionError("provider must not be called")


class _BlockingProvider(LLMProvider):
    """用于确定性制造响应传输取消窗口。"""

    def __init__(self) -> None:
        """初始化进入信号。"""
        self.entered = asyncio.Event()

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> Any:
        """阻塞直到调用方取消任务。"""
        self.entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class _NamedFailureInjector:
    """命中一个指定边界一次后抛出可恢复 I/O 错误。"""

    def __init__(self, failpoint: str) -> None:
        """保存待触发边界。"""
        self.failpoint = failpoint
        self.triggered = False

    def hit(self, name: str) -> None:
        """在目标边界抛出一次错误。"""
        if name == self.failpoint and not self.triggered:
            self.triggered = True
            raise OSError(f"injected failure: {name}")


class _ExitInjector:
    """命中边界时模拟进程立即退出。"""

    def __init__(self, failpoint: str) -> None:
        """保存待触发边界。"""
        self.failpoint = failpoint

    def hit(self, name: str) -> None:
        """用 SystemExit 模拟不会运行异常清理的进程退出。"""
        if name == self.failpoint:
            raise SystemExit(name)


class _RecordingInjector:
    """记录所有经过的恢复边界。"""

    def __init__(self) -> None:
        """初始化空记录。"""
        self.hits: list[str] = []

    def hit(self, name: str) -> None:
        """记录命名边界。"""
        self.hits.append(name)


def _settings(root: Path) -> AppSettings:
    """构造最小本地测试配置。"""
    return AppSettings(
        llm_base_url="http://localhost",
        project_root=root,
        rag_auto_build_enabled=False,
    )


def _write_preserved_project_state(root: Path) -> list[Path]:
    """写入 reset 永远不得删除的权威项目资源。"""
    paths = [
        root / "project.godot",
        root / ".ai_agent_service/map_agent/revisions.json",
        root / ".ai_agent_service/transactions/tx.json",
        root / ".ai_agent_service/map_agent/resource_registry.json",
        root / ".ai_agent_service/map_agent/spatial_index.json",
        root / ".ai_agent_service/global_memory.json",
        root / ".ai_agent_service/rag/index.json",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve", encoding="utf-8")
    return paths


class ResetFailureMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_epoch_failure_keeps_prior_conversation_active(self) -> None:
        """barrier 前失败不得承认或后台完成 reset。"""
        for failpoint in {
            "reset_record_after_prepare",
            "epoch_barrier_before_write",
        }:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                injector = _NamedFailureInjector(failpoint)
                store = SessionStore(
                    root / "sessions",
                    project_root=root,
                    reset_failure_injector=injector,
                )
                session = store.get_or_create("s1", set())
                old_epoch = session.session_epoch
                session.turn_counter = 4
                store.save(session)
                engine = QueryEngine(
                    _settings(root),
                    store,
                    _UnusedProvider(),
                    event_store=EventStore(),
                )

                response = await engine.reset("s1")

                self.assertFalse(response.ok)
                self.assertEqual(store.current_epoch("s1"), old_epoch)
                self.assertEqual(
                    store.get_or_create("s1", set()).turn_counter,
                    4,
                )
                restarted = SessionStore(root / "sessions", project_root=root)
                QueryEngine(
                    _settings(root),
                    restarted,
                    _UnusedProvider(),
                    event_store=EventStore(),
                )
                self.assertEqual(restarted.current_epoch("s1"), old_epoch)
                self.assertEqual(restarted.get_or_create("s1", set()).turn_counter, 4)

    async def test_crash_before_epoch_barrier_is_abandoned_on_restart(self) -> None:
        """prepared-only reset 在重启时不得越过未建立的 epoch barrier。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(
                root / "sessions",
                project_root=root,
                reset_failure_injector=_ExitInjector("epoch_barrier_before_write"),
            )
            session = store.get_or_create("s1", set())
            old_epoch = session.session_epoch
            session.turn_counter = 7
            store.save(session)
            with self.assertRaises(SystemExit):
                store.begin_reset("s1")

            restarted = SessionStore(root / "sessions", project_root=root)
            QueryEngine(
                _settings(root),
                restarted,
                _UnusedProvider(),
                event_store=EventStore(),
            )

            self.assertEqual(restarted.current_epoch("s1"), old_epoch)
            self.assertEqual(restarted.get_or_create("s1", set()).turn_counter, 7)
            self.assertEqual(restarted.pending_reset_records(), [])

    async def test_every_cleanup_boundary_restarts_idempotently(self) -> None:
        """每个 reset 清理边界失败后都能重启续跑且保留项目权威状态。"""
        failpoints = sorted(
            name
            for name in RESET_FAILPOINTS
            if name.startswith("cleanup_") or name == "reset_record_before_cleaned"
        )
        for failpoint in failpoints:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                preserved = _write_preserved_project_state(root)
                store = SessionStore(
                    root / "sessions",
                    project_root=root,
                    reset_failure_injector=_NamedFailureInjector(failpoint),
                )
                session = store.get_or_create("s1", set())
                old_epoch = session.session_epoch
                session.turn_counter = 4
                store.save(session)
                events = EventStore()
                events.append("s1", "old", {}, session_epoch=old_epoch)
                engine = QueryEngine(
                    _settings(root),
                    store,
                    _UnusedProvider(),
                    event_store=events,
                )

                response = await engine.reset("s1")

                self.assertTrue(response.ok)
                self.assertNotEqual(response.session_epoch, old_epoch)
                restarted = SessionStore(root / "sessions", project_root=root)
                restarted_events = EventStore()
                QueryEngine(
                    _settings(root),
                    restarted,
                    _UnusedProvider(),
                    event_store=restarted_events,
                )
                self.assertEqual(restarted.pending_reset_records(), [])
                self.assertEqual(
                    restarted.current_epoch("s1"),
                    response.session_epoch,
                )
                self.assertEqual(
                    restarted.get_or_create("s1", set()).turn_counter,
                    0,
                )
                self.assertTrue(all(path.exists() for path in preserved))


class RecoverySupervisorMatrixTests(unittest.TestCase):
    def test_all_internal_recovery_boundaries_are_deterministic(self) -> None:
        """Attempt、token、调度和终态边界均经过命名 failpoint。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            session = store.get_or_create("s1", set())
            recorder = _RecordingInjector()
            supervisor = RecoverySupervisor(recorder)
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit"),
            )
            problem = supervisor.problem(
                session,
                error_code="map_artifact_turn_identity_conflict",
                text="conflict",
                side_effect_state="committed",
            )
            token = str(problem["retry_token"])
            supervisor.begin_attempt(
                session,
                ChatRequest(
                    session_id="s1",
                    user_message="edit",
                    recovery_token=token,
                ),
            )
            supervisor.complete_attempt(session, waiting_frontend=False)
            supervisor.mark_terminal(
                session,
                outcome="completed",
                authorized_by="completion_gate",
            )
            external = {
                "fresh_turn_before_allocate",
                "fresh_turn_after_allocate",
                "event_delivery_before_publish",
                "event_delivery_after_publish",
            }
            self.assertEqual(
                (RECOVERY_FAILPOINTS - external) - set(recorder.hits),
                set(),
            )

    def test_restart_preserves_first_root_cause_budget_and_schedule(self) -> None:
        """重启续接同一 TaskRun，不重复 Attempt 或清零独立预算。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root)
            session = store.get_or_create("s1", set())
            supervisor = RecoverySupervisor()
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit"),
            )
            problem = supervisor.problem(
                session,
                error_code="submission_internal_error",
                text="first failure",
                side_effect_state="rolled_back",
            )
            store.save_task_run(session)
            original = copy.deepcopy(session.task_run)

            restarted_store = SessionStore(root)
            restored = restarted_store.get_or_create("s1", set())
            resumed = supervisor.resume_after_restart(restored)

            self.assertIsNotNone(resumed)
            assert restored.task_run is not None
            self.assertEqual(
                restored.task_run["first_root_cause"]["text"],
                "first failure",
            )
            self.assertEqual(
                restored.task_run["retry_counts"],
                original["retry_counts"],
            )
            self.assertEqual(
                len(restored.task_run["attempt_history"]),
                len(original["attempt_history"]),
            )
            self.assertEqual(problem["disposition"], "retry_new_attempt")
            self.assertGreater(problem["next_action"]["backoff_ms"], 0)

    def test_ambiguous_effect_and_exhausted_budget_pause(self) -> None:
        """歧义副作用和预算耗尽都必须暂停，不能自动 replay。"""
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionStore(Path(tmp)).get_or_create("s1", set())
            supervisor = RecoverySupervisor()
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit"),
            )
            ambiguous = supervisor.problem(
                session,
                error_code="submission_internal_error",
                text="unknown outcome",
                side_effect_state="ambiguous",
            )
            self.assertEqual(ambiguous["disposition"], "pause_for_user")
            self.assertFalse(ambiguous["retryable"])

            session.task_run = None
            request = ChatRequest(session_id="s1", user_message="edit")
            supervisor.begin_attempt(session, request)
            last: dict[str, Any] = {}
            for _ in range(4):
                last = supervisor.problem(
                    session,
                    error_code="submission_internal_error",
                    text="rolled back",
                    side_effect_state="rolled_back",
                )
                token = last.get("retry_token")
                if isinstance(token, str):
                    supervisor.begin_attempt(
                        session,
                        request.model_copy(update={"recovery_token": token}),
                    )
            self.assertEqual(last["disposition"], "pause_for_user")
            self.assertFalse(last["retryable"])
            assert session.task_run is not None
            self.assertEqual(
                session.task_run["first_root_cause"]["text"],
                "rolled back",
            )

    def test_completion_and_cancellation_require_explicit_authority(self) -> None:
        """只有 Completion Gate 或显式取消可以授权相应终态。"""
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionStore(Path(tmp)).get_or_create("s1", set())
            supervisor = RecoverySupervisor()
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit"),
            )
            with self.assertRaises(ValueError):
                supervisor.mark_terminal(
                    session,
                    outcome="completed",
                    authorized_by="provider_response",
                )
            with self.assertRaises(ValueError):
                supervisor.mark_terminal(
                    session,
                    outcome="cancelled",
                    authorized_by="user_stop",
                )

    def test_generator_exit_is_recorded_as_transport_only(self) -> None:
        """GeneratorExit 类响应关闭不得把 durable task 改为完成或取消。"""
        with tempfile.TemporaryDirectory() as tmp:
            session = SessionStore(Path(tmp)).get_or_create("s1", set())
            supervisor = RecoverySupervisor()
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit"),
            )
            status_before = str(session.task_run["status"])
            supervisor.record_transport_loss(session, transport="response")
            assert session.task_run is not None
            self.assertEqual(session.task_run["status"], status_before)
            self.assertEqual(
                session.task_run["transport_history"][0]["error_code"],
                "response_transport_lost",
            )

    def test_every_durable_supervisor_boundary_recovers_without_duplicate_attempt(
        self,
    ) -> None:
        """每个内部 durable failpoint 重启后都保持单一 attempt 身份。"""
        external = {
            "fresh_turn_before_allocate",
            "fresh_turn_after_allocate",
            "event_delivery_before_publish",
            "event_delivery_after_publish",
        }
        for failpoint in sorted(RECOVERY_FAILPOINTS - external):
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = SessionStore(root)
                session = store.get_or_create("s1", set())
                baseline = RecoverySupervisor(
                    persist_callback=store.save_task_run,
                )
                injector = _NamedFailureInjector(failpoint)
                failing = RecoverySupervisor(
                    injector,
                    store.save_task_run,
                )
                try:
                    if failpoint.startswith("checkpoint_"):
                        failing.begin_attempt(
                            session,
                            ChatRequest(session_id="s1", user_message="edit"),
                        )
                    elif failpoint.startswith("retry_token_") and "consume" in failpoint:
                        baseline.begin_attempt(
                            session,
                            ChatRequest(session_id="s1", user_message="edit"),
                        )
                        problem = baseline.problem(
                            session,
                            error_code="map_artifact_turn_identity_conflict",
                            text="conflict",
                            side_effect_state="committed",
                        )
                        failing.begin_attempt(
                            session,
                            ChatRequest(
                                session_id="s1",
                                user_message="edit",
                                recovery_token=str(problem["retry_token"]),
                            ),
                        )
                    elif failpoint.startswith("terminal_cleanup_"):
                        baseline.begin_attempt(
                            session,
                            ChatRequest(session_id="s1", user_message="edit"),
                        )
                        failing.mark_terminal(
                            session,
                            outcome="completed",
                            authorized_by="completion_gate",
                        )
                    else:
                        baseline.begin_attempt(
                            session,
                            ChatRequest(session_id="s1", user_message="edit"),
                        )
                        failing.problem(
                            session,
                            error_code="map_artifact_turn_identity_conflict",
                            text="conflict",
                            side_effect_state="committed",
                        )
                except OSError:
                    pass
                self.assertTrue(injector.triggered)

                restarted_store = SessionStore(root)
                restored = restarted_store.get_or_create("s1", set())
                if isinstance(restored.task_run, dict):
                    RecoverySupervisor(
                        persist_callback=restarted_store.save_task_run,
                    ).resume_after_restart(restored)
                    attempts = restored.task_run.get("attempt_history", [])
                    ids = [
                        str(attempt.get("attempt_id"))
                        for attempt in attempts
                        if isinstance(attempt, dict)
                    ]
                    self.assertEqual(len(ids), len(set(ids)))
                    self.assertLessEqual(len(ids), 1)
                    self.assertEqual(restored.agent_stack, [])

    def test_fresh_turn_failpoints_preserve_monotonic_allocation(self) -> None:
        """fresh-turn 分配前后退出都不会重新分配已暴露 turn。"""
        for failpoint in {
            "fresh_turn_before_allocate",
            "fresh_turn_after_allocate",
        }:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = SessionStore(root)
                session = store.get_or_create("s1", set())
                session.turn_counter = 4
                store.save(session)
                supervisor = RecoverySupervisor(_NamedFailureInjector(failpoint))
                try:
                    supervisor.hit_failpoint("fresh_turn_before_allocate")
                    session.new_turn_id()
                    supervisor.hit_failpoint("fresh_turn_after_allocate")
                except OSError:
                    pass

                restarted = SessionStore(root).get_or_create("s1", set())
                next_turn = restarted.new_turn_id()
                self.assertGreaterEqual(int(next_turn[1:]), 5)
                if failpoint == "fresh_turn_after_allocate":
                    self.assertGreaterEqual(int(next_turn[1:]), 6)


class RecoveryBoundaryEndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_startup_resumes_persisted_recovery_schedule(self) -> None:
        """服务重启会恢复 TaskRun 调度并发布 recovery_resumed。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_store = SessionStore(root / "sessions", project_root=root)
            session = old_store.get_or_create("s1", set())
            supervisor = RecoverySupervisor(
                persist_callback=old_store.save_task_run,
            )
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit"),
            )
            supervisor.problem(
                session,
                error_code="submission_internal_error",
                text="restart me",
                side_effect_state="rolled_back",
            )

            events = EventStore()
            restarted_store = SessionStore(
                root / "sessions",
                project_root=root,
            )
            restarted_engine = QueryEngine(
                _settings(root),
                restarted_store,
                _UnusedProvider(),
                event_store=events,
            )
            resumed = await restarted_engine.resume_pending_recoveries()

            self.assertEqual(resumed, 1)
            restored = restarted_store.get_or_create(
                "s1",
                restarted_engine.available_tools,
            )
            assert restored.task_run is not None
            self.assertEqual(restored.task_run["status"], "recovering")
            self.assertEqual(len(restored.task_run["attempt_history"]), 1)
            recovery_events = [
                event for event in events.list_after("s1", 0) if event.type == "recovery_resumed"
            ]
            self.assertEqual(len(recovery_events), 1)

    async def test_server_tool_exception_returns_typed_continue_agent_result(self) -> None:
        """server tool 异常被封装为 active-agent 可消费的 typed result。"""

        async def failing_handler(
            _args: dict[str, Any],
            _context: ToolContext,
        ) -> dict[str, Any]:
            """模拟无副作用工具异常。"""
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tool = ToolDef(
                name="failing_tool",
                domain="core",
                side="server",
                is_read_only=True,
                handler=failing_handler,
            )
            result, is_error = await _invoke_server_tool(
                tool,
                {},
                ToolContext(
                    SecuritySettings(project_root=root),
                    session_id="s1",
                    session_epoch="e1",
                ),
            )
            message = _tool_message("call-1", result, is_error=is_error)
            payload = json.loads(message["content"])
            self.assertTrue(is_error)
            self.assertEqual(payload["error_code"], "server_tool_exception")
            self.assertEqual(payload["disposition"], "continue_agent")

    async def test_provider_fallback_is_selected_only_inside_backend(self) -> None:
        """primary 失败后的 fallback 由 provider 边界选择一次。"""
        provider = object.__new__(OpenAICompatibleProvider)
        provider._default_model = "primary"  # type: ignore[attr-defined]
        provider._fallback_model = "fallback"  # type: ignore[attr-defined]
        calls: list[str] = []
        fallbacks: list[tuple[str, str]] = []

        async def fake_chat_once(
            _messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]],
            model: str,
            *_args: Any,
        ) -> AssistantTurn:
            """primary 失败，fallback 成功。"""
            calls.append(model)
            if model == "primary":
                raise LLMError("primary failed")
            return AssistantTurn(
                raw_message={"role": "assistant", "content": "ok"},
                content="ok",
                model=model,
            )

        provider._chat_once = fake_chat_once  # type: ignore[method-assign]
        result = await provider.chat(
            [],
            [],
            on_fallback=lambda primary, fallback: fallbacks.append((primary, fallback)),
        )
        self.assertEqual(calls, ["primary", "fallback"])
        self.assertEqual(fallbacks, [("primary", "fallback")])
        self.assertEqual(result.model, "fallback")

    async def test_provider_primary_and_fallback_exhaustion_propagates_once(
        self,
    ) -> None:
        """primary/fallback 都失败时由模型边界报告耗尽，不由前端选模型。"""
        provider = object.__new__(OpenAICompatibleProvider)
        provider._default_model = "primary"  # type: ignore[attr-defined]
        provider._fallback_model = "fallback"  # type: ignore[attr-defined]
        calls: list[str] = []

        async def failing_chat_once(
            _messages: list[dict[str, Any]],
            _tools: list[dict[str, Any]],
            model: str,
            *_args: Any,
        ) -> AssistantTurn:
            """记录并拒绝两个 provider attempt。"""
            calls.append(model)
            raise LLMError(f"{model} failed")

        provider._chat_once = failing_chat_once  # type: ignore[method-assign]
        with self.assertRaises(LLMError):
            await provider.chat([], [])
        self.assertEqual(calls, ["primary", "fallback"])

    async def test_event_delivery_failpoints_are_idempotent_transport_state(
        self,
    ) -> None:
        """publish 前后失败重试都不会复制同一 committed event。"""
        for failpoint in {
            "event_delivery_before_publish",
            "event_delivery_after_publish",
        }:
            with self.subTest(failpoint=failpoint), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                events = EventStore()
                store = SessionStore(root / "sessions", project_root=root)
                engine = QueryEngine(
                    _settings(root),
                    store,
                    _UnusedProvider(),
                    event_store=events,
                    recovery_failure_injector=_NamedFailureInjector(failpoint),
                )
                session = store.get_or_create("s1", engine.available_tools)
                engine._recovery_supervisor.begin_attempt(
                    session,
                    ChatRequest(session_id="s1", user_message="edit"),
                )
                buffer = _SubmissionPublicationBuffer(
                    session=session,
                    request_id="r1",
                    turn_id="t1",
                    map_artifact_turn=StagedMapArtifactTurn(
                        session_id="s1",
                        session_epoch=session.session_epoch,
                        turn_id="t1",
                        request_id="r1",
                    ),
                )
                buffer.events.append(("s1", "grant_created", {"grant_id": "g1"}))

                engine._flush_submission_publications(buffer)
                engine._flush_submission_publications(buffer)

                delivered = [
                    event for event in events.list_after("s1", 0) if event.type == "grant_created"
                ]
                self.assertEqual(len(delivered), 1)
                assert session.task_run is not None
                self.assertEqual(
                    len(session.task_run.get("transport_history", [])),
                    1,
                )

    async def test_cancelled_response_transport_persists_pause_without_idle(self) -> None:
        """客户端断连取消请求后，TaskRun 以 checkpointed pause 留存。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "sessions", project_root=root)
            provider = _BlockingProvider()
            engine = QueryEngine(
                _settings(root),
                store,
                provider,
                event_store=EventStore(),
            )
            task = asyncio.create_task(
                engine.submit_user_turn(
                    ChatRequest(
                        session_id="s1",
                        request_id="r1",
                        user_message="inspect project",
                    )
                )
            )
            await asyncio.wait_for(provider.entered.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            restored = store.get_or_create("s1", engine.available_tools)
            assert restored.task_run is not None
            self.assertEqual(restored.task_run["status"], "paused")
            self.assertEqual(
                restored.task_run["first_root_cause"]["error_code"],
                "response_transport_lost",
            )

    async def test_explicit_stop_resume_and_cancel_use_distinct_task_states(self) -> None:
        """stop 保留检查点，resume 续接，只有 cancel 进入 cancelled。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SessionStore(root / "sessions", project_root=root)
            engine = QueryEngine(
                _settings(root),
                store,
                _UnusedProvider(),
                event_store=EventStore(),
            )
            session = store.get_or_create("s1", engine.available_tools)
            session.map_task_state.start_new_task("task-1", lineage_id="lineage-1")
            session.map_task_lineage = {"lineage_id": "lineage-1"}
            session.map_task_state.make_checkpoint(
                "user stopped",
                pause_kind="user_interrupted",
            )
            supervisor = RecoverySupervisor()
            supervisor.begin_attempt(
                session,
                ChatRequest(session_id="s1", user_message="edit"),
            )
            supervisor.problem(
                session,
                error_code="user_stop",
                text="stopped",
                side_effect_state="none",
            )
            store.save_task_run(session)
            store.save(session)

            resumed = await engine.resume_paused_map_task("s1")
            self.assertTrue(resumed["resumed"])
            self.assertEqual(session.map_task_state.status, "running")
            assert session.task_run is not None
            self.assertEqual(session.task_run["status"], "recovering")

            cancelled = await engine.cancel_map_task("s1")
            self.assertTrue(cancelled["cancelled"])
            self.assertEqual(session.map_task_state.status, "cancelled")
            self.assertEqual(session.task_run["status"], "cancelled")
