"""地图工具完整结果的会话级聚合存储与事务内读取。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Protocol

from app.storage.atomic import atomic_write_json

MAP_ARTIFACT_SCHEMA: Final = "map_tool_artifacts_v1"
MAP_ARTIFACT_VERSION: Final = 2
MAP_COORDINATED_COMMIT_VERSION: Final = 1
MAX_MAP_ARTIFACT_PAGE_ITEMS: Final = 200
MAX_MAP_ARTIFACT_FILE_BYTES: Final = 100 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
MAP_COORDINATED_COMMIT_FAILPOINTS: Final[frozenset[str]] = frozenset(
    {
        "artifact_prepare_before_write",
        "artifact_prepare_after_write",
        "session_publish_before_write",
        "session_publish_after_write",
        "commit_marker_before_write",
        "commit_marker_after_write",
        "cleanup_before_write",
        "cleanup_after_write",
    }
)


class CoordinatedCommitFailureInjector(Protocol):
    """定义仅由测试组合注入的协调提交故障边界。"""

    def hit(self, name: str) -> None:
        """在命名边界触发测试定义的确定性故障。"""


class MapArtifactTurnConflictError(ValueError):
    """A committed turn id was reused with a different canonical fingerprint."""

    def __init__(self, turn_id: str) -> None:
        """Initialize a stable typed integrity conflict."""
        super().__init__(f"conflicting committed map artifact turn: {turn_id}")
        self.turn_id = turn_id
        self.error_code = "map_artifact_turn_identity_conflict"


def _safe_session_name(value: str) -> str:
    """把会话标识转换为稳定且不可逃逸目录的名称。"""
    cleaned = _SAFE_NAME_RE.sub("-", value).strip(".-")
    return cleaned[:80] if cleaned else hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _canonical_fingerprint(value: Any) -> str:
    """返回 JSON 值的稳定 SHA-256 指纹。"""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _session_references_turn(
    value: Any,
    turn_id: str,
    entry_fingerprints: dict[str, str],
) -> bool:
    """Return whether a Session contains every exact locator for a prepared turn."""
    found: set[str] = set()

    def visit(item: Any) -> None:
        """Walk JSON-native Session values and collect matching locators."""
        if isinstance(item, dict):
            if item.get("artifact_turn_id") == turn_id and isinstance(
                item.get("artifact_entry_id"), str
            ):
                entry_id = str(item["artifact_entry_id"])
                if entry_fingerprints.get(entry_id) == item.get("artifact_fingerprint"):
                    found.add(entry_id)
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(value)
    return bool(entry_fingerprints) and set(entry_fingerprints).issubset(found)


@dataclass(frozen=True)
class MapArtifactLocator:
    """定位聚合文件中的一个地图工具结果。"""

    artifact_ref: str
    turn_id: str
    entry_id: str
    fingerprint: str = ""
    artifact_kind: str = "map_tool_result"
    session_epoch: str = ""

    def as_dict(self) -> dict[str, str]:
        """返回适合写入 LLM history 的结构化定位信息。"""
        payload = {
            "artifact_ref": self.artifact_ref,
            "artifact_turn_id": self.turn_id,
            "artifact_entry_id": self.entry_id,
            "artifact_kind": self.artifact_kind,
        }
        if self.fingerprint:
            payload["artifact_fingerprint"] = self.fingerprint
        if self.session_epoch:
            payload["session_epoch"] = self.session_epoch
        return payload


@dataclass
class StagedMapArtifactTurn:
    """工具结果提交事务中尚未发布的一个聚合 turn。"""

    session_id: str
    turn_id: str
    request_id: str | None
    session_epoch: str = ""
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_entry(
        self,
        *,
        tool_use_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """按 tool_use_id 幂等加入完整工具结果，拒绝同 id 的不同内容。"""
        fingerprint = _canonical_fingerprint(
            {"tool": tool_name, "input": tool_args, "result": result}
        )
        entry = {
            "artifact_kind": "map_tool_result",
            "tool": tool_name,
            "input": tool_args,
            "result": result,
            "fingerprint": fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        existing = self.entries.get(tool_use_id)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise ValueError(f"conflicting map artifact entry for tool_use_id: {tool_use_id}")
            return
        self.entries[tool_use_id] = entry

    @property
    def fingerprint(self) -> str:
        """返回忽略时间戳后的 turn 级规范指纹。"""
        stable_entries = {
            entry_id: {key: value for key, value in entry.items() if key != "created_at"}
            for entry_id, entry in self.entries.items()
        }
        return _canonical_fingerprint(stable_entries)


@dataclass(frozen=True)
class MapArtifactStore:
    """在单个 ``map_artifacts.json`` 中保存一个会话的地图工具结果。"""

    project_root: Path
    session_id: str
    failure_injector: CoordinatedCommitFailureInjector | None = None
    session_epoch: str = ""

    def _hit_failpoint(self, name: str) -> None:
        """仅在构造时显式注入测试依赖后触发命名故障。"""
        if name not in MAP_COORDINATED_COMMIT_FAILPOINTS:
            raise ValueError(f"unknown coordinated commit failpoint: {name}")
        if self.failure_injector is not None:
            self.failure_injector.hit(name)

    @property
    def session_root(self) -> Path:
        """返回当前会话的地图 artifact 目录。"""
        return (
            self.project_root
            / ".ai_agent_service"
            / "artifacts"
            / _safe_session_name(self.session_id)
        )

    @property
    def path(self) -> Path:
        """返回当前会话唯一的地图工具 artifact 文件。"""
        return self.session_root / "map_artifacts.json"

    @property
    def relative_ref(self) -> str:
        """返回工程根目录相对引用。"""
        return self.path.relative_to(self.project_root).as_posix()

    def locator(
        self,
        turn_id: str,
        entry_id: str,
        fingerprint: str = "",
    ) -> MapArtifactLocator:
        """创建指向聚合文件条目的定位器。"""
        return MapArtifactLocator(
            self.relative_ref,
            turn_id,
            entry_id,
            fingerprint,
            session_epoch=self.session_epoch,
        )

    def merge_turn(self, staged: StagedMapArtifactTurn) -> None:
        """在 Session 提交后原子合并暂存 turn，拒绝覆盖不同指纹。"""
        document = self._load_document()
        if not self.assert_mergeable(staged, document=document):
            return
        turns = document["turns"]
        turns[staged.turn_id] = {
            "request_id": staged.request_id,
            "fingerprint": staged.fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": staged.entries,
            "publication_state": "committed",
        }
        atomic_write_json(self.path, document)

    def prepare_turn(self, staged: StagedMapArtifactTurn) -> bool:
        """Durably prepare an artifact turn before Session locator publication."""
        document = self._load_document()
        existing = document["turns"].get(staged.turn_id)
        if isinstance(existing, dict):
            if existing.get("fingerprint") != staged.fingerprint:
                raise MapArtifactTurnConflictError(staged.turn_id)
            if existing.get("publication_state") == "prepared":
                return True
            return False
        if not self.assert_mergeable(staged, document=document):
            return False
        old_digest = _canonical_fingerprint(document)
        document["turns"][staged.turn_id] = {
            "request_id": staged.request_id,
            "fingerprint": staged.fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": staged.entries,
            "publication_state": "prepared",
        }
        document.setdefault("coordinated_commits", {})[staged.turn_id] = {
            "schema_version": MAP_COORDINATED_COMMIT_VERSION,
            "session_id": staged.session_id,
            "session_epoch": staged.session_epoch,
            "turn_id": staged.turn_id,
            "entry_ids": sorted(staged.entries),
            "entry_fingerprints": {
                entry_id: str(entry.get("fingerprint", ""))
                for entry_id, entry in staged.entries.items()
            },
            "fingerprint": staged.fingerprint,
            "old_document_digest": old_digest,
            "new_document_digest": _canonical_fingerprint(document["turns"]),
            "temporary_paths": [],
            "lifecycle_state": "prepared",
        }
        self._hit_failpoint("artifact_prepare_before_write")
        atomic_write_json(self.path, document)
        self._hit_failpoint("artifact_prepare_after_write")
        return True

    def commit_prepared_turn(self, staged: StagedMapArtifactTurn) -> None:
        """Publish a prepared artifact after the Session locator is durable."""
        document = self._load_document()
        turn = document["turns"].get(staged.turn_id)
        commit = document.get("coordinated_commits", {}).get(staged.turn_id)
        if not isinstance(turn, dict) or not isinstance(commit, dict):
            if not self.assert_mergeable(staged, document=document):
                return
            raise ValueError("prepared coordinated artifact commit was not found")
        if (
            turn.get("fingerprint") != staged.fingerprint
            or commit.get("fingerprint") != staged.fingerprint
        ):
            raise MapArtifactTurnConflictError(staged.turn_id)
        turn["publication_state"] = "committed"
        commit["lifecycle_state"] = "committed"
        self._hit_failpoint("commit_marker_before_write")
        atomic_write_json(self.path, document)
        self._hit_failpoint("commit_marker_after_write")
        document["coordinated_commits"].pop(staged.turn_id, None)
        self._hit_failpoint("cleanup_before_write")
        atomic_write_json(self.path, document)
        self._hit_failpoint("cleanup_after_write")

    def discard_prepared_turn(self, staged: StagedMapArtifactTurn) -> None:
        """Remove an unreferenced prepared turn after Session publication fails."""
        document = self._load_document()
        turn = document["turns"].get(staged.turn_id)
        if not isinstance(turn, dict):
            return
        if (
            turn.get("publication_state") == "prepared"
            and turn.get("fingerprint") == staged.fingerprint
        ):
            document["turns"].pop(staged.turn_id, None)
            document.setdefault("coordinated_commits", {}).pop(
                staged.turn_id,
                None,
            )
            atomic_write_json(self.path, document)

    def reconcile_with_session(self, session_payload: dict[str, Any]) -> None:
        """Resolve prepared turns from exact locators in a durable Session."""
        document = self._load_document()
        changed = False
        for turn_id, commit_value in list(document.get("coordinated_commits", {}).items()):
            if not isinstance(commit_value, dict):
                continue
            turn = document["turns"].get(turn_id)
            if not isinstance(turn, dict):
                document["coordinated_commits"].pop(turn_id, None)
                changed = True
                continue
            if turn.get("publication_state") == "committed":
                document["coordinated_commits"].pop(turn_id, None)
                changed = True
                continue
            entry_fingerprints_value = commit_value.get(
                "entry_fingerprints",
                {},
            )
            entry_fingerprints = (
                {
                    str(entry_id): str(entry_fingerprint)
                    for entry_id, entry_fingerprint in entry_fingerprints_value.items()
                }
                if isinstance(entry_fingerprints_value, dict)
                else {}
            )
            referenced = _session_references_turn(
                session_payload,
                str(turn_id),
                entry_fingerprints,
            )
            if referenced:
                turn["publication_state"] = "committed"
                commit_value["lifecycle_state"] = "committed"
            else:
                document["turns"].pop(turn_id, None)
            document["coordinated_commits"].pop(turn_id, None)
            changed = True
        if changed:
            atomic_write_json(self.path, document)

    def assert_mergeable(
        self,
        staged: StagedMapArtifactTurn,
        *,
        document: dict[str, Any] | None = None,
    ) -> bool:
        """在 Session 提交前检查 turn 冲突；相同指纹表示幂等无需再写。"""
        if staged.session_id != self.session_id:
            raise ValueError("staged map artifact belongs to another session")
        if staged.session_epoch != self.session_epoch:
            raise ValueError("staged map artifact belongs to another session epoch")
        current = document if document is not None else self._load_document()
        existing = current["turns"].get(staged.turn_id)
        if not isinstance(existing, dict):
            return True
        if existing.get("fingerprint") != staged.fingerprint:
            raise MapArtifactTurnConflictError(staged.turn_id)
        return False

    def max_reserved_turn_counter(self) -> int:
        """返回 artifact 文档中已占用的最大 ``tN`` turn 序号。"""
        document = self._load_document()
        maximum = 0
        for turn_id in document["turns"]:
            match = re.fullmatch(r"t([1-9][0-9]*)", str(turn_id))
            if match is not None:
                maximum = max(maximum, int(match.group(1)))
        return maximum

    def read_page(
        self,
        artifact_ref: str,
        *,
        turn_id: str = "",
        entry_id: str = "",
        field: str = "",
        fingerprint: str = "",
        offset: int = 0,
        limit: int = 50,
        staged: StagedMapArtifactTurn | None = None,
    ) -> dict[str, Any]:
        """读取当前事务暂存条目或当前 schema 已提交条目。"""
        path = self._resolve_ref(artifact_ref)
        if path != self.path.resolve():
            raise ValueError("unsupported map artifact reference")
        if not turn_id or not entry_id:
            raise ValueError(
                "aggregated map artifact requires artifact_turn_id and artifact_entry_id"
            )
        entry, source = self._resolve_aggregated_entry(
            turn_id=turn_id,
            entry_id=entry_id,
            staged=staged,
        )
        if not fingerprint:
            raise ValueError("aggregated map artifact requires artifact_fingerprint")
        if entry.get("fingerprint") != fingerprint:
            raise ValueError("map artifact locator fingerprint mismatch")
        result = entry.get("result")
        if not isinstance(result, dict):
            raise ValueError("map artifact result must be an object")
        metadata: dict[str, Any] = {
            "artifact_ref": artifact_ref,
            "artifact_turn_id": turn_id,
            "artifact_entry_id": entry_id,
            "artifact_kind": entry.get("artifact_kind", "map_tool_result"),
            "source": source,
            "session_epoch": self.session_epoch,
            "tool": entry.get("tool"),
            "fingerprint": entry.get("fingerprint"),
            "available_fields": sorted(str(key) for key in result),
        }
        if not field:
            return metadata
        normalized_field = field.removeprefix("result.")
        if normalized_field not in result:
            raise ValueError(f"map artifact has no result field: {normalized_field}")
        value = result[normalized_field]
        if isinstance(value, list):
            start = max(0, offset)
            page_limit = max(1, min(limit, MAX_MAP_ARTIFACT_PAGE_ITEMS))
            end = min(len(value), start + page_limit)
            return {
                **metadata,
                "field": normalized_field,
                "value": value[start:end],
                "offset": start,
                "limit": page_limit,
                "total": len(value),
                "has_more": end < len(value),
            }
        return {
            **metadata,
            "field": normalized_field,
            "value": value,
            "has_more": False,
        }

    def _load_document(self) -> dict[str, Any]:
        """读取并验证聚合文档；不存在时创建空文档。"""
        if not self.path.exists():
            return {
                "schema": MAP_ARTIFACT_SCHEMA,
                "version": MAP_ARTIFACT_VERSION,
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
                "turns": {},
                "coordinated_commits": {},
            }
        if self.path.stat().st_size > MAX_MAP_ARTIFACT_FILE_BYTES:
            raise ValueError("map artifact file exceeds size limit")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("map artifact document must be an object")
        if payload.get("schema") != MAP_ARTIFACT_SCHEMA:
            raise ValueError("unsupported map artifact schema")
        if payload.get("session_id") != self.session_id:
            raise ValueError("map artifact belongs to another session")
        if self.session_epoch and str(payload.get("session_epoch", "")) != self.session_epoch:
            raise ValueError("map artifact belongs to another session epoch")
        if not isinstance(payload.get("turns"), dict):
            raise ValueError("map artifact turns must be an object")
        if not isinstance(payload.get("coordinated_commits"), dict):
            payload["coordinated_commits"] = {}
        return payload

    def _resolve_aggregated_entry(
        self,
        *,
        turn_id: str,
        entry_id: str,
        staged: StagedMapArtifactTurn | None,
    ) -> tuple[dict[str, Any], str]:
        """解析事务内或已提交的聚合条目。"""
        if (
            staged is not None
            and staged.session_id == self.session_id
            and staged.session_epoch == self.session_epoch
            and staged.turn_id == turn_id
        ):
            entry = staged.entries.get(entry_id)
            if isinstance(entry, dict):
                return entry, "staged"
        document = self._load_document()
        turn = document["turns"].get(turn_id)
        if not isinstance(turn, dict):
            raise ValueError(f"map artifact turn was not found: {turn_id}")
        if turn.get("publication_state", "committed") != "committed":
            raise ValueError(f"map artifact turn is not committed: {turn_id}")
        entries = turn.get("entries")
        if not isinstance(entries, dict) or not isinstance(entries.get(entry_id), dict):
            raise ValueError(f"map artifact entry was not found: {entry_id}")
        return entries[entry_id], "committed"

    def _resolve_ref(self, artifact_ref: str) -> Path:
        """限定引用为当前会话唯一 canonical artifact 文档。"""
        if not artifact_ref or Path(artifact_ref).is_absolute():
            raise ValueError("artifact_ref must be a project-relative path")
        session_root = self.session_root.resolve()
        candidate = (self.project_root / artifact_ref).resolve(strict=False)
        if candidate.parent != session_root or candidate != self.path.resolve():
            raise ValueError("artifact_ref is outside the current session map artifact directory")
        return candidate


def clear_session_artifacts(project_root: Path, session_id: str) -> None:
    """精确删除一个 session 的所有旧版及 epoch 化 artifact 目录。

    该操作只触碰 ``.ai_agent_service/artifacts`` 下由 session id 唯一推导出的
    两个目录，不触碰 Godot 事务 journal、revision、registry 或工程内容。
    """
    artifact_root = (project_root / ".ai_agent_service" / "artifacts").resolve()
    candidates = {
        artifact_root / _safe_session_name(session_id),
        artifact_root / hashlib.sha256(session_id.encode("utf-8")).hexdigest(),
    }
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.parent != artifact_root:
            raise ValueError("refusing to delete artifact path outside artifact root")
        if resolved.exists():
            shutil.rmtree(resolved)
