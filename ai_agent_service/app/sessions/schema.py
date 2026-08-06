"""会话持久化版本与旧载荷的一次性迁移。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final

SESSION_SCHEMA_VERSION: Final = 9

_LEGACY_MAP_FIELDS: Final[dict[str, str]] = {
    "map_completion_blockers": "completion_blockers",
    "latest_map_validations": "latest_validations",
    "map_validation_failure_counts": "validation_failure_counts",
    "map_validation_contracts": "validation_contracts",
    "map_validation_workflows": "validation_workflows",
    "map_no_progress_streaks": "no_progress_streaks",
    "latest_map_revisions": "latest_revisions",
    "latest_map_layers": "latest_layers",
    "latest_map_region_reads": "region_reads",
    "latest_map_region_summaries": "region_summaries",
    "map_context_state": "context_state",
}


def session_payload_version(payload: dict[str, Any]) -> int:
    """读取会话载荷版本；无版本的历史文件按 v1 处理。"""
    value = payload.get("schema_version", 1)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("session schema_version must be an integer")
    if value < 1:
        raise ValueError("session schema_version must be positive")
    if value > SESSION_SCHEMA_VERSION:
        raise ValueError(
            f"session schema_version={value} is newer than supported "
            f"version={SESSION_SCHEMA_VERSION}"
        )
    return value


def migrate_session_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """把历史会话载荷迁移到当前 schema，并返回是否发生升级。"""
    source_version = session_payload_version(payload)
    migrated = deepcopy(payload)
    if source_version == SESSION_SCHEMA_VERSION:
        return migrated, False

    raw_state = migrated.get("map_task_state")
    map_state: dict[str, Any] = dict(raw_state) if isinstance(raw_state, dict) else {}
    if source_version < 2:
        for legacy_name, state_name in _LEGACY_MAP_FIELDS.items():
            legacy_value = migrated.pop(legacy_name, None)
            if legacy_value is None:
                continue
            current_value = map_state.get(state_name)
            if current_value in (None, {}, []):
                map_state[state_name] = legacy_value

        legacy_iterations = migrated.pop("map_auto_iterations", 0)
        if (
            isinstance(legacy_iterations, int)
            and not isinstance(legacy_iterations, bool)
            and legacy_iterations > int(map_state.get("auto_iterations", 0) or 0)
        ):
            map_state["auto_iterations"] = legacy_iterations

    if source_version < 3:
        map_state.setdefault("workflow_schema_version", 1)
        map_state.setdefault("workflow_events", [])
        map_state.setdefault("workflow_scopes", {})
        map_state.setdefault("evidence_registry", {})
        map_state.setdefault("retry_registry", {})
        map_state.setdefault("transaction_journals", [])
        map_state.setdefault("pause_report", {})

    if source_version < 4:
        migrated.setdefault("map_request_scope", {})
        migrated.setdefault("map_task_lineage", {})
        raw_frames = migrated.get("agent_stack")
        if isinstance(raw_frames, list):
            for raw_frame in raw_frames:
                if not isinstance(raw_frame, dict):
                    continue
                raw_frame.setdefault("map_request_lineage_id", None)
                raw_frame.setdefault("map_task_id", None)

    if source_version < 5:
        legacy_pause_reason = str(map_state.get("pause_reason", ""))
        pause_kind = {
            "user_interrupted": "user_interrupted",
            "client_timeout": "client_timeout",
            "provider_exhausted": "provider_exhausted",
            "budget_exhausted": "budget_exhausted",
        }.get(legacy_pause_reason)
        if pause_kind is None:
            pause_kind = (
                "no_progress_exhausted"
                if map_state.get("status") == "paused" and bool(map_state.get("pause_report"))
                else ""
            )
        map_state.setdefault("pause_kind", pause_kind)
        checkpoint = map_state.get("checkpoint")
        if isinstance(checkpoint, dict) and pause_kind:
            checkpoint.setdefault("pause_kind", pause_kind)

    if source_version < 6:
        legacy_resume = map_state.pop("resumed_from_checkpoint", False)
        task_id = str(map_state.get("task_id", ""))
        raw_lineage = migrated.get("map_task_lineage")
        lineage = raw_lineage if isinstance(raw_lineage, dict) else {}
        lineage_id = str(lineage.get("lineage_id", "")) or task_id
        if (
            legacy_resume is True
            and map_state.get("status") == "running"
            and task_id
            and lineage_id
        ):
            map_state["resume_authorization"] = {
                "task_id": task_id,
                "lineage_id": lineage_id,
            }
        else:
            map_state.setdefault("resume_authorization", None)

    if source_version < 7:
        raw_groups = migrated.get("delegate_groups")
        if isinstance(raw_groups, dict):
            for raw_group in raw_groups.values():
                if not isinstance(raw_group, dict):
                    continue
                legacy_remaining = raw_group.pop("remaining", None)
                if (
                    isinstance(legacy_remaining, list)
                    and legacy_remaining
                    and not isinstance(raw_group.get("scheduler_plan"), dict)
                ):
                    raw_group["migration_error"] = (
                        "legacy delegate group has no scheduler graph; "
                        "pending children were blocked during migration"
                    )

    if source_version < 8:
        # `session_epoch` 由 SessionStore 的独立 epoch barrier 补齐。这里保留空值，
        # 让旧会话可以在首次加载时安全绑定到已经持久化的当前 epoch。
        migrated.setdefault("session_epoch", "")
        migrated.setdefault("task_run", None)

    if source_version < 9:
        raw_frames = migrated.get("agent_stack")
        if isinstance(raw_frames, list):
            for raw_frame in raw_frames:
                if isinstance(raw_frame, dict):
                    raw_frame.setdefault("domain_owner_contract", {})

    migrated["map_task_state"] = map_state
    migrated["schema_version"] = SESSION_SCHEMA_VERSION
    return migrated, True
