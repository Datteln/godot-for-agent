from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.api.schemas import ChatFinalResponse
from app.config import AppSettings
from app.llm.provider import AssistantTurn, LLMError
from app.application.response_policy import _apply_verification_policy
from app.security.settings import security_settings_from_app
from app.sessions.store import Session, SessionStore
from app.verify.recovery import VerifyRecoveryExecutor, VerifyRecoveryRejected
from app.verify.runner import VerifyRunner
from app.verify.syntax_check import SyntaxCheckResult


class FixedProvider:
    def __init__(self, value: str | LLMError) -> None:
        self.value = value
        self.calls = 0

    @property
    def supports_tool_calling(self) -> bool:
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        return False

    async def chat(self, *_args: Any, **_kwargs: Any) -> AssistantTurn:
        self.calls += 1
        if isinstance(self.value, LLMError):
            raise self.value
        return AssistantTurn(
            raw_message={"role": "assistant", "content": self.value},
            content=self.value,
            model="verify-model",
        )


def _session(frame_id: str = "f1") -> Session:
    session = Session(session_id="s1", session_epoch="e1")
    session.agent_stack = [
        Frame(id=frame_id, agent=get_agent("coordinator", set()), messages=[])
    ]
    return session


def _candidate(path: str, frame_id: str = "f1", policy: str = "required") -> dict[str, Any]:
    return {
        "tool_use_id": "tool-1",
        "frame_id": frame_id,
        "tool_name": "apply_text_edit",
        "path": path,
        "input": {"path": path},
        "verification_policy": policy,
    }


class VerifyOutcomeRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        root: Path,
        provider: FixedProvider,
        candidate: dict[str, Any],
        *,
        session: Session | None = None,
        syntax: bool = False,
    ) -> tuple[Session, list[dict[str, Any]], Any]:
        settings = AppSettings(
            project_root=root,
            verify_syntax_enabled=syntax,
            verify_max_retries=2,
            rag_auto_build_enabled=False,
        )
        events: list[dict[str, Any]] = []
        runner = VerifyRunner(
            settings,
            provider,
            lambda _session_id, event_type, payload: events.append(
                {"type": event_type, "payload": payload}
            )
            or len(events),
            lambda _effort: "verify-model",
            lambda _effort: 0,
        )
        active = session or _session()
        outcomes = await runner.run(
            active,
            security_settings_from_app(settings),
            [candidate],
        )
        return active, events, outcomes[0]

    async def test_target_read_error_is_unavailable_and_exactly_injected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active, events, outcome = await self._run(
                Path(tmp),
                FixedProvider("unused"),
                _candidate("missing.py"),
            )

        self.assertEqual(outcome.status, "unavailable")
        self.assertEqual(outcome.reason_code, "target_unreadable")
        payload = events[-1]["payload"]
        self.assertNotIn("passed", payload)
        self.assertEqual(payload["outcome"]["reason_code"], "target_unreadable")
        injected = json.loads(str(active.agent_stack[0].messages[-1]["content"]))
        self.assertEqual(injected["verify_outcome"], payload["outcome"])

    async def test_missing_frame_and_exhausted_budget_never_call_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("value = 1\n", encoding="utf-8")
            missing_provider = FixedProvider("unused")
            _, _, missing = await self._run(
                root,
                missing_provider,
                _candidate("ok.py", frame_id="missing"),
            )
            exhausted_session = _session()
            exhausted_session.verify_retry_count["ok.py"] = 2
            exhausted_provider = FixedProvider("unused")
            _, _, exhausted = await self._run(
                root,
                exhausted_provider,
                _candidate("ok.py"),
                session=exhausted_session,
            )

        self.assertEqual(missing.reason_code, "owning_frame_missing")
        self.assertEqual(exhausted.reason_code, "attempt_budget_exhausted")
        self.assertEqual(missing_provider.calls, 0)
        self.assertEqual(exhausted_provider.calls, 0)

    async def test_provider_error_and_malformed_response_are_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("value = 1\n", encoding="utf-8")
            _, _, provider_error = await self._run(
                root,
                FixedProvider(LLMError("offline", error_code="llm_error")),
                _candidate("ok.py"),
            )
            _, _, malformed = await self._run(
                root,
                FixedProvider("not json"),
                _candidate("ok.py"),
            )
            _, _, timeout = await self._run(
                root,
                FixedProvider(LLMError("timeout", error_code="provider_timeout")),
                _candidate("ok.py"),
            )
            legacy_payload = json.dumps(
                {"passed": True, "issues": [], "summary": "legacy"}
            )
            _, _, unsupported = await self._run(
                root,
                FixedProvider(legacy_payload),
                _candidate("ok.py"),
            )

        self.assertEqual(provider_error.reason_code, "provider_error")
        self.assertEqual(malformed.reason_code, "response_malformed")
        self.assertEqual(timeout.reason_code, "provider_timeout")
        self.assertEqual(unsupported.reason_code, "unsupported_verify_schema")

    async def test_semantic_pass_and_failure_use_canonical_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("value = 1\n", encoding="utf-8")
            passed_payload = json.dumps(
                {
                    "schema_version": 1,
                    "status": "passed",
                    "phase": "semantic",
                    "reason_code": "verified",
                    "summary": "verified",
                    "issues": [],
                    "attempt": 1,
                    "max_attempts": 2,
                    "retryable": False,
                    "recovery_actions": [],
                }
            )
            failed_payload = json.dumps(
                {
                    "schema_version": 1,
                    "status": "failed",
                    "phase": "semantic",
                    "reason_code": "semantic_issue",
                    "summary": "bad reference",
                    "issues": [
                        {
                            "severity": "error",
                            "file_path": "ok.py",
                            "line": 1,
                            "message": "undefined reference",
                        }
                    ],
                    "attempt": 1,
                    "max_attempts": 2,
                    "retryable": True,
                    "recovery_actions": [],
                }
            )
            _, _, passed = await self._run(
                root, FixedProvider(passed_payload), _candidate("ok.py")
            )
            _, _, failed = await self._run(
                root, FixedProvider(failed_payload), _candidate("ok.py")
            )

        self.assertEqual(passed.status, "passed")
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.reason_code, "semantic_issue")

    async def test_missing_syntax_validator_emits_unavailable_before_semantic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "notes.txt").write_text("text\n", encoding="utf-8")
            passed_payload = json.dumps(
                {
                    "schema_version": 1,
                    "status": "passed",
                    "phase": "semantic",
                    "reason_code": "verified",
                    "summary": "verified",
                    "issues": [],
                    "attempt": 1,
                    "max_attempts": 2,
                    "retryable": False,
                    "recovery_actions": [],
                }
            )
            _, events, final = await self._run(
                root,
                FixedProvider(passed_payload),
                _candidate("notes.txt"),
                syntax=True,
            )

        completed = [item for item in events if item["type"] == "verify_completed"]
        self.assertEqual(completed[0]["payload"]["outcome"]["reason_code"], "validator_missing")
        self.assertEqual(final.status, "passed")

    async def test_syntax_timeout_is_unavailable_not_passed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("value = 1\n", encoding="utf-8")
            passed_payload = json.dumps(
                {
                    "schema_version": 1,
                    "status": "passed",
                    "phase": "semantic",
                    "reason_code": "verified",
                    "summary": "verified",
                    "issues": [],
                    "attempt": 1,
                    "max_attempts": 2,
                    "retryable": False,
                    "recovery_actions": [],
                }
            )
            report = SyntaxCheckResult(
                status="unavailable",
                reason_code="validator_timeout",
                summary="timed out",
            )
            with patch(
                "app.verify.runner.run_syntax_check",
                new=AsyncMock(return_value=report),
            ):
                _, events, _ = await self._run(
                    root,
                    FixedProvider(passed_payload),
                    _candidate("ok.py"),
                    syntax=True,
                )

        completed = [item for item in events if item["type"] == "verify_completed"]
        self.assertEqual(completed[0]["payload"]["outcome"]["status"], "unavailable")
        self.assertEqual(
            completed[0]["payload"]["outcome"]["reason_code"],
            "validator_timeout",
        )

    async def test_syntax_failure_is_failed_and_skips_semantic_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            provider = FixedProvider("unused")
            _, _, outcome = await self._run(
                root,
                provider,
                _candidate("bad.py"),
                syntax=True,
            )

        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.reason_code, "syntax_issue")
        self.assertGreater(len(outcome.issues), 0)
        self.assertEqual(provider.calls, 0)

    async def test_verify_attempts_survive_session_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active, _, _ = await self._run(
                root,
                FixedProvider("unused"),
                _candidate("missing.py"),
            )
            store = SessionStore(root / "sessions", project_root=root)
            persisted = store.get_or_create("verify-session", set())
            persisted.verify_attempts = active.verify_attempts
            persisted.verify_state = active.verify_state
            store.save(persisted)
            restored = SessionStore(root / "sessions", project_root=root).get_or_create(
                "verify-session", set()
            )
            self.assertEqual(restored.verify_attempts, active.verify_attempts)
            self.assertEqual(restored.verify_state, active.verify_state)

    async def test_recovery_action_is_single_use(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active, _, _ = await self._run(
                root,
                FixedProvider("unused"),
                _candidate("missing.py"),
            )
            identity = next(iter(active.verify_attempts))
            executor = VerifyRecoveryExecutor(
                security_settings_from_app(AppSettings(project_root=root))
            )
            await executor.execute(
                active,
                identity=identity,
                action="pause_unverified",
                target="missing.py",
            )
            with self.assertRaisesRegex(VerifyRecoveryRejected, "already consumed"):
                await executor.execute(
                    active,
                    identity=identity,
                    action="pause_unverified",
                    target="missing.py",
                )

    async def test_deterministic_recovery_uses_configured_handler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text("value = 1\n", encoding="utf-8")
            active, _, _ = await self._run(
                root,
                FixedProvider(LLMError("offline", error_code="provider_error")),
                _candidate("ok.py"),
            )
            identity = next(iter(active.verify_attempts))

            async def deterministic(target: str) -> dict[str, Any]:
                return {"target": target, "status": "passed"}

            executor = VerifyRecoveryExecutor(
                security_settings_from_app(AppSettings(project_root=root)),
                deterministic_check=deterministic,
            )
            result = await executor.execute(
                active,
                identity=identity,
                action="run_deterministic_check",
                target="ok.py",
            )

        self.assertEqual(result.result["status"], "passed")


class VerificationPolicyTests(unittest.TestCase):
    def test_required_unavailable_pauses_while_advisory_is_labeled(self) -> None:
        required = _session()
        required.verify_state["map.gd"] = {
            "policy": "required",
            "status": "unavailable",
            "reason_code": "provider_error",
            "recovery_actions": [{"action": "pause_unverified"}],
        }
        blocked = _apply_verification_policy(required, ChatFinalResponse(text="done"))
        self.assertEqual(blocked.type, "error")
        self.assertEqual(blocked.error_code, "verification_unavailable")

        advisory = _session()
        advisory.verify_state["note.gd"] = {
            "policy": "advisory",
            "status": "unavailable",
            "reason_code": "validator_missing",
            "recovery_actions": [{"action": "pause_unverified"}],
        }
        labeled = _apply_verification_policy(advisory, ChatFinalResponse(text="done"))
        self.assertEqual(labeled.type, "final")
        self.assertIn("[UNVERIFIED]", labeled.text)


if __name__ == "__main__":
    unittest.main()
