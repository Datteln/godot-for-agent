"""Closed, single-use recovery executor for unavailable verification outcomes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from app.security.paths import path_ok
from app.security.settings import SecuritySettings
from app.verify.contracts import RecoveryActionName

if TYPE_CHECKING:
    from app.sessions.store import Session

RecoveryHandler = Callable[[str], Awaitable[dict[str, Any]]]


class VerifyRecoveryRejected(ValueError):
    """A recovery action is unsupported, repeated, stale, or exceeds its budget."""


@dataclass(frozen=True, slots=True)
class VerifyRecoveryResult:
    identity: str
    action: RecoveryActionName
    target: str
    result: dict[str, Any]


class VerifyRecoveryExecutor:
    """Execute one permitted action without granting new path/model/tool authority."""

    def __init__(
        self,
        security: SecuritySettings,
        *,
        deterministic_check: RecoveryHandler | None = None,
        retry_verifier: RecoveryHandler | None = None,
        configured_fallback: RecoveryHandler | None = None,
    ) -> None:
        self._security = security
        self._deterministic_check = deterministic_check
        self._retry_verifier = retry_verifier
        self._configured_fallback = configured_fallback

    async def execute(
        self,
        session: "Session",
        *,
        identity: str,
        action: RecoveryActionName,
        target: str,
    ) -> VerifyRecoveryResult:
        record = session.verify_attempts.get(identity)
        if not isinstance(record, dict):
            raise VerifyRecoveryRejected("unknown verification retry identity")
        if str(record.get("target", "")) != target:
            raise VerifyRecoveryRejected("verification action target does not match identity")
        permitted = {
            str(item.get("action", ""))
            for item in record.get("permitted_actions", [])
            if isinstance(item, dict)
        }
        if action not in permitted:
            raise VerifyRecoveryRejected("verification recovery action is not permitted")
        consumed = record.setdefault("consumed_actions", [])
        if action in consumed:
            raise VerifyRecoveryRejected("verification recovery action was already consumed")
        if action != "pause_unverified" and int(record.get("remaining_budget", 0)) <= 0:
            raise VerifyRecoveryRejected("verification recovery budget is exhausted")
        if action != "pause_unverified" and not path_ok(target, self._security, write=False):
            raise VerifyRecoveryRejected("verification target is outside the project boundary")

        result = await self._execute_action(action, target)
        consumed.append(action)
        if action != "pause_unverified":
            record["remaining_budget"] = max(int(record.get("remaining_budget", 0)) - 1, 0)
        record["last_action"] = action
        record["last_action_result"] = result
        return VerifyRecoveryResult(identity, action, target, result)

    async def _execute_action(
        self,
        action: RecoveryActionName,
        target: str,
    ) -> dict[str, Any]:
        if action == "reread_target":
            full_path = self._security.project_root / target
            content = await asyncio.to_thread(full_path.read_bytes)
            return {"path": target, "bytes": len(content), "readable": True}
        if action == "rediscover_target":
            matches = await asyncio.to_thread(self._rediscover, target)
            return {"target": target, "matches": matches}
        if action == "run_deterministic_check":
            return await self._call_required(self._deterministic_check, action, target)
        if action == "retry_verifier":
            return await self._call_required(self._retry_verifier, action, target)
        if action == "use_configured_fallback":
            return await self._call_required(self._configured_fallback, action, target)
        if action == "pause_unverified":
            return {"paused": True, "verified": False, "target": target}
        raise VerifyRecoveryRejected(f"unsupported verification recovery action: {action}")

    def _rediscover(self, target: str) -> list[str]:
        basename = Path(target).name
        if not basename:
            return []
        matches: list[str] = []
        for candidate in self._security.project_root.rglob(basename):
            relative = candidate.relative_to(self._security.project_root).as_posix()
            if candidate.is_file() and path_ok(relative, self._security, write=False):
                matches.append(relative)
            if len(matches) >= 50:
                break
        return sorted(matches)

    @staticmethod
    async def _call_required(
        handler: RecoveryHandler | None,
        action: RecoveryActionName,
        target: str,
    ) -> dict[str, Any]:
        if handler is None:
            raise VerifyRecoveryRejected(f"recovery handler is not configured: {action}")
        return await handler(target)
