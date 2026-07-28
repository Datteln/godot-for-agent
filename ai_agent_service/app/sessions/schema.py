"""会话持久化版本与旧载荷的一次性迁移。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final

SESSION_SCHEMA_VERSION: Final = 4

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

    migrated["map_task_state"] = map_state
    migrated["schema_version"] = SESSION_SCHEMA_VERSION
    return migrated, True
