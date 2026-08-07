"""会话重置资源的可机读所有权与清理合同。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, Protocol

ResourceLayer = Literal["backend", "persisted", "frontend", "project"]
ResetOwnership = Literal["reset_owned", "preserved"]


@dataclass(frozen=True)
class SessionResourceContract:
    """描述一个按 ``session_id`` 关联资源的生命周期合同。"""

    resource_id: str
    layer: ResourceLayer
    ownership: ResetOwnership
    lifecycle_owner: str
    cleanup_operation: str


SESSION_RESOURCE_CONTRACTS: Final[tuple[SessionResourceContract, ...]] = (
    SessionResourceContract(
        "active_requests",
        "backend",
        "reset_owned",
        "AgentApplication",
        "cancel and serialize on the per-session lock",
    ),
    SessionResourceContract(
        "in_memory_session",
        "backend",
        "reset_owned",
        "SessionStore",
        "remove_session_payload",
    ),
    SessionResourceContract(
        "session_document",
        "persisted",
        "reset_owned",
        "SessionStore",
        "remove_session_payload",
    ),
    SessionResourceContract(
        "task_run_journal",
        "persisted",
        "reset_owned",
        "SessionStore",
        "remove_session_payload",
    ),
    SessionResourceContract(
        "map_artifacts",
        "persisted",
        "reset_owned",
        "MapArtifactStore",
        "clear_session_artifacts",
    ),
    SessionResourceContract(
        "delegate_artifacts",
        "persisted",
        "reset_owned",
        "DelegateArtifactStore",
        "clear_session_artifacts",
    ),
    SessionResourceContract(
        "event_content",
        "backend",
        "reset_owned",
        "EventStore",
        "reset",
    ),
    SessionResourceContract(
        "event_sequence_highwater",
        "backend",
        "preserved",
        "EventStore",
        "preserve monotonic sequence and emit reset boundary",
    ),
    SessionResourceContract(
        "history_projection_cache",
        "backend",
        "reset_owned",
        "AgentApplication",
        "drop the exact (session_id, old_epoch) key",
    ),
    SessionResourceContract(
        "turn_progress",
        "backend",
        "reset_owned",
        "AgentApplication",
        "drop the exact session_id key",
    ),
    SessionResourceContract(
        "recovery_pointer",
        "persisted",
        "reset_owned",
        "RecoveryPointerStore",
        "clear",
    ),
    SessionResourceContract(
        "epoch_barrier",
        "persisted",
        "preserved",
        "SessionStore",
        "replace atomically with the new epoch",
    ),
    SessionResourceContract(
        "reset_record",
        "persisted",
        "preserved",
        "SessionStore",
        "retain the cleaned audit record",
    ),
    SessionResourceContract(
        "frontend_messages",
        "frontend",
        "reset_owned",
        "ChatPanel",
        "clear after reset acknowledgement",
    ),
    SessionResourceContract(
        "frontend_pending_calls",
        "frontend",
        "reset_owned",
        "ChatPanel and AgentStateStore",
        "clear after reset acknowledgement",
    ),
    SessionResourceContract(
        "frontend_file_authorization",
        "frontend",
        "reset_owned",
        "FileStateCache",
        "clear after reset acknowledgement",
    ),
    SessionResourceContract(
        "frontend_event_cursor",
        "frontend",
        "reset_owned",
        "AgentHttpClient and AgentStateStore",
        "adopt acknowledged epoch and cursor",
    ),
    SessionResourceContract(
        "frontend_recovery_ui",
        "frontend",
        "reset_owned",
        "ChatPanel",
        "close after reset acknowledgement",
    ),
    SessionResourceContract(
        "godot_project_content",
        "project",
        "preserved",
        "Godot editor",
        "never touched by session reset",
    ),
    SessionResourceContract(
        "authoritative_revisions",
        "project",
        "preserved",
        "MapRevisionTracker",
        "never touched by session reset",
    ),
    SessionResourceContract(
        "transaction_journals",
        "project",
        "preserved",
        "UnifiedUndoManager",
        "never touched by session reset",
    ),
    SessionResourceContract(
        "registries_indexes_and_blueprints",
        "project",
        "preserved",
        "Godot project services",
        "never touched by session reset",
    ),
    SessionResourceContract(
        "global_configuration_memory_and_rag",
        "project",
        "preserved",
        "application services",
        "never touched by session reset",
    ),
)

SESSION_RESOURCE_BY_ID: Final[dict[str, SessionResourceContract]] = {
    contract.resource_id: contract for contract in SESSION_RESOURCE_CONTRACTS
}

BACKEND_RESET_STEPS: Final[tuple[str, ...]] = (
    "event_content",
    "in_memory_session",
    "session_document",
    "task_run_journal",
    "map_artifacts",
    "delegate_artifacts",
    "recovery_pointer",
    "history_projection_cache",
    "turn_progress",
)

RESET_FAILPOINTS: Final[frozenset[str]] = frozenset(
    {
        "reset_record_after_prepare",
        "epoch_barrier_before_write",
        "epoch_barrier_after_write",
        "reset_record_after_epoch_switch",
        *{
            f"cleanup_{position}_{resource_id}"
            for resource_id in BACKEND_RESET_STEPS
            for position in ("before", "after")
        },
        "reset_record_before_cleaned",
        "reset_record_after_cleaned",
    }
)


class ResetFailureInjector(Protocol):
    """定义仅测试组合可注入的 reset 故障接口。"""

    def hit(self, name: str) -> None:
        """在一个命名 reset 边界触发确定性故障。"""


def validate_session_resource_contracts() -> None:
    """验证资源 id 唯一且所有后端 reset 步骤都有明确合同。"""
    ids = [contract.resource_id for contract in SESSION_RESOURCE_CONTRACTS]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate session resource contract")
    missing = set(BACKEND_RESET_STEPS) - set(ids)
    if missing:
        raise ValueError(f"unclassified backend reset resources: {sorted(missing)}")
    invalid = {
        resource_id
        for resource_id in BACKEND_RESET_STEPS
        if SESSION_RESOURCE_BY_ID[resource_id].ownership != "reset_owned"
    }
    if invalid:
        raise ValueError(f"backend reset steps must be reset-owned: {sorted(invalid)}")


validate_session_resource_contracts()
