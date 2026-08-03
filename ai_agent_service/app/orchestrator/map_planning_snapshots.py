"""地图规划权威快照的合同、投影与持久化。"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Final

from app.storage.atomic import atomic_write_json

AUTHORITATIVE_MAP_SNAPSHOT_SCHEMA: Final = "authoritative_map_snapshot_v1"
APPROVED_MAP_BATCH_SCHEMA: Final = "approved_map_batch_v1"
PLANNING_REPAIR_SCHEMA: Final = "planning_repair_v1"
MAX_PLANNING_SNAPSHOT_BYTES: Final = 24 * 1024 * 1024
MAX_PLANNER_PROJECTION_CELLS: Final = 512
MAX_PLANNING_SNAPSHOT_PAGE_ITEMS: Final = 200
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _canonical_json(value: Any) -> str:
    """把 JSON 原生值序列化为稳定文本。"""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    """返回 JSON 原生值的完整 SHA-256 摘要。"""
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _snapshot_fact_payload(snapshot: AuthoritativeMapSnapshot) -> dict[str, Any]:
    """提取决定快照身份的事实，排除定位来源和派生 id。"""
    payload = asdict(snapshot)
    payload.pop("snapshot_id", None)
    payload.pop("evidence_sources", None)
    return payload


def _safe_name(value: str) -> str:
    """把会话或快照标识规整为不可逃逸的文件名。"""
    cleaned = _SAFE_NAME_RE.sub("-", value).strip(".-")
    return cleaned[:96] if cleaned else _digest(value)[:24]


def _whole_int(value: Any, default: int = 0) -> int:
    """把非布尔整数安全规整为 int。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _normalized_layer(tool_args: dict[str, Any], result: dict[str, Any]) -> int:
    """从工具结果或调用参数解析 legacy TileMap 图层。"""
    value = result.get("map_layer", tool_args.get("map_layer", 0))
    return _whole_int(value)


def _normalized_target(tool_args: dict[str, Any], result: dict[str, Any]) -> str:
    """从工具结果或调用参数解析 canonical 地图目标。"""
    value = result.get("target", result.get("target_path", tool_args.get("target_path", "")))
    return value.strip() if isinstance(value, str) else ""


def _normalized_revision(result: dict[str, Any]) -> int:
    """从前端 canonical map read 结果解析 revision。"""
    value = result.get("map_revision")
    return _whole_int(value, -1)


def planning_snapshot_scope(target_path: str, map_layer: int) -> str:
    """生成目标和图层隔离的快照作用域键。"""
    return f"{target_path.strip()}::map_layer={map_layer}"


def _cell_is_occupied(cell: dict[str, Any], dimension: int) -> bool:
    """按 canonical cell 字段判断格子是否被地图内容占用。"""
    if dimension == 3:
        return _whole_int(cell.get("item"), -1) >= 0
    return _whole_int(cell.get("source_id"), -1) >= 0


def _planner_cell(cell: dict[str, Any], dimension: int) -> dict[str, Any]:
    """生成不泄漏 atlas/item 写入身份的 planner cell 投影。"""
    coords_value = cell.get("coords")
    coords = deepcopy(coords_value) if isinstance(coords_value, dict) else {}
    return {
        "coords": coords,
        "occupied": _cell_is_occupied(cell, dimension),
        "semantic_layer": str(cell.get("semantic_layer", "")),
        "tags": deepcopy(cell.get("tags", [])) if isinstance(cell.get("tags"), list) else [],
    }


def _region_contains(
    outer: dict[str, Any],
    inner: dict[str, Any],
    dimension: int,
) -> bool:
    """判断快照覆盖是否完整包含路径事实区域。"""
    axes = ("x", "y", "z") if dimension == 3 else ("x", "y")
    sizes = {"x": "width", "y": "height", "z": "depth"}
    for axis in axes:
        size = sizes[axis]
        outer_start = _whole_int(outer.get(axis))
        inner_start = _whole_int(inner.get(axis))
        outer_end = outer_start + max(1, _whole_int(outer.get(size), 1)) - 1
        inner_end = inner_start + max(1, _whole_int(inner.get(size), 1)) - 1
        if inner_start < outer_start or inner_end > outer_end:
            return False
    return True


@dataclass(frozen=True)
class AuthoritativeMapSnapshot:
    """保存一次 revision 绑定的完整地图规划事实。"""

    snapshot_id: str
    target_path: str
    map_layer: int
    map_revision: int
    dimension: int
    coverage: dict[str, Any]
    completeness: dict[str, bool]
    evidence_sources: dict[str, Any]
    canonical_cells: tuple[dict[str, Any], ...] = ()
    collision_support: dict[str, Any] = field(default_factory=dict)
    object_occupancy: dict[str, Any] = field(default_factory=dict)
    traversal_profile: dict[str, Any] = field(default_factory=dict)
    route_facts: dict[str, Any] = field(default_factory=dict)
    resource_bindings: dict[str, Any] = field(default_factory=dict)
    execution_eligible: bool = False
    schema: str = AUTHORITATIVE_MAP_SNAPSHOT_SCHEMA

    @property
    def digest(self) -> str:
        """返回忽略外部定位信息的快照内容摘要。"""
        return _digest(_snapshot_fact_payload(self))

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化 JSON 字典并附加摘要。"""
        payload = asdict(self)
        payload["canonical_cells"] = [deepcopy(item) for item in self.canonical_cells]
        payload["digest"] = self.digest
        return payload

    def planner_projection(self) -> dict[str, Any]:
        """生成 planner 可见且剔除逐格 atlas/item 身份的受限投影。"""
        occupied = [
            item for item in self.canonical_cells if _cell_is_occupied(item, self.dimension)
        ]
        cells = [
            _planner_cell(item, self.dimension) for item in occupied[:MAX_PLANNER_PROJECTION_CELLS]
        ]
        semantic_resources = [
            str(key) for key in self.resource_bindings if key != "observed_atlas_summary"
        ]
        if self.resource_bindings.get("observed_atlas_summary"):
            semantic_resources.append("ground")
        return {
            "schema": self.schema,
            "snapshot_id": self.snapshot_id,
            "digest": self.digest,
            "target_path": self.target_path,
            "map_layer": self.map_layer,
            "map_revision": self.map_revision,
            "dimension": self.dimension,
            "coverage": deepcopy(self.coverage),
            "completeness": deepcopy(self.completeness),
            "occupied_cells": cells,
            "occupied_cells_total": len(occupied),
            "occupied_cells_truncated": len(occupied) > len(cells),
            "collision_support": deepcopy(self.collision_support),
            "object_occupancy": deepcopy(self.object_occupancy),
            "traversal_profile": deepcopy(self.traversal_profile),
            "route_facts": deepcopy(self.route_facts),
            "semantic_resources": sorted(set(semantic_resources)),
            "execution_eligible": self.execution_eligible,
        }


def build_region_snapshot(
    tool_args: dict[str, Any],
    result: dict[str, Any],
    *,
    evidence_ref: dict[str, Any] | None = None,
) -> AuthoritativeMapSnapshot:
    """从 canonical `describe_map_region` 结果构造基础权威快照。

    Args:
        tool_args: 实际执行的区域读取参数。
        result: Godot 返回且已附加 map revision 的完整结果。
        evidence_ref: 指向原始地图工具 artifact 的不可变定位信息。

    Returns:
        包含完整 cell 事实和受限 planner 投影能力的快照。

    Raises:
        ValueError: 目标、revision 或区域读取合同缺失时抛出。
    """
    target_path = _normalized_target(tool_args, result)
    revision = _normalized_revision(result)
    if not target_path:
        raise ValueError("authoritative map snapshot requires target_path")
    if revision < 0:
        raise ValueError("authoritative map snapshot requires map_revision")
    dimension = 3 if _whole_int(result.get("dimension"), 2) == 3 else 2
    layer = _normalized_layer(tool_args, result)
    cells_value = result.get("cells")
    cells = (
        tuple(deepcopy(item) for item in cells_value if isinstance(item, dict))
        if isinstance(cells_value, list)
        else ()
    )
    cells_format = str(result.get("cells_format", tool_args.get("cells_format", "summary_only")))
    omitted = max(0, _whole_int(result.get("cells_omitted")))
    total = max(0, _whole_int(result.get("cells_total")))
    non_empty = max(0, _whole_int(result.get("non_empty_count")))
    returned = max(0, _whole_int(result.get("cells_returned"), len(cells)))
    coverage_complete = (
        cells_format in {"full", "non_empty_only"}
        and omitted == 0
        and (
            (cells_format == "full" and returned == total)
            or (cells_format == "non_empty_only" and returned == non_empty)
        )
    )
    coverage = {
        "region": {
            key: _whole_int(tool_args.get(key), 1 if key in {"width", "height", "depth"} else 0)
            for key in ("x", "y", "z", "width", "height", "depth")
            if key in tool_args or key in {"x", "y", "width", "height"}
        },
        "used_bounds": deepcopy(result.get("used_bounds", {})),
        "cells_format": cells_format,
        "cells_total": total,
        "cells_returned": returned,
        "cells_omitted": omitted,
        "non_empty_count": non_empty,
        "complete": coverage_complete,
    }
    collision_value = result.get("collision_support")
    collision_support = (
        deepcopy(collision_value)
        if isinstance(collision_value, dict)
        else {
            "source": "canonical_editor_cells",
            "complete": coverage_complete,
            "filled_cells": non_empty,
        }
    )
    object_value = result.get("object_occupancy")
    object_occupancy = (
        deepcopy(object_value)
        if isinstance(object_value, dict)
        else {"source": "unavailable", "freshness": "unknown", "complete": False}
    )
    bindings_value = result.get("resource_bindings")
    resource_bindings = (
        deepcopy(bindings_value)
        if isinstance(bindings_value, dict) and bindings_value
        else {"observed_atlas_summary": deepcopy(result.get("atlas_summary", []))}
    )
    completeness = {
        "coverage": coverage_complete,
        "canonical_cells": coverage_complete,
        "collision_support": bool(collision_support.get("complete", False)),
        "object_occupancy": bool(object_occupancy.get("complete", False)),
        "traversal_profile": False,
        "trajectory_coverage": False,
        "entry_anchor": False,
        "reachable_frontier": False,
        "resource_bindings": bool(resource_bindings),
    }
    snapshot = AuthoritativeMapSnapshot(
        snapshot_id="",
        target_path=target_path,
        map_layer=layer,
        map_revision=revision,
        dimension=dimension,
        coverage=coverage,
        completeness=completeness,
        evidence_sources={"map_region": deepcopy(evidence_ref or {})},
        canonical_cells=cells,
        collision_support=collision_support,
        object_occupancy=object_occupancy,
        resource_bindings=resource_bindings,
        execution_eligible=False,
    )
    return replace(snapshot, snapshot_id=f"mapsnap-{snapshot.digest[:24]}")


def merge_frontier_snapshot(
    snapshot: AuthoritativeMapSnapshot,
    tool_args: dict[str, Any],
    result: dict[str, Any],
    *,
    evidence_ref: dict[str, Any] | None = None,
) -> AuthoritativeMapSnapshot:
    """把确定性 frontier 和 traversal profile 合并为派生快照。

    Args:
        snapshot: 同目标、图层和 revision 的基础快照。
        tool_args: 实际 frontier 计算参数。
        result: Godot 返回的 frontier 计算结果。
        evidence_ref: frontier 工具原始 artifact 定位信息。

    Returns:
        带有 route facts 和显式 traversal profile 的新快照。

    Raises:
        ValueError: frontier 结果不属于基础快照时抛出。
    """
    target_path = _normalized_target(tool_args, result)
    revision = _normalized_revision(result)
    layer = _normalized_layer(tool_args, result)
    if (
        target_path != snapshot.target_path
        or layer != snapshot.map_layer
        or revision != snapshot.map_revision
    ):
        raise ValueError("frontier facts do not match authoritative snapshot scope")
    contract_value = result.get("planning_contract")
    contract = deepcopy(contract_value) if isinstance(contract_value, dict) else {}
    traversal_value = contract.get("traversal")
    traversal = deepcopy(traversal_value) if isinstance(traversal_value, dict) else {}
    required_traversal = {
        "movement_model",
        "cell_occupancy",
        "requires_support",
        "support_occupancy",
        "max_horizontal_gap",
        "max_rise",
        "max_fall",
    }
    traversal_complete = required_traversal.issubset(traversal)
    if traversal_complete:
        traversal["source"] = "canonical_frontier_contract"
        traversal["source_fields"] = {
            key: "explicit_tool_input" for key in sorted(required_traversal)
        }
    route_facts = {
        "entry_anchor": deepcopy(result.get("start_anchor", result.get("start", {}))),
        "reachable_frontier": deepcopy(
            result.get("reachable_frontier", result.get("rightmost_frontier", {}))
        ),
        "frontier_candidates": deepcopy(result.get("frontier_candidates", [])),
        "reachable_count": _whole_int(result.get("reachable_count")),
        "planning_contract": contract,
    }
    completeness = deepcopy(snapshot.completeness)
    route_region_value = contract.get("region", result.get("region", {}))
    route_region = route_region_value if isinstance(route_region_value, dict) else {}
    snapshot_region_value = snapshot.coverage.get("region", {})
    snapshot_region = snapshot_region_value if isinstance(snapshot_region_value, dict) else {}
    completeness["trajectory_coverage"] = bool(route_region) and _region_contains(
        snapshot_region,
        route_region,
        snapshot.dimension,
    )
    completeness["traversal_profile"] = traversal_complete
    completeness["entry_anchor"] = bool(route_facts["entry_anchor"])
    completeness["reachable_frontier"] = bool(route_facts["reachable_frontier"])
    execution_eligible = all(
        bool(completeness.get(key, False))
        for key in (
            "coverage",
            "canonical_cells",
            "collision_support",
            "object_occupancy",
            "traversal_profile",
            "trajectory_coverage",
            "entry_anchor",
            "reachable_frontier",
            "resource_bindings",
        )
    )
    sources = deepcopy(snapshot.evidence_sources)
    sources["reachable_frontier"] = deepcopy(evidence_ref or {})
    derived = replace(
        snapshot,
        snapshot_id="",
        completeness=completeness,
        evidence_sources=sources,
        traversal_profile=traversal,
        route_facts=route_facts,
        execution_eligible=execution_eligible,
    )
    snapshot_id = f"mapsnap-{derived.digest[:24]}"
    return replace(derived, snapshot_id=snapshot_id)


@dataclass(frozen=True)
class PlanningSnapshotStore:
    """在当前会话目录内原子保存和验证规划快照。"""

    project_root: Path
    session_id: str
    session_epoch: str = ""

    @property
    def root(self) -> Path:
        """返回当前会话规划快照目录。"""
        session_digest = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()
        return (
            self.project_root
            / ".ai_agent_service"
            / "artifacts"
            / session_digest
            / "planning_snapshots"
        )

    def store(self, snapshot: AuthoritativeMapSnapshot) -> dict[str, Any]:
        """原子保存快照并返回适合 reducer 持久化的定位信息。"""
        payload = snapshot.to_dict()
        document = {
            "schema": AUTHORITATIVE_MAP_SNAPSHOT_SCHEMA,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "snapshot_id": snapshot.snapshot_id,
            "digest": snapshot.digest,
            "snapshot": payload,
            "planner_projection": snapshot.planner_projection(),
        }
        encoded = _canonical_json(document).encode("utf-8")
        if len(encoded) > MAX_PLANNING_SNAPSHOT_BYTES:
            raise ValueError(f"planning snapshot exceeds {MAX_PLANNING_SNAPSHOT_BYTES} bytes")
        path = self.root / f"{_safe_name(snapshot.snapshot_id)}.json"
        atomic_write_json(path, document)
        return {
            "artifact_kind": AUTHORITATIVE_MAP_SNAPSHOT_SCHEMA,
            "artifact_ref": path.relative_to(self.project_root).as_posix(),
            "snapshot_id": snapshot.snapshot_id,
            "digest": snapshot.digest,
            "target_path": snapshot.target_path,
            "map_layer": snapshot.map_layer,
            "map_revision": snapshot.map_revision,
            "completeness": deepcopy(snapshot.completeness),
            "execution_eligible": snapshot.execution_eligible,
        }

    def read(self, artifact_ref: str) -> AuthoritativeMapSnapshot:
        """读取并验证当前会话的完整规划快照。"""
        document = self._read_document(artifact_ref)
        snapshot_value = document.get("snapshot")
        if not isinstance(snapshot_value, dict):
            raise ValueError("planning snapshot payload must be an object")
        data = dict(snapshot_value)
        data.pop("digest", None)
        cells_value = data.get("canonical_cells", [])
        data["canonical_cells"] = tuple(
            deepcopy(item) for item in cells_value if isinstance(item, dict)
        )
        snapshot = AuthoritativeMapSnapshot(**data)
        if snapshot.digest != document.get("digest"):
            raise ValueError("planning snapshot digest mismatch")
        return snapshot

    def read_projection_page(
        self,
        artifact_ref: str,
        *,
        field: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """安全读取 planner 投影元数据或一个分页字段。"""
        document = self._read_document(artifact_ref)
        projection = document.get("planner_projection")
        if not isinstance(projection, dict):
            raise ValueError("planning snapshot projection must be an object")
        metadata = {
            "artifact_kind": AUTHORITATIVE_MAP_SNAPSHOT_SCHEMA,
            "artifact_ref": artifact_ref,
            "snapshot_id": document.get("snapshot_id"),
            "digest": document.get("digest"),
            "available_fields": sorted(str(key) for key in projection),
        }
        if not field:
            return metadata
        if field not in projection:
            raise ValueError(f"planning snapshot projection has no field: {field}")
        value = projection[field]
        if not isinstance(value, list):
            return {**metadata, "field": field, "value": deepcopy(value), "has_more": False}
        start = max(0, offset)
        page_limit = max(1, min(limit, MAX_PLANNING_SNAPSHOT_PAGE_ITEMS))
        end = min(len(value), start + page_limit)
        return {
            **metadata,
            "field": field,
            "value": deepcopy(value[start:end]),
            "offset": start,
            "limit": page_limit,
            "total": len(value),
            "has_more": end < len(value),
        }

    def _read_document(self, artifact_ref: str) -> dict[str, Any]:
        """解析受限 artifact 引用并返回通过身份检查的文档。"""
        if not artifact_ref or Path(artifact_ref).is_absolute():
            raise ValueError("artifact_ref must be a project-relative path")
        root = self.root.resolve()
        candidate = (self.project_root / artifact_ref).resolve(strict=True)
        if candidate.parent != root or candidate.suffix.lower() != ".json":
            raise ValueError("artifact_ref is outside planning snapshot directory")
        if candidate.stat().st_size > MAX_PLANNING_SNAPSHOT_BYTES:
            raise ValueError("planning snapshot file exceeds size limit")
        value = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("planning snapshot document must be an object")
        if value.get("schema") != AUTHORITATIVE_MAP_SNAPSHOT_SCHEMA:
            raise ValueError("unsupported planning snapshot schema")
        if value.get("session_id") != self.session_id:
            raise ValueError("planning snapshot belongs to another session")
        if self.session_epoch and value.get("session_epoch") != self.session_epoch:
            raise ValueError("planning snapshot belongs to another session epoch")
        return value


@dataclass(frozen=True)
class ApprovedBatchStore:
    """持久化 validator/compiler 产出的不可变批准批次。"""

    project_root: Path
    session_id: str
    session_epoch: str = ""

    @property
    def root(self) -> Path:
        """返回当前会话批准批次目录。"""
        session_digest = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()
        return (
            self.project_root
            / ".ai_agent_service"
            / "artifacts"
            / session_digest
            / "approved_map_batches"
        )

    def store(self, record: dict[str, Any]) -> dict[str, Any]:
        """原子写入批准记录并返回 worker 可绑定的定位信息。"""
        approval_id = str(record.get("approval_id", "")).strip()
        fingerprint = str(record.get("batch_fingerprint", "")).strip()
        if not approval_id or not fingerprint:
            raise ValueError("approved batch requires approval id and fingerprint")
        document = {
            "schema": APPROVED_MAP_BATCH_SCHEMA,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "record": deepcopy(record),
        }
        path = self.root / f"{_safe_name(approval_id)}.json"
        atomic_write_json(path, document)
        return {
            "artifact_ref": path.relative_to(self.project_root).as_posix(),
            "batch_id": approval_id,
            "target_path": str(record.get("target", "")),
            "map_layer": _whole_int(record.get("map_layer")),
            "map_revision": _whole_int(record.get("expected_revision")),
            "snapshot_id": str(record.get("snapshot_id", "")),
            "snapshot_digest": str(record.get("snapshot_digest", "")),
            "batch_fingerprint": fingerprint,
        }

    def read(self, artifact_ref: str) -> dict[str, Any]:
        """读取并验证当前会话的不可变批准批次。"""
        if not artifact_ref or Path(artifact_ref).is_absolute():
            raise ValueError("approved batch artifact_ref must be project-relative")
        root = self.root.resolve()
        candidate = (self.project_root / artifact_ref).resolve(strict=True)
        if candidate.parent != root or candidate.suffix.lower() != ".json":
            raise ValueError("approved batch artifact is outside session directory")
        value = json.loads(candidate.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema") != APPROVED_MAP_BATCH_SCHEMA:
            raise ValueError("unsupported approved batch artifact")
        if value.get("session_id") != self.session_id:
            raise ValueError("approved batch belongs to another session")
        if self.session_epoch and value.get("session_epoch") != self.session_epoch:
            raise ValueError("approved batch belongs to another session epoch")
        record = value.get("record")
        if not isinstance(record, dict):
            raise ValueError("approved batch artifact has no record")
        return deepcopy(record)


@dataclass(frozen=True)
class PlanningRepairStore:
    """保存失败规划的结构化 issue/repair 证据。"""

    project_root: Path
    session_id: str
    session_epoch: str = ""

    @property
    def root(self) -> Path:
        """返回当前会话 repair artifact 目录。"""
        session_digest = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()
        return (
            self.project_root
            / ".ai_agent_service"
            / "artifacts"
            / session_digest
            / "planning_repairs"
        )

    def store(self, payload: dict[str, Any]) -> dict[str, str]:
        """原子保存 repair payload 并返回引用与 digest。"""
        digest = _digest(payload)
        document = {
            "schema": PLANNING_REPAIR_SCHEMA,
            "session_id": self.session_id,
            "session_epoch": self.session_epoch,
            "digest": digest,
            "repair": deepcopy(payload),
        }
        path = self.root / f"repair-{digest[:24]}.json"
        atomic_write_json(path, document)
        return {
            "repair_artifact_ref": path.relative_to(self.project_root).as_posix(),
            "repair_artifact_digest": digest,
        }
