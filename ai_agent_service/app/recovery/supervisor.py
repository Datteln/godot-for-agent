"""持久化任务尝试与统一错误恢复策略。"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Final, Literal, Protocol, cast

if TYPE_CHECKING:
    from app.api.schemas import ChatRequest
    from app.sessions.store import Session

RecoveryDisposition = Literal[
    "continue_agent",
    "retry_same_attempt",
    "retry_new_attempt",
    "retry_new_turn",
    "refresh_and_replan",
    "wait_frontend",
    "pause_for_user",
    "terminal",
]
SideEffectState = Literal[
    "none",
    "not_started",
    "prepared",
    "ambiguous",
    "committed",
    "rolled_back",
]
RetryOwner = Literal["backend", "frontend", "user", "none"]
FailureScope = Literal[
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
]


@dataclass(frozen=True)
class FailurePolicy:
    """一个稳定错误码的恢复合同。"""

    scope: FailureScope
    disposition: RecoveryDisposition
    retryable: bool
    side_effect_state: SideEffectState
    retry_owner: RetryOwner
    budget_key: str
    budget: int
    checkpoint_required: bool
    terminal_condition: str
    base_backoff_ms: int = 0
    max_backoff_ms: int = 0


def _policy(
    scope: FailureScope,
    disposition: RecoveryDisposition,
    retryable: bool,
    side_effect_state: SideEffectState,
    retry_owner: RetryOwner,
    budget_key: str,
    budget: int,
    checkpoint_required: bool,
    terminal_condition: str,
    *,
    base_backoff_ms: int = 0,
    max_backoff_ms: int = 0,
) -> FailurePolicy:
    """构造完整且易审计的失败策略。"""
    return FailurePolicy(
        scope=scope,
        disposition=disposition,
        retryable=retryable,
        side_effect_state=side_effect_state,
        retry_owner=retry_owner,
        budget_key=budget_key,
        budget=budget,
        checkpoint_required=checkpoint_required,
        terminal_condition=terminal_condition,
        base_backoff_ms=base_backoff_ms,
        max_backoff_ms=max_backoff_ms,
    )


FAILURE_POLICIES: Final[dict[str, FailurePolicy]] = {
    "stale_session_epoch": _policy(
        "request",
        "wait_frontend",
        True,
        "none",
        "frontend",
        "frontend_sync",
        3,
        False,
        "frontend adopts current epoch",
    ),
    "invalid_request_shape": _policy(
        "request",
        "wait_frontend",
        True,
        "none",
        "frontend",
        "frontend_correction",
        3,
        False,
        "client supplies a valid request",
    ),
    "invalid_recovery_token": _policy(
        "request",
        "pause_for_user",
        False,
        "none",
        "user",
        "recovery_token",
        0,
        True,
        "a fresh bound recovery identity is issued",
    ),
    "pending_tool_results": _policy(
        "front_tool",
        "wait_frontend",
        True,
        "prepared",
        "frontend",
        "frontend_results",
        3,
        True,
        "matching results arrive or user cancels",
    ),
    "missing_agent_frame": _policy(
        "task",
        "pause_for_user",
        False,
        "none",
        "user",
        "task_integrity",
        0,
        True,
        "a valid checkpoint/frame is restored",
    ),
    "tool_result_batch_mismatch": _policy(
        "front_tool",
        "pause_for_user",
        False,
        "ambiguous",
        "user",
        "front_result_integrity",
        0,
        True,
        "matching idempotent batch is supplied",
    ),
    "tool_result_preflight_failed": _policy(
        "front_tool",
        "wait_frontend",
        True,
        "none",
        "frontend",
        "frontend_results",
        3,
        True,
        "a complete matching batch arrives",
    ),
    "front_tool_result_malformed": _policy(
        "front_tool",
        "wait_frontend",
        True,
        "none",
        "frontend",
        "frontend_results",
        3,
        True,
        "frontend corrects the pending result",
    ),
    "front_tool_result_error": _policy(
        "front_tool",
        "continue_agent",
        True,
        "rolled_back",
        "backend",
        "agent_continuation",
        4,
        True,
        "agent chooses a replacement action",
    ),
    "server_tool_exception": _policy(
        "server_tool",
        "continue_agent",
        True,
        "none",
        "backend",
        "agent_continuation",
        4,
        True,
        "agent handles the typed tool result",
    ),
    "server_tool_protocol_error": _policy(
        "server_tool",
        "continue_agent",
        True,
        "none",
        "backend",
        "agent_continuation",
        4,
        True,
        "agent corrects the tool request",
    ),
    "region_too_large": _policy(
        "server_tool",
        "continue_agent",
        True,
        "none",
        "backend",
        "agent_continuation",
        4,
        True,
        "agent reduces the requested region",
    ),
    "missing_artifact": _policy(
        "server_tool",
        "retry_new_attempt",
        True,
        "none",
        "backend",
        "reader_recovery",
        3,
        True,
        "artifact producer is rerun or reference is replaced",
    ),
    "submission_internal_error": _policy(
        "request",
        "retry_new_attempt",
        True,
        "rolled_back",
        "backend",
        "submission",
        3,
        True,
        "backend retry succeeds or budget pauses",
        base_backoff_ms=25,
        max_backoff_ms=250,
    ),
    "session_persistence_failed": _policy(
        "persistence",
        "retry_same_attempt",
        True,
        "rolled_back",
        "backend",
        "persistence",
        3,
        True,
        "durable save succeeds or budget pauses",
        base_backoff_ms=50,
        max_backoff_ms=500,
    ),
    "map_artifact_publication_failed": _policy(
        "publication",
        "retry_same_attempt",
        True,
        "prepared",
        "backend",
        "publication",
        3,
        True,
        "publication reconciles or becomes ambiguous",
        base_backoff_ms=50,
        max_backoff_ms=500,
    ),
    "map_artifact_turn_identity_conflict": _policy(
        "publication",
        "retry_new_turn",
        True,
        "committed",
        "backend",
        "fresh_turn",
        3,
        True,
        "strictly greater turn commits or budget pauses",
    ),
    "map_revision_conflict": _policy(
        "transaction",
        "refresh_and_replan",
        True,
        "rolled_back",
        "backend",
        "authoritative_refresh",
        3,
        True,
        "authoritative revision is refreshed",
    ),
    "approved_write_batch_required": _policy(
        "transaction",
        "refresh_and_replan",
        True,
        "not_started",
        "backend",
        "authoritative_refresh",
        3,
        True,
        "batch is reread and revalidated",
    ),
    "dependency_binding_failed": _policy(
        "plan_step",
        "refresh_and_replan",
        True,
        "none",
        "backend",
        "plan_recovery",
        3,
        True,
        "new authoritative bindings produce a runnable plan",
    ),
    "result_schema_mismatch": _policy(
        "plan_step",
        "refresh_and_replan",
        True,
        "none",
        "backend",
        "plan_recovery",
        3,
        True,
        "replacement result satisfies the declared schema",
    ),
    "reader_recovery_incomplete": _policy(
        "plan_step",
        "retry_new_attempt",
        True,
        "none",
        "backend",
        "reader_recovery",
        3,
        True,
        "reader supplies the missing canonical inputs",
    ),
    "map_resource_resolution_failed": _policy(
        "plan_step",
        "refresh_and_replan",
        True,
        "none",
        "backend",
        "plan_recovery",
        3,
        True,
        "authoritative resource identity is resolved",
    ),
    "map_resource_ambiguous": _policy(
        "plan_step",
        "pause_for_user",
        False,
        "none",
        "user",
        "plan_terminal",
        0,
        True,
        "user selects one canonical resource",
    ),
    "predecessor_not_succeeded": _policy(
        "plan_step",
        "pause_for_user",
        False,
        "none",
        "user",
        "plan_terminal",
        0,
        True,
        "predecessor reaches a valid terminal outcome",
    ),
    "plan_step_recoverable": _policy(
        "plan_step",
        "retry_new_attempt",
        True,
        "none",
        "backend",
        "plan_recovery",
        3,
        True,
        "step succeeds or recovery exhausts",
        base_backoff_ms=25,
        max_backoff_ms=250,
    ),
    "plan_step_permanent": _policy(
        "plan_step",
        "terminal",
        False,
        "none",
        "none",
        "plan_terminal",
        0,
        True,
        "proven permanent step failure",
    ),
    "provider_primary_failed": _policy(
        "provider",
        "retry_new_attempt",
        True,
        "none",
        "backend",
        "provider",
        1,
        True,
        "configured fallback is attempted",
    ),
    "provider_exhausted": _policy(
        "provider",
        "pause_for_user",
        False,
        "none",
        "user",
        "provider",
        1,
        True,
        "provider availability or model choice changes",
    ),
    "agent_turn_budget_exhausted": _policy(
        "task",
        "pause_for_user",
        False,
        "none",
        "user",
        "agent_turns",
        0,
        True,
        "user explicitly resumes from checkpoint",
    ),
    "response_transport_lost": _policy(
        "transport",
        "pause_for_user",
        True,
        "ambiguous",
        "user",
        "transport",
        1,
        True,
        "client reconnects and observes durable attempt state",
    ),
    "event_delivery_failed": _policy(
        "transport",
        "retry_new_attempt",
        True,
        "committed",
        "backend",
        "event_delivery",
        5,
        True,
        "event delivery resumes from cursor",
        base_backoff_ms=25,
        max_backoff_ms=500,
    ),
    "reset_epoch_barrier_failed": _policy(
        "persistence",
        "pause_for_user",
        True,
        "none",
        "user",
        "reset",
        1,
        False,
        "epoch barrier can be persisted",
    ),
    "ambiguous_commit": _policy(
        "transaction",
        "pause_for_user",
        False,
        "ambiguous",
        "user",
        "ambiguous_commit",
        0,
        True,
        "operator proves commit or rollback",
    ),
    "recovery_budget_exhausted": _policy(
        "task",
        "pause_for_user",
        False,
        "none",
        "user",
        "recovery",
        0,
        True,
        "user explicitly resumes with new authority",
    ),
    "user_stop": _policy(
        "task",
        "pause_for_user",
        True,
        "none",
        "user",
        "user_control",
        1,
        True,
        "user explicitly resumes",
    ),
    "user_cancelled": _policy(
        "task",
        "terminal",
        False,
        "rolled_back",
        "none",
        "user_control",
        0,
        False,
        "explicit cancellation is durable",
    ),
    "completion_gate_blocked": _policy(
        "task",
        "refresh_and_replan",
        True,
        "none",
        "backend",
        "completion_gate",
        3,
        True,
        "Completion Gate becomes allowed",
    ),
    "completion_gate_succeeded": _policy(
        "task",
        "terminal",
        False,
        "committed",
        "none",
        "completion_gate",
        0,
        True,
        "Completion Gate is the authoritative success",
    ),
    "internal_error": _policy(
        "task",
        "pause_for_user",
        False,
        "ambiguous",
        "user",
        "internal",
        0,
        True,
        "operator or a new deployment resolves ambiguity",
    ),
}

RECOVERY_FAILPOINTS: Final[frozenset[str]] = frozenset(
    {
        "attempt_outcome_before_persist",
        "attempt_outcome_after_persist",
        "checkpoint_before_persist",
        "checkpoint_after_persist",
        "disposition_before_persist",
        "disposition_after_persist",
        "retry_token_before_issue",
        "retry_token_after_issue",
        "retry_token_before_consume",
        "retry_token_after_consume",
        "supervisor_before_schedule",
        "supervisor_after_schedule",
        "fresh_turn_before_allocate",
        "fresh_turn_after_allocate",
        "event_delivery_before_publish",
        "event_delivery_after_publish",
        "terminal_cleanup_before",
        "terminal_cleanup_after",
    }
)


def validate_failure_policies() -> None:
    """验证失败清单完整字段、预算和恢复所有权自洽。"""
    dispositions = {
        "continue_agent",
        "retry_same_attempt",
        "retry_new_attempt",
        "retry_new_turn",
        "refresh_and_replan",
        "wait_frontend",
        "pause_for_user",
        "terminal",
    }
    if {policy.disposition for policy in FAILURE_POLICIES.values()} != dispositions:
        raise ValueError("failure policies do not cover every recovery disposition")
    for error_code, policy in FAILURE_POLICIES.items():
        if not error_code or not policy.budget_key or not policy.terminal_condition:
            raise ValueError(f"incomplete failure policy: {error_code}")
        if policy.budget < 0:
            raise ValueError(f"negative recovery budget: {error_code}")
        if policy.base_backoff_ms < 0 or policy.max_backoff_ms < 0:
            raise ValueError(f"negative recovery backoff: {error_code}")
        if policy.max_backoff_ms and policy.base_backoff_ms > policy.max_backoff_ms:
            raise ValueError(f"invalid recovery backoff bound: {error_code}")
        if policy.retry_owner == "frontend" and policy.disposition != "wait_frontend":
            raise ValueError(f"frontend cannot own automatic replay: {error_code}")


validate_failure_policies()


class RecoveryFailureInjector(Protocol):
    """定义仅测试组合可注入的 attempt 恢复故障接口。"""

    def hit(self, name: str) -> None:
        """在命名恢复边界触发确定性故障。"""


def _now() -> str:
    """返回 UTC ISO 时间。"""
    return datetime.now(timezone.utc).isoformat()


def _after_ms(delay_ms: int) -> str:
    """返回延迟指定毫秒后的 UTC ISO 时间。"""
    return (datetime.now(timezone.utc) + timedelta(milliseconds=delay_ms)).isoformat()


def _canonical_input(request: ChatRequest) -> dict[str, Any]:
    """生成可持久化、可比较的规范 attempt 输入。"""
    payload = request.model_dump(mode="json")
    payload.pop("recovery_token", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "payload": payload,
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


class RecoveryTokenError(ValueError):
    """恢复 token 不存在、已使用或与当前任务身份不匹配。"""


class RecoverySupervisor:
    """统一创建 Attempt、分类 problem 并持久化恢复身份。"""

    def __init__(
        self,
        failure_injector: RecoveryFailureInjector | None = None,
        persist_callback: Callable[[Session], None] | None = None,
    ) -> None:
        """构造恢复监督器；故障依赖只能由测试组合显式注入。"""
        self._failure_injector = failure_injector
        self._persist_callback = persist_callback

    def _hit(self, name: str) -> None:
        """触发一个经过白名单验证的恢复 failpoint。"""
        if name not in RECOVERY_FAILPOINTS:
            raise ValueError(f"unknown recovery failpoint: {name}")
        if self._failure_injector is not None:
            self._failure_injector.hit(name)

    def hit_failpoint(self, name: str) -> None:
        """允许宿主在 fresh-turn/event 等外部边界复用监督 failpoint。"""
        self._hit(name)

    def _persist(self, session: Session) -> None:
        """在配置了 durable store 时立即保存当前 TaskRun。"""
        if self._persist_callback is not None:
            self._persist_callback(session)

    @staticmethod
    def policy_for(error_code: str) -> FailurePolicy:
        """返回稳定错误码策略；未知错误统一 fail-closed。"""
        return FAILURE_POLICIES.get(error_code, FAILURE_POLICIES["internal_error"])

    def begin_attempt(self, session: Session, request: ChatRequest) -> dict[str, Any]:
        """为一次 `/chat` 提交创建或续接持久化 Attempt。"""
        current = session.task_run if isinstance(session.task_run, dict) else None
        can_continue = (
            current is not None
            and current.get("session_epoch") == session.session_epoch
            and current.get("status") in {"running", "recovering", "waiting_frontend", "paused"}
            and (request.user_message is None or request.recovery_token is not None)
        )
        if not can_continue:
            lineage_id = (
                session.map_request_scope.lineage_id
                or str(session.map_task_lineage.get("lineage_id", ""))
                or secrets.token_urlsafe(12)
            )
            current = {
                "version": 1,
                "task_id": session.map_task_state.task_id or secrets.token_urlsafe(18),
                "lineage_id": lineage_id,
                "session_id": session.session_id,
                "session_epoch": session.session_epoch,
                "checkpoint_id": self._checkpoint_id(session),
                "first_root_cause": None,
                "attempt_history": [],
                "retry_counts": {},
                "retry_token": None,
                "next_action": None,
                "status": "running",
                "created_at": _now(),
                "updated_at": _now(),
            }
        assert current is not None
        if request.recovery_token is not None:
            self._consume_token(session, current, request.recovery_token)
        self._hit("checkpoint_before_persist")
        attempts = current.setdefault("attempt_history", [])
        attempt = {
            "attempt_id": secrets.token_urlsafe(18),
            "index": len(attempts) + 1,
            "canonical_input": _canonical_input(request),
            "checkpoint_id": self._checkpoint_id(session),
            "started_at": _now(),
            "finished_at": None,
            "error_code": None,
            "disposition": None,
            "side_effect_state": "not_started",
            "status": "running",
        }
        attempts.append(attempt)
        current["current_attempt_id"] = attempt["attempt_id"]
        current["checkpoint_id"] = attempt["checkpoint_id"]
        current["status"] = "running"
        current["updated_at"] = _now()
        session.task_run = current
        self._persist(session)
        self._hit("checkpoint_after_persist")
        return attempt

    def problem(
        self,
        session: Session,
        *,
        error_code: str,
        text: str,
        side_effect_state: SideEffectState | None = None,
        next_action: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """记录 problem 并返回构造 `ChatErrorResponse` 所需的结构化字段。"""
        run = self._require_run(session)
        policy = self.policy_for(error_code)
        counts = run.setdefault("retry_counts", {})
        count = int(counts.get(policy.budget_key, 0)) + 1
        counts[policy.budget_key] = count
        disposition = policy.disposition
        retryable = policy.retryable
        if retryable and policy.budget >= 0 and count > policy.budget:
            disposition = "pause_for_user"
            retryable = False
        effect = side_effect_state or policy.side_effect_state
        disposition, retryable = self._reconcile_disposition(
            disposition,
            retryable,
            effect,
        )
        root_cause = {
            "error_code": error_code,
            "text": text,
            "attempt_id": run.get("current_attempt_id"),
            "recorded_at": _now(),
        }
        if run.get("first_root_cause") is None:
            run["first_root_cause"] = root_cause
        attempt = self._current_attempt(run)
        if policy.checkpoint_required and run.get("checkpoint_id") is None:
            run["checkpoint_id"] = f"attempt:{attempt['attempt_id']}"
            attempt["checkpoint_id"] = run["checkpoint_id"]
        self._hit("attempt_outcome_before_persist")
        attempt.update(
            {
                "finished_at": _now(),
                "error_code": error_code,
                "disposition": disposition,
                "side_effect_state": effect,
                "status": "failed",
            }
        )
        self._persist(session)
        self._hit("attempt_outcome_after_persist")
        token: str | None = None
        if retryable and disposition in {
            "retry_same_attempt",
            "retry_new_attempt",
            "retry_new_turn",
            "refresh_and_replan",
        }:
            self._hit("retry_token_before_issue")
            token = secrets.token_urlsafe(32)
            run["retry_token"] = {
                "sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                "used": False,
                "session_epoch": session.session_epoch,
                "task_id": run["task_id"],
                "checkpoint_id": run.get("checkpoint_id"),
                "side_effect_state": effect,
                "permitted_disposition": disposition,
                "canonical_attempt_sha256": attempt.get("canonical_input", {}).get("sha256"),
                "issued_at": _now(),
            }
            self._persist(session)
            self._hit("retry_token_after_issue")
        action = dict(next_action or {})
        action.setdefault("owner", policy.retry_owner)
        action.setdefault("retry_count", count)
        action.setdefault("retry_budget", policy.budget)
        action.setdefault("terminal_condition", policy.terminal_condition)
        delay_ms = self._backoff_ms(policy, count)
        if delay_ms:
            action.setdefault("backoff_ms", delay_ms)
            action.setdefault("not_before", _after_ms(delay_ms))
        self._hit("disposition_before_persist")
        run["next_action"] = action
        run["active_disposition"] = disposition
        run["side_effect_state"] = effect
        run["status"] = (
            "paused"
            if disposition == "pause_for_user"
            else (
                "waiting_frontend"
                if disposition == "wait_frontend"
                else "terminal" if disposition == "terminal" else "recovering"
            )
        )
        run["updated_at"] = _now()
        self._persist(session)
        self._hit("disposition_after_persist")
        self._hit("supervisor_before_schedule")
        run["supervisor_schedule"] = {
            "disposition": disposition,
            "owner": action["owner"],
            "not_before": action.get("not_before"),
            "scheduled_at": _now(),
        }
        self._persist(session)
        self._hit("supervisor_after_schedule")
        return {
            "text": text,
            "error_code": error_code,
            "task_id": str(run["task_id"]),
            "attempt_id": str(run["current_attempt_id"]),
            "checkpoint_id": run.get("checkpoint_id"),
            "disposition": disposition,
            "retryable": retryable,
            "side_effect_state": effect,
            "retry_token": token,
            "next_action": action,
        }

    def resume_after_restart(self, session: Session) -> dict[str, Any] | None:
        """恢复当前 epoch 中尚未完成的监督调度，不创建重复 Attempt。"""
        if not isinstance(session.task_run, dict):
            return None
        run = session.task_run
        if run.get("session_epoch") != session.session_epoch:
            return None
        status = str(run.get("status", ""))
        if status == "running":
            attempt = self._current_attempt(run)
            if attempt.get("status") == "failed":
                error_code = str(attempt.get("error_code") or "internal_error")
                policy = self.policy_for(error_code)
                effect_value = str(attempt.get("side_effect_state") or "ambiguous")
                if effect_value not in {
                    "none",
                    "not_started",
                    "prepared",
                    "ambiguous",
                    "committed",
                    "rolled_back",
                }:
                    effect_value = "ambiguous"
                effect = cast(SideEffectState, effect_value)
                disposition, retryable = self._reconcile_disposition(
                    policy.disposition,
                    policy.retryable,
                    effect,
                )
                if not retryable and disposition not in {"terminal", "wait_frontend"}:
                    disposition = "pause_for_user"
                run["active_disposition"] = disposition
                run["side_effect_state"] = effect
                run["status"] = (
                    "waiting_frontend"
                    if disposition == "wait_frontend"
                    else (
                        "terminal"
                        if disposition == "terminal"
                        else "paused" if disposition == "pause_for_user" else "recovering"
                    )
                )
            else:
                run["active_disposition"] = "pause_for_user"
                run["side_effect_state"] = "ambiguous"
                run["status"] = "paused"
                run["next_action"] = {
                    "action": "reconcile_interrupted_attempt",
                    "owner": "user",
                }
            run["updated_at"] = _now()
            self._persist(session)
            status = str(run["status"])
        if status not in {"recovering", "waiting_frontend", "paused"}:
            return None
        disposition_value = str(run.get("active_disposition", "pause_for_user"))
        if disposition_value not in {
            "continue_agent",
            "retry_same_attempt",
            "retry_new_attempt",
            "retry_new_turn",
            "refresh_and_replan",
            "wait_frontend",
            "pause_for_user",
            "terminal",
        }:
            disposition_value = "pause_for_user"
        disposition = cast(RecoveryDisposition, disposition_value)
        if disposition == "pause_for_user" and run.get("active_disposition") != disposition:
            run["active_disposition"] = disposition
            run["status"] = "paused"
        schedule = run.get("supervisor_schedule")
        if isinstance(schedule, dict) and bool(schedule.get("resumed_after_restart", False)):
            return None
        if not isinstance(schedule, dict):
            run["supervisor_schedule"] = {
                "disposition": disposition,
                "owner": "user" if disposition == "pause_for_user" else "backend",
                "not_before": None,
                "scheduled_at": _now(),
                "resumed_after_restart": True,
            }
        else:
            schedule["resumed_after_restart"] = True
            schedule["resumed_at"] = _now()
        run["updated_at"] = _now()
        self._persist(session)
        return run

    def record_transport_loss(self, session: Session, *, transport: str) -> None:
        """记录响应/事件传输丢失，但不改变 durable task 生命周期。"""
        if not isinstance(session.task_run, dict):
            return
        run = session.task_run
        events = run.setdefault("transport_history", [])
        if isinstance(events, list):
            events.append(
                {
                    "error_code": (
                        "event_delivery_failed"
                        if transport == "event"
                        else "response_transport_lost"
                    ),
                    "transport": transport,
                    "recorded_at": _now(),
                    "attempt_id": run.get("current_attempt_id"),
                }
            )
        run["updated_at"] = _now()
        self._persist(session)

    def force_pause(
        self,
        session: Session,
        problem: dict[str, Any],
        *,
        action: str,
        reason: str,
    ) -> dict[str, Any]:
        """当运行时证据使自动动作不再安全时收紧为持久 pause。"""
        run = self._require_run(session)
        attempt = self._current_attempt(run)
        next_action = {
            "action": action,
            "owner": "user",
            "reason": reason,
        }
        attempt["disposition"] = "pause_for_user"
        run["active_disposition"] = "pause_for_user"
        run["status"] = "paused"
        run["retry_token"] = None
        run["next_action"] = next_action
        run["supervisor_schedule"] = {
            "disposition": "pause_for_user",
            "owner": "user",
            "not_before": None,
            "scheduled_at": _now(),
        }
        run["updated_at"] = _now()
        self._persist(session)
        tightened = dict(problem)
        tightened.update(
            {
                "disposition": "pause_for_user",
                "retryable": False,
                "retry_token": None,
                "next_action": next_action,
            }
        )
        return tightened

    def mark_terminal(
        self,
        session: Session,
        *,
        outcome: Literal["completed", "cancelled", "failed_permanently"],
        authorized_by: str,
    ) -> None:
        """记录由 Completion Gate、显式取消或永久失败授权的终态。"""
        run = self._require_run(session)
        if outcome == "completed" and authorized_by != "completion_gate":
            raise ValueError("completed task requires Completion Gate authorization")
        if outcome == "cancelled" and authorized_by != "explicit_cancel":
            raise ValueError("cancelled task requires explicit cancellation")
        self._hit("terminal_cleanup_before")
        run["status"] = outcome
        run["terminal_authority"] = authorized_by
        run["retry_token"] = None
        run["next_action"] = None
        run["updated_at"] = _now()
        self._persist(session)
        self._hit("terminal_cleanup_after")

    def complete_attempt(self, session: Session, *, waiting_frontend: bool) -> None:
        """把当前 Attempt 标记为成功或等待前端副作用。"""
        run = self._require_run(session)
        attempt = self._current_attempt(run)
        attempt["finished_at"] = _now()
        attempt["status"] = "waiting_frontend" if waiting_frontend else "succeeded"
        attempt["side_effect_state"] = "prepared" if waiting_frontend else "committed"
        run["status"] = "waiting_frontend" if waiting_frontend else "succeeded"
        run["updated_at"] = _now()
        self._persist(session)

    @staticmethod
    def _backoff_ms(policy: FailurePolicy, count: int) -> int:
        """按策略计算有上界的指数退避。"""
        if policy.base_backoff_ms <= 0:
            return 0
        delay = policy.base_backoff_ms * (2 ** max(0, count - 1))
        return int(min(delay, policy.max_backoff_ms or delay))

    @staticmethod
    def _reconcile_disposition(
        disposition: RecoveryDisposition,
        retryable: bool,
        effect: SideEffectState,
    ) -> tuple[RecoveryDisposition, bool]:
        """在调度前按副作用证据收紧不安全的自动恢复。"""
        if effect == "ambiguous":
            return "pause_for_user", False
        if disposition == "continue_agent" and effect not in {"none", "not_started", "rolled_back"}:
            return "pause_for_user", False
        if disposition in {"retry_same_attempt", "retry_new_attempt"} and effect not in {
            "none",
            "not_started",
            "rolled_back",
        }:
            return "pause_for_user", False
        if disposition == "retry_new_turn" and effect != "committed":
            return "pause_for_user", False
        if disposition == "wait_frontend" and effect == "committed":
            return "pause_for_user", False
        return disposition, retryable

    @staticmethod
    def _checkpoint_id(session: Session) -> str | None:
        """提取稳定 checkpoint id；旧检查点无 id 时按内容生成摘要。"""
        checkpoint = session.map_task_state.checkpoint
        if not isinstance(checkpoint, dict) or not checkpoint:
            return None
        explicit = checkpoint.get("checkpoint_id")
        if isinstance(explicit, str) and explicit:
            return explicit
        canonical = json.dumps(
            checkpoint,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def _require_run(session: Session) -> dict[str, Any]:
        """返回当前 TaskRun；调用顺序错误时显式失败。"""
        if not isinstance(session.task_run, dict):
            raise ValueError("recovery attempt has not been started")
        return session.task_run

    @staticmethod
    def _current_attempt(run: dict[str, Any]) -> dict[str, Any]:
        """返回 TaskRun 当前 Attempt。"""
        current_id = run.get("current_attempt_id")
        attempts = run.get("attempt_history")
        if isinstance(attempts, list):
            for attempt in reversed(attempts):
                if isinstance(attempt, dict) and attempt.get("attempt_id") == current_id:
                    return attempt
        raise ValueError("current recovery attempt is missing")

    def _consume_token(
        self,
        session: Session,
        run: dict[str, Any],
        token: str,
    ) -> None:
        """验证并一次性消费绑定当前 TaskRun 的恢复 token。"""
        metadata = run.get("retry_token")
        if not isinstance(metadata, dict) or bool(metadata.get("used", False)):
            raise RecoveryTokenError("recovery token is missing or already used")
        actual = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expected = str(metadata.get("sha256", ""))
        if not hmac.compare_digest(actual, expected):
            raise RecoveryTokenError("recovery token does not match the active task")
        if metadata.get("session_epoch") != run.get("session_epoch"):
            raise RecoveryTokenError("recovery token belongs to another session epoch")
        if metadata.get("task_id") != run.get("task_id"):
            raise RecoveryTokenError("recovery token belongs to another task")
        if metadata.get("checkpoint_id") != run.get("checkpoint_id"):
            raise RecoveryTokenError("recovery token belongs to another checkpoint")
        if metadata.get("side_effect_state") != run.get("side_effect_state"):
            raise RecoveryTokenError("recovery token side-effect state changed")
        if metadata.get("permitted_disposition") != run.get("active_disposition"):
            raise RecoveryTokenError("recovery token disposition changed")
        current = self._current_attempt(run)
        canonical = current.get("canonical_input")
        canonical_sha = canonical.get("sha256") if isinstance(canonical, dict) else None
        if metadata.get("canonical_attempt_sha256") != canonical_sha:
            raise RecoveryTokenError("recovery token attempt identity changed")
        self._hit("retry_token_before_consume")
        metadata["used"] = True
        metadata["consumed_at"] = _now()
        self._persist(session)
        self._hit("retry_token_after_consume")
