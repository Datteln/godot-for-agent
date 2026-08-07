"""当前 Session schema 边界；旧版本不迁移、不读取。"""

from __future__ import annotations

from typing import Any, Final

SESSION_SCHEMA_VERSION: Final = 10
SESSION_SCHEMA_EPOCH: Final = "session-workflow-manifest-v1"


class UnsupportedSessionSchemaError(ValueError):
    """表示磁盘 Session 不属于当前唯一支持的 schema epoch。"""

    error_code: Final = "unsupported_session_schema"


def session_payload_version(payload: dict[str, Any]) -> int:
    """验证并返回唯一支持的当前 Session 版本。"""
    value = payload.get("schema_version")
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnsupportedSessionSchemaError(
            "Session 缺少当前 schema_version；请创建新 Session"
        )
    if value != SESSION_SCHEMA_VERSION:
        raise UnsupportedSessionSchemaError(
            f"Session schema_version={value} 不受支持；当前仅支持 "
            f"{SESSION_SCHEMA_VERSION}，请创建新 Session"
        )
    epoch = payload.get("schema_epoch")
    if epoch != SESSION_SCHEMA_EPOCH:
        raise UnsupportedSessionSchemaError(
            "Session schema epoch 不受支持；请创建新 Session"
        )
    return value


def validate_session_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """验证当前 Session 顶层与 workflow manifest 引用合同。"""
    if not isinstance(payload, dict):
        raise UnsupportedSessionSchemaError("Session payload 必须是对象")
    session_payload_version(payload)
    workflow = payload.get("workflow")
    if not isinstance(workflow, dict):
        raise UnsupportedSessionSchemaError(
            "Session 缺少当前 workflow manifest 引用；请创建新 Session"
        )
    required = {"schema_epoch", "lineage", "manifest_digest", "generation"}
    if set(workflow) != required:
        raise UnsupportedSessionSchemaError(
            "Session workflow manifest 引用结构不受支持；请创建新 Session"
        )
    if (
        not isinstance(workflow.get("lineage"), str)
        or not workflow["lineage"]
        or not isinstance(workflow.get("manifest_digest"), str)
        or len(workflow["manifest_digest"]) != 64
        or isinstance(workflow.get("generation"), bool)
        or not isinstance(workflow.get("generation"), int)
        or workflow["generation"] < 1
    ):
        raise UnsupportedSessionSchemaError(
            "Session workflow manifest 引用值无效；请创建新 Session"
        )
    return payload


__all__ = [
    "SESSION_SCHEMA_EPOCH",
    "SESSION_SCHEMA_VERSION",
    "UnsupportedSessionSchemaError",
    "session_payload_version",
    "validate_session_payload",
]
