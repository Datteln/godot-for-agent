"""Artifact 读取工具的结构化错误响应。"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".svg"})


def image_artifact_mismatch(
    artifact_ref: str,
    expected_kind: str,
) -> dict[str, Any] | None:
    """若引用明显是图片，返回可恢复的工具类型不匹配响应。"""
    normalized = artifact_ref.strip().replace("\\", "/")
    if PurePosixPath(normalized).suffix.lower() not in _IMAGE_SUFFIXES:
        return None
    return {
        "ok": False,
        "error_code": "incompatible_artifact_kind",
        "artifact_ref": artifact_ref,
        "actual_kind": "image",
        "expected_kind": expected_kind,
        "recommended_tool": "read_image_metadata",
        "hint": (
            "截图只能用 read_image_metadata 做视觉确认；精确坐标、source_id、"
            "atlas 坐标和 revision 请用 describe_map_context/describe_map_region。"
        ),
    }


def artifact_read_error(
    artifact_ref: str,
    expected_kind: str,
    error: OSError | TypeError | ValueError,
) -> dict[str, Any]:
    """把边界、格式和缺失错误转换成不会中断编排的结构化响应。"""
    message = str(error)
    missing = isinstance(error, FileNotFoundError) or "not found" in message.lower()
    error_code = "missing_artifact" if missing else "invalid_artifact_ref"
    return {
        "ok": False,
        "error_code": error_code,
        "artifact_ref": artifact_ref,
        "expected_kind": expected_kind,
        "message": message,
    }
