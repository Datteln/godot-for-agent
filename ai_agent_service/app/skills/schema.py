"""Skill frontmatter 的版本化兼容读取。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Final

SKILL_METADATA_SCHEMA_VERSION: Final = 2
_CANONICAL_METADATA_KEYS: Final[dict[str, str]] = {
    "schema_version": "schema-version",
    "allowed_tools": "allowed-tools",
    "capability_tags": "capability-tags",
    "required_capabilities": "required-capabilities",
    "compatible_roles": "compatible-roles",
    "compatible_stages": "compatible-stages",
    "compatible_modes": "compatible-modes",
}


def skill_metadata_version(metadata: dict[str, Any]) -> int:
    """读取 Skill 元数据版本；无版本的历史 Skill 按 v1 处理。"""
    raw = metadata.get("schema-version", metadata.get("schema_version", 1))
    if isinstance(raw, str) and raw.isdigit():
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ValueError("skill schema_version must be a positive integer")
    if raw > SKILL_METADATA_SCHEMA_VERSION:
        raise ValueError(
            f"skill schema_version={raw} is newer than supported "
            f"version={SKILL_METADATA_SCHEMA_VERSION}"
        )
    return raw


def migrate_skill_metadata(metadata: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """把旧 Skill 元数据一次性规范化为当前键名和 schema 版本。"""
    source_version = skill_metadata_version(metadata)
    migrated = deepcopy(metadata)
    changed = source_version != SKILL_METADATA_SCHEMA_VERSION
    for legacy_key, canonical_key in _CANONICAL_METADATA_KEYS.items():
        if legacy_key not in migrated:
            continue
        if canonical_key not in migrated:
            migrated[canonical_key] = migrated[legacy_key]
        migrated.pop(legacy_key, None)
        changed = True
    if source_version < 2:
        required = migrated.get(
            "required-capabilities",
        )
        tags = migrated.get("capability-tags")
        if not required and isinstance(tags, list):
            migrated["required-capabilities"] = [
                f"domain:{item}" for item in tags if isinstance(item, str) and item
            ]
    migrated["schema-version"] = SKILL_METADATA_SCHEMA_VERSION
    return migrated, changed
