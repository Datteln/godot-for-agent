"""Two-phase verification with canonical outcomes and bounded recovery guidance."""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from app.config import AppSettings
from app.llm.provider import LLMError, LLMProvider
from app.orchestrator.turn.model_policy import EFFORT_TEMPERATURE, resolve_thinking_budget
from app.query.helpers import _parse_verify_response, _VERIFY_SYSTEM_PROMPT
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext
from app.tools.server_tools.read_file import read_file_handler
from app.verify.contracts import (
    VerifyIssue,
    VerifyOutcome,
    VerifyRecoveryAction,
    VerifyRetryIdentity,
)
from app.verify.syntax_check import run_syntax_check

logger = logging.getLogger(__name__)


class VerifyRunner:
    """Run every configured verifier without converting unavailability to success."""

    def __init__(
        self,
        settings: AppSettings,
        llm: LLMProvider,
        emit: Callable[[str, str, dict[str, Any]], int],
        model_for_effort: Callable[[str], str | None],
        thinking_budget_for_effort: Callable[[str], int | None],
    ) -> None:
        self._settings = settings
        self._llm = llm
        self._emit = emit
        self._model_for_effort = model_for_effort
        self._thinking_budget_for_effort = thinking_budget_for_effort

    async def run(
        self,
        session: Session,
        security: SecuritySettings,
        candidates: list[dict[str, Any]],
        model_override: str | None = None,
    ) -> list[VerifyOutcome]:
        """Verify candidates sequentially and return each candidate's final outcome."""
        outcomes: list[VerifyOutcome] = []
        for candidate in candidates:
            outcomes.append(
                await self._verify_one(session, security, candidate, model_override)
            )
        return outcomes

    async def _verify_one(
        self,
        session: Session,
        security: SecuritySettings,
        candidate: dict[str, Any],
        model_override: str | None = None,
    ) -> VerifyOutcome:
        tool_use_id = str(candidate.get("tool_use_id", ""))
        frame_id = str(candidate.get("frame_id", ""))
        tool_name = str(candidate.get("tool_name", ""))
        path = str(candidate.get("path", ""))
        max_attempts = max(1, self._settings.verify_max_retries)
        previous_attempts = int(session.verify_retry_count.get(path, 0))
        attempt = previous_attempts + 1
        frame = next((item for item in session.agent_stack if item.id == frame_id), None)

        if frame is None:
            outcome = VerifyOutcome.unavailable(
                phase="semantic",
                reason_code="owning_frame_missing",
                summary=f"无法找到触发校验的 Frame：{frame_id}",
                attempt=min(attempt, max_attempts),
                max_attempts=max_attempts,
                recovery_actions=(
                    VerifyRecoveryAction(action="pause_unverified", target=path),
                ),
            )
            self._emit_outcome(session, candidate, outcome, frame_id=frame_id, message_index=0)
            session.verify_state[path] = {
                "policy": "required",
                "status": outcome.status,
                "reason_code": outcome.reason_code,
                "summary": outcome.summary,
                "recovery_actions": [item.to_payload() for item in outcome.recovery_actions],
                "verified": False,
            }
            self._record_attempt(session, path, b"", outcome)
            return outcome

        if attempt > max_attempts:
            outcome = VerifyOutcome.unavailable(
                phase="semantic",
                reason_code="attempt_budget_exhausted",
                summary="等价校验尝试预算已耗尽，不能通过重复请求重置。",
                attempt=max_attempts,
                max_attempts=max_attempts,
                recovery_actions=(
                    VerifyRecoveryAction(action="pause_unverified", target=path),
                ),
            )
            self._emit_and_inject(session, candidate, frame, outcome)
            self._record_attempt(session, path, b"", outcome)
            return outcome

        if self._settings.verify_syntax_enabled:
            self._emit_started(session, candidate, "syntax", frame.id, len(frame.messages))
            syntax_report = await run_syntax_check(
                path=path,
                project_root=security.project_root,
                godot_path=self._settings.verify_godot_path,
                timeout_s=self._settings.verify_syntax_timeout,
            )
            if syntax_report.status == "unavailable":
                syntax_outcome = VerifyOutcome.unavailable(
                    phase="syntax",
                    reason_code=syntax_report.reason_code,
                    summary=syntax_report.summary,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    recovery_actions=self._recovery_actions(
                        path,
                        attempt,
                        max_attempts,
                        include_deterministic=False,
                    ),
                )
                self._emit_and_inject(session, candidate, frame, syntax_outcome)
            else:
                issues = tuple(
                    VerifyIssue(
                        severity=item.severity,
                        file_path=item.file_path,
                        line=item.line,
                        message=item.message,
                    )
                    for item in syntax_report.issues
                )
                if syntax_report.status == "failed":
                    outcome = VerifyOutcome.failed(
                        phase="syntax",
                        reason_code="syntax_issue",
                        summary=syntax_report.summary,
                        issues=issues,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    self._emit_and_inject(session, candidate, frame, outcome)
                    self._record_attempt(session, path, b"", outcome)
                    session.verify_retry_count[path] = attempt
                    return outcome
                syntax_outcome = VerifyOutcome.passed(
                    phase="syntax",
                    summary="确定性语法检查通过。",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                self._emit_and_inject(session, candidate, frame, syntax_outcome)

        self._emit_started(session, candidate, "semantic", frame.id, len(frame.messages))

        def on_fallback(primary: str, fallback: str) -> None:
            self._emit(
                session.session_id,
                "agent_model_fallback",
                {
                    "frame_id": frame_id,
                    "loop": 0,
                    "primary_model": primary,
                    "fallback_model": fallback,
                    "source": "verify",
                },
            )

        outcome, target_content = await self._run_semantic_verify(
            security,
            tool_name,
            candidate.get("input", {}),
            path,
            attempt,
            max_attempts,
            model_override,
            on_fallback=on_fallback,
        )
        self._emit_and_inject(session, candidate, frame, outcome)
        self._record_attempt(session, path, target_content, outcome)
        session.verify_retry_count[path] = 0 if outcome.status == "passed" else attempt
        logger.info(
            "Verify semantic finished session=%s path=%s status=%s reason=%s issues=%d",
            session.session_id,
            path,
            outcome.status,
            outcome.reason_code,
            len(outcome.issues),
        )
        return outcome

    async def _run_semantic_verify(
        self,
        security: SecuritySettings,
        tool_name: str,
        tool_input: dict[str, Any],
        path: str,
        attempt: int,
        max_attempts: int,
        model_override: str | None = None,
        on_fallback: Callable[[str, str], None] | None = None,
    ) -> tuple[VerifyOutcome, bytes]:
        try:
            file_payload = await read_file_handler(
                {"path": path, "limit": 20000},
                ToolContext(security=security, session_id="verify"),
            )
            file_content = str(file_payload.get("content", ""))
            content_bytes = file_content.encode("utf-8")
        except (OSError, ValueError) as exc:
            logger.warning("Verify target unreadable path=%s error_type=%s", path, type(exc).__name__)
            return (
                VerifyOutcome.unavailable(
                    phase="semantic",
                    reason_code="target_unreadable",
                    summary=f"无法在项目安全边界内读取校验目标：{path}",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    recovery_actions=self._recovery_actions(
                        path,
                        attempt,
                        max_attempts,
                        include_reread=True,
                    ),
                ),
                b"",
            )

        user_payload = {
            "tool_name": tool_name,
            "tool_input_path": tool_input.get("path", path),
            "file_path": path,
            "file_content": file_content,
            "verify_attempt": attempt,
            "verify_max_attempts": max_attempts,
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _VERIFY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        try:
            turn = await self._llm.chat(
                messages,
                [],
                model=model_override or self._model_for_effort(self._settings.verify_effort),
                temperature=EFFORT_TEMPERATURE.get(self._settings.verify_effort, 0.0),
                thinking_budget=resolve_thinking_budget(
                    self._settings.verify_effort,
                    self._thinking_budget_for_effort,
                ),
                on_fallback=on_fallback,
            )
        except LLMError as exc:
            reason_code = (
                "provider_timeout"
                if exc.error_code in {"provider_timeout", "timeout"}
                else "provider_error"
            )
            logger.warning(
                "Verify provider unavailable path=%s reason=%s model=%s attempts=%d",
                path,
                reason_code,
                exc.model,
                exc.wire_attempt_count,
            )
            return (
                VerifyOutcome.unavailable(
                    phase="semantic",
                    reason_code=reason_code,
                    summary=f"语义校验 provider 不可用：{reason_code}",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    recovery_actions=self._recovery_actions(
                        path,
                        attempt,
                        max_attempts,
                        include_deterministic=True,
                    ),
                ),
                content_bytes,
            )

        return (
            _parse_verify_response(
                turn.content or "",
                attempt=attempt,
                max_attempts=max_attempts,
            ),
            content_bytes,
        )

    @staticmethod
    def _recovery_actions(
        target: str,
        attempt: int,
        max_attempts: int,
        *,
        include_reread: bool = False,
        include_deterministic: bool = False,
    ) -> tuple[VerifyRecoveryAction, ...]:
        actions: list[VerifyRecoveryAction] = []
        if attempt < max_attempts:
            if include_reread:
                actions.extend(
                    [
                        VerifyRecoveryAction(action="reread_target", target=target),
                        VerifyRecoveryAction(action="rediscover_target", target=target),
                    ]
                )
            if include_deterministic:
                actions.append(
                    VerifyRecoveryAction(action="run_deterministic_check", target=target)
                )
            actions.append(VerifyRecoveryAction(action="retry_verifier", target=target))
        actions.append(VerifyRecoveryAction(action="pause_unverified", target=target))
        return tuple(actions)

    def _emit_started(
        self,
        session: Session,
        candidate: dict[str, Any],
        phase: str,
        frame_id: str,
        message_index: int,
    ) -> None:
        self._emit(
            session.session_id,
            "verify_started",
            {
                "tool_use_id": str(candidate.get("tool_use_id", "")),
                "file_path": str(candidate.get("path", "")),
                "phase": phase,
                "frame_id": frame_id,
                "message_index": message_index,
            },
        )

    def _emit_outcome(
        self,
        session: Session,
        candidate: dict[str, Any],
        outcome: VerifyOutcome,
        *,
        frame_id: str,
        message_index: int,
    ) -> None:
        self._emit(
            session.session_id,
            "verify_completed",
            {
                "tool_use_id": str(candidate.get("tool_use_id", "")),
                "file_path": str(candidate.get("path", "")),
                "frame_id": frame_id,
                "message_index": message_index,
                "outcome": outcome.to_payload(),
            },
        )

    def _emit_and_inject(
        self,
        session: Session,
        candidate: dict[str, Any],
        frame: Any,
        outcome: VerifyOutcome,
    ) -> None:
        self._emit_outcome(
            session,
            candidate,
            outcome,
            frame_id=str(frame.id),
            message_index=len(frame.messages),
        )
        actions = [item.action for item in outcome.recovery_actions]
        path = str(candidate.get("path", ""))
        frame.messages.append(
            {
                "role": "system",
                "content": json.dumps(
                    {
                        "verify_outcome": outcome.to_payload(),
                        "verify_target": path,
                        "guidance": {
                            "rule": (
                                "Use at most one listed recovery action. Never claim that "
                                "verification passed when status is unavailable."
                            ),
                            "permitted_actions": actions,
                        },
                    },
                    ensure_ascii=False,
                ),
            }
        )
        policy = str(candidate.get("verification_policy", "required"))
        if policy not in {"required", "advisory"}:
            policy = "required"
        session.verify_state[path] = {
            "policy": policy,
            "status": outcome.status,
            "reason_code": outcome.reason_code,
            "summary": outcome.summary,
            "recovery_actions": [item.to_payload() for item in outcome.recovery_actions],
            "verified": outcome.status == "passed",
        }

    @staticmethod
    def _record_attempt(
        session: Session,
        path: str,
        content: bytes,
        outcome: VerifyOutcome,
    ) -> None:
        identity = VerifyRetryIdentity.create(
            target=path,
            content=content,
            phase=outcome.phase,
            root_cause=outcome.reason_code,
        )
        session.verify_attempts[identity.key] = {
            "target": identity.target,
            "target_digest": identity.target_digest,
            "phase": identity.phase,
            "root_cause": identity.root_cause,
            "attempt": outcome.attempt,
            "max_attempts": outcome.max_attempts,
            "remaining_budget": max(outcome.max_attempts - outcome.attempt, 0),
            "permitted_actions": [item.to_payload() for item in outcome.recovery_actions],
            "consumed_actions": [],
            "outcome": outcome.to_payload(),
            "outcome_digest": hashlib.sha256(
                json.dumps(
                    outcome.to_payload(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        }
