"""定义地图规划参考上下文与确定性执行范围。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


class MapPlanningContextError(ValueError):
    """表示规划上下文、上下文集合或执行操作不满足类型合同。"""


def _required_text(value: Any, field_name: str) -> str:
    """返回规范化非空文本，不合法时抛出合同错误。"""
    if not isinstance(value, str) or not value.strip():
        raise MapPlanningContextError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_int(value: Any, field_name: str) -> int | None:
    """返回可选非布尔整数，不合法时抛出合同错误。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MapPlanningContextError(f"{field_name} must be an integer")
    return value


def _canonical_target(value: Any) -> str | None:
    """校验规范地图目标，拒绝带 layer/revision 装饰的内部索引键。"""
    if value is None:
        return None
    target = _required_text(value, "target_path")
    if "::map_layer=" in target or "::revision=" in target:
        raise MapPlanningContextError(
            "target_path must be canonical and cannot contain scope decorations"
        )
    return target


def _integer_region(value: Any) -> dict[str, int] | None:
    """规范化可选整数区域字典。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MapPlanningContextError("region must be an object")
    region: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int):
            raise MapPlanningContextError("region values must be integers")
        region[str(key)] = item
    return region


@dataclass(frozen=True)
class MapPlanningContextEntry:
    """保存一条可独立刷新、只具读取权限的地图规划上下文。"""

    context_id: str
    semantic_role: str
    artifact_ref: str
    digest: str
    provenance: dict[str, Any]
    target_path: str | None = None
    map_layer: int | None = None
    region: dict[str, int] | None = None
    source_revision: int | None = None
    fact_fields: tuple[str, ...] = ()
    fresh: bool = True

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MapPlanningContextEntry:
        """从字典校验并恢复一条规划上下文。"""
        raw_fields = value.get("fact_fields", ())
        if not isinstance(raw_fields, (list, tuple)) or not all(
            isinstance(item, str) and item.strip() for item in raw_fields
        ):
            raise MapPlanningContextError("fact_fields must contain non-empty strings")
        provenance = value.get("provenance", {})
        if not isinstance(provenance, dict):
            raise MapPlanningContextError("provenance must be an object")
        fresh = value.get("fresh", True)
        if not isinstance(fresh, bool):
            raise MapPlanningContextError("fresh must be a boolean")
        return cls(
            context_id=_required_text(
                value.get("context_id", value.get("snapshot_id")), "context_id"
            ),
            semantic_role=_required_text(
                value.get("semantic_role", "map_reference"), "semantic_role"
            ),
            artifact_ref=_required_text(value.get("artifact_ref"), "artifact_ref"),
            digest=_required_text(value.get("digest"), "digest"),
            provenance=dict(provenance),
            target_path=_canonical_target(value.get("target_path")),
            map_layer=_optional_int(value.get("map_layer"), "map_layer"),
            region=_integer_region(value.get("region")),
            source_revision=_optional_int(
                value.get("source_revision", value.get("map_revision")),
                "source_revision",
            ),
            fact_fields=tuple(dict.fromkeys(item.strip() for item in raw_fields)),
            fresh=fresh,
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: dict[str, Any],
        *,
        semantic_role: str = "map_reference",
    ) -> MapPlanningContextEntry:
        """把旧权威快照迁移为一条规划上下文。"""
        value = dict(snapshot)
        value.setdefault("context_id", value.get("snapshot_id"))
        value.setdefault("semantic_role", semantic_role)
        value.setdefault(
            "provenance",
            {
                "kind": "authoritative_map_snapshot_v1",
                "snapshot_id": value.get("snapshot_id"),
            },
        )
        value.setdefault("source_revision", value.get("map_revision"))
        value.setdefault(
            "fact_fields",
            ["coverage", "occupancy", "traversal", "entry", "reachable_frontier"],
        )
        return cls.from_dict(value)

    def to_dict(self) -> dict[str, Any]:
        """把规划上下文序列化为稳定 JSON 字典。"""
        return {
            "context_id": self.context_id,
            "semantic_role": self.semantic_role,
            "artifact_ref": self.artifact_ref,
            "digest": self.digest,
            "provenance": dict(self.provenance),
            "target_path": self.target_path,
            "map_layer": self.map_layer,
            "region": dict(self.region) if self.region is not None else None,
            "source_revision": self.source_revision,
            "fact_fields": list(self.fact_fields),
            "fresh": self.fresh,
        }


@dataclass(frozen=True)
class MapPlanningContextBundle:
    """保存一次 planner 调用冻结绑定的有序规划上下文集合。"""

    bundle_id: str
    contexts: tuple[MapPlanningContextEntry, ...]
    required_roles: tuple[str, ...]

    @classmethod
    def from_entries(
        cls,
        entries: Iterable[MapPlanningContextEntry],
        *,
        required_roles: Iterable[str] = (),
    ) -> MapPlanningContextBundle:
        """从上下文条目构造带稳定摘要身份的集合。"""
        contexts = tuple(entries)
        if not contexts:
            raise MapPlanningContextError("planning context bundle cannot be empty")
        context_ids = [entry.context_id for entry in contexts]
        if len(context_ids) != len(set(context_ids)):
            raise MapPlanningContextError("planning context ids must be unique")
        normalized_roles = tuple(
            dict.fromkeys(_required_text(role, "required_role") for role in required_roles)
        )
        available_roles = {entry.semantic_role for entry in contexts}
        missing_roles = set(normalized_roles) - available_roles
        if missing_roles:
            raise MapPlanningContextError(
                "planning context bundle is missing roles: " + ",".join(sorted(missing_roles))
            )
        payload = [entry.to_dict() for entry in contexts]
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        return cls(
            bundle_id=f"map-context-bundle:{digest}",
            contexts=contexts,
            required_roles=normalized_roles,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MapPlanningContextBundle:
        """从字典校验并恢复规划上下文集合。"""
        raw_contexts = value.get("contexts")
        if not isinstance(raw_contexts, list):
            raise MapPlanningContextError("planning context bundle requires contexts")
        contexts = tuple(
            MapPlanningContextEntry.from_dict(item)
            for item in raw_contexts
            if isinstance(item, dict)
        )
        if len(contexts) != len(raw_contexts):
            raise MapPlanningContextError("planning contexts must be objects")
        raw_roles = value.get("required_roles", ())
        if not isinstance(raw_roles, (list, tuple)):
            raise MapPlanningContextError("required_roles must be an array")
        generated = cls.from_entries(contexts, required_roles=raw_roles)
        raw_bundle_id = value.get("bundle_id")
        if raw_bundle_id is None:
            return generated
        bundle_id = _required_text(raw_bundle_id, "bundle_id")
        if bundle_id != generated.bundle_id:
            raise MapPlanningContextError("planning context bundle digest is invalid")
        return generated

    def to_dict(self) -> dict[str, Any]:
        """把规划上下文集合序列化为稳定 JSON 字典。"""
        return {
            "bundle_id": self.bundle_id,
            "contexts": [entry.to_dict() for entry in self.contexts],
            "required_roles": list(self.required_roles),
        }


@dataclass(frozen=True)
class MapExecutionOperation:
    """描述一个确定性编译、仅绑定单一地图执行范围的操作。"""

    operation_id: str
    target_path: str
    map_layer: int
    expected_revision: int
    write_payload: dict[str, Any]
    artifact_ref: str | None = None
    batch_id: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MapExecutionOperation:
        """从字典校验并恢复一个确定性执行操作。"""
        payload = value.get("write_payload")
        if not isinstance(payload, dict) or not payload:
            raise MapPlanningContextError("write_payload must be a non-empty object")
        target = _canonical_target(value.get("target_path"))
        if target is None:
            raise MapPlanningContextError("execution operation requires target_path")
        map_layer = _optional_int(value.get("map_layer"), "map_layer")
        revision = _optional_int(value.get("expected_revision"), "expected_revision")
        if map_layer is None or revision is None:
            raise MapPlanningContextError(
                "execution operation requires map_layer and expected_revision"
            )
        artifact_ref = value.get("artifact_ref")
        batch_id = value.get("batch_id")
        return cls(
            operation_id=_required_text(value.get("operation_id"), "operation_id"),
            target_path=target,
            map_layer=map_layer,
            expected_revision=revision,
            write_payload=dict(payload),
            artifact_ref=(
                _required_text(artifact_ref, "artifact_ref") if artifact_ref is not None else None
            ),
            batch_id=_required_text(batch_id, "batch_id") if batch_id is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        """把执行操作序列化为稳定 JSON 字典。"""
        return {
            "operation_id": self.operation_id,
            "target_path": self.target_path,
            "map_layer": self.map_layer,
            "expected_revision": self.expected_revision,
            "write_payload": dict(self.write_payload),
            "artifact_ref": self.artifact_ref,
            "batch_id": self.batch_id,
        }
