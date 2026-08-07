"""Canonical multi-state verification outcome and recovery contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping, TypeAlias, cast

VerifyStatus: TypeAlias = Literal["passed", "failed", "unavailable"]
VerifyPhase: TypeAlias = Literal["syntax", "semantic", "deterministic"]
VerifyReasonCode: TypeAlias = Literal[
    "verified",
    "syntax_issue",
    "semantic_issue",
    "target_unreadable",
    "owning_frame_missing",
    "attempt_budget_exhausted",
    "provider_error",
    "provider_timeout",
    "validator_missing",
    "validator_timeout",
    "response_malformed",
    "unsupported_verify_schema",
]
RecoveryActionName: TypeAlias = Literal[
    "reread_target",
    "rediscover_target",
    "run_deterministic_check",
    "retry_verifier",
    "use_configured_fallback",
    "pause_unverified",
]

_STATUSES = frozenset({"passed", "failed", "unavailable"})
_PHASES = frozenset({"syntax", "semantic", "deterministic"})
_REASONS = frozenset(
    {
        "verified",
        "syntax_issue",
        "semantic_issue",
        "target_unreadable",
        "owning_frame_missing",
        "attempt_budget_exhausted",
        "provider_error",
        "provider_timeout",
        "validator_missing",
        "validator_timeout",
        "response_malformed",
        "unsupported_verify_schema",
    }
)
_ACTIONS = frozenset(
    {
        "reread_target",
        "rediscover_target",
        "run_deterministic_check",
        "retry_verifier",
        "use_configured_fallback",
        "pause_unverified",
    }
)


class UnsupportedVerifySchemaError(ValueError):
    """Raised when a payload is not the sole accepted Verify schema."""

    error_code = "unsupported_verify_schema"


@dataclass(frozen=True, slots=True)
class VerifyIssue:
    severity: Literal["error", "warning", "info"]
    file_path: str
    message: str
    line: int | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "VerifyIssue":
        severity = str(payload.get("severity", ""))
        if severity not in {"error", "warning", "info"}:
            raise UnsupportedVerifySchemaError("invalid Verify issue severity")
        return cls(
            severity=cast(Literal["error", "warning", "info"], severity),
            file_path=str(payload.get("file_path", "")),
            message=str(payload.get("message", "")),
            line=(
                int(payload["line"])
                if isinstance(payload.get("line"), int)
                and not isinstance(payload.get("line"), bool)
                else None
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "file_path": self.file_path,
            "line": self.line,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class VerifyRecoveryAction:
    action: RecoveryActionName
    target: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "VerifyRecoveryAction":
        action = str(payload.get("action", ""))
        if action not in _ACTIONS:
            raise UnsupportedVerifySchemaError(f"unsupported Verify recovery action: {action}")
        return cls(
            action=cast(RecoveryActionName, action),
            target=str(payload.get("target", "")),
        )

    def to_payload(self) -> dict[str, str]:
        return {"action": self.action, "target": self.target}


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """The only accepted persisted, event, and UI verification representation."""

    SCHEMA_VERSION: ClassVar[int] = 1

    schema_version: int
    status: VerifyStatus
    phase: VerifyPhase
    reason_code: VerifyReasonCode
    summary: str
    issues: tuple[VerifyIssue, ...]
    attempt: int
    max_attempts: int
    retryable: bool
    recovery_actions: tuple[VerifyRecoveryAction, ...]

    def __post_init__(self) -> None:
        if self.schema_version != self.SCHEMA_VERSION:
            raise UnsupportedVerifySchemaError("unsupported Verify schema_version")
        if self.status not in _STATUSES or self.phase not in _PHASES:
            raise UnsupportedVerifySchemaError("unsupported Verify status or phase")
        if self.reason_code not in _REASONS:
            raise UnsupportedVerifySchemaError("unsupported Verify reason_code")
        if self.attempt < 1 or self.max_attempts < 1 or self.attempt > self.max_attempts:
            raise UnsupportedVerifySchemaError("invalid Verify attempt budget")
        if self.status == "passed" and (self.issues or self.reason_code != "verified"):
            raise UnsupportedVerifySchemaError("passed Verify outcome must be verified and issue-free")
        if self.status == "failed" and (
            not self.issues or self.reason_code not in {"syntax_issue", "semantic_issue"}
        ):
            raise UnsupportedVerifySchemaError("failed Verify outcome requires defect issues")
        if self.status == "unavailable" and (
            self.issues
            or self.reason_code in {"verified", "syntax_issue", "semantic_issue"}
            or not self.recovery_actions
        ):
            raise UnsupportedVerifySchemaError(
                "unavailable Verify outcome requires a non-defect cause and recovery action"
            )
        if self.status != "unavailable" and self.recovery_actions:
            raise UnsupportedVerifySchemaError(
                "recovery actions are only valid for unavailable verification"
            )
        action_names = [item.action for item in self.recovery_actions]
        if len(action_names) != len(set(action_names)):
            raise UnsupportedVerifySchemaError("duplicate Verify recovery action")

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "VerifyOutcome":
        if "passed" in payload or set(payload) - {
            "schema_version",
            "status",
            "phase",
            "reason_code",
            "summary",
            "issues",
            "attempt",
            "max_attempts",
            "retryable",
            "recovery_actions",
        }:
            raise UnsupportedVerifySchemaError(
                "legacy or extended Verify payload is unsupported"
            )
        try:
            raw_issues = payload["issues"]
            raw_actions = payload["recovery_actions"]
            if not isinstance(raw_issues, list) or not isinstance(raw_actions, list):
                raise TypeError("issues and recovery_actions must be arrays")
            if not all(isinstance(item, Mapping) for item in raw_issues):
                raise TypeError("every Verify issue must be an object")
            if not all(isinstance(item, Mapping) for item in raw_actions):
                raise TypeError("every Verify recovery action must be an object")
            status = str(payload["status"])
            phase = str(payload["phase"])
            reason = str(payload["reason_code"])
            if status not in _STATUSES or phase not in _PHASES or reason not in _REASONS:
                raise ValueError("unknown closed enum member")
            return cls(
                schema_version=int(payload["schema_version"]),
                status=cast(VerifyStatus, status),
                phase=cast(VerifyPhase, phase),
                reason_code=cast(VerifyReasonCode, reason),
                summary=str(payload["summary"]),
                issues=tuple(
                    VerifyIssue.from_payload(item)
                    for item in raw_issues
                    if isinstance(item, Mapping)
                ),
                attempt=int(payload["attempt"]),
                max_attempts=int(payload["max_attempts"]),
                retryable=payload["retryable"] is True,
                recovery_actions=tuple(
                    VerifyRecoveryAction.from_payload(item)
                    for item in raw_actions
                    if isinstance(item, Mapping)
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, UnsupportedVerifySchemaError):
                raise
            raise UnsupportedVerifySchemaError("malformed Verify payload") from exc

    @classmethod
    def passed(
        cls,
        *,
        phase: VerifyPhase,
        summary: str,
        attempt: int,
        max_attempts: int,
    ) -> "VerifyOutcome":
        return cls(
            schema_version=cls.SCHEMA_VERSION,
            status="passed",
            phase=phase,
            reason_code="verified",
            summary=summary,
            issues=(),
            attempt=attempt,
            max_attempts=max_attempts,
            retryable=False,
            recovery_actions=(),
        )

    @classmethod
    def failed(
        cls,
        *,
        phase: VerifyPhase,
        reason_code: Literal["syntax_issue", "semantic_issue"],
        summary: str,
        issues: tuple[VerifyIssue, ...],
        attempt: int,
        max_attempts: int,
    ) -> "VerifyOutcome":
        if not issues:
            raise UnsupportedVerifySchemaError("failed Verify outcome requires issues")
        return cls(
            schema_version=cls.SCHEMA_VERSION,
            status="failed",
            phase=phase,
            reason_code=reason_code,
            summary=summary,
            issues=issues,
            attempt=attempt,
            max_attempts=max_attempts,
            retryable=attempt < max_attempts,
            recovery_actions=(),
        )

    @classmethod
    def unavailable(
        cls,
        *,
        phase: VerifyPhase,
        reason_code: VerifyReasonCode,
        summary: str,
        attempt: int,
        max_attempts: int,
        recovery_actions: tuple[VerifyRecoveryAction, ...],
    ) -> "VerifyOutcome":
        return cls(
            schema_version=cls.SCHEMA_VERSION,
            status="unavailable",
            phase=phase,
            reason_code=reason_code,
            summary=summary,
            issues=(),
            attempt=attempt,
            max_attempts=max_attempts,
                retryable=(
                    attempt < max_attempts
                    and any(item.action != "pause_unverified" for item in recovery_actions)
                ),
            recovery_actions=recovery_actions,
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "status": self.status,
            "phase": self.phase,
            "reason_code": self.reason_code,
            "summary": self.summary,
            "issues": [item.to_payload() for item in self.issues],
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "retryable": self.retryable,
            "recovery_actions": [item.to_payload() for item in self.recovery_actions],
        }


@dataclass(frozen=True, slots=True)
class VerifyRetryIdentity:
    target: str
    target_digest: str
    phase: VerifyPhase
    root_cause: VerifyReasonCode

    @classmethod
    def create(
        cls,
        *,
        target: str,
        content: bytes,
        phase: VerifyPhase,
        root_cause: VerifyReasonCode,
    ) -> "VerifyRetryIdentity":
        return cls(
            target=target,
            target_digest=hashlib.sha256(content).hexdigest(),
            phase=phase,
            root_cause=root_cause,
        )

    @property
    def key(self) -> str:
        canonical = json.dumps(
            {
                "target": self.target,
                "target_digest": self.target_digest,
                "phase": self.phase,
                "root_cause": self.root_cause,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
