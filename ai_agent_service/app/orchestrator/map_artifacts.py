"""地图工具完整结果的会话级聚合存储与事务内读取。"""

from __future__ import annotations

import hashlib
import json
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from app.storage.atomic import atomic_write_json

MAP_ARTIFACT_SCHEMA: Final = "map_tool_artifacts_v1"
MAP_ARTIFACT_VERSION: Final = 1
MAX_MAP_ARTIFACT_PAGE_ITEMS: Final = 200
MAX_MAP_ARTIFACT_FILE_BYTES: Final = 100 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")


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


@dataclass(frozen=True)
class MapArtifactLocator:
    """定位聚合文件中的一个地图工具结果。"""

    artifact_ref: str
    turn_id: str
    entry_id: str
    artifact_kind: str = "map_tool_result"

    def as_dict(self) -> dict[str, str]:
        """返回适合写入 LLM history 的结构化定位信息。"""
        return {
            "artifact_ref": self.artifact_ref,
            "artifact_turn_id": self.turn_id,
            "artifact_entry_id": self.entry_id,
            "artifact_kind": self.artifact_kind,
        }


@dataclass
class StagedMapArtifactTurn:
    """工具结果提交事务中尚未发布的一个聚合 turn。"""

    session_id: str
    turn_id: str
    request_id: str | None
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
                raise ValueError(
                    f"conflicting map artifact entry for tool_use_id: {tool_use_id}"
                )
            return
        self.entries[tool_use_id] = entry

    @property
    def fingerprint(self) -> str:
        """返回忽略时间戳后的 turn 级规范指纹。"""
        stable_entries = {
            entry_id: {
                key: value
                for key, value in entry.items()
                if key != "created_at"
            }
            for entry_id, entry in self.entries.items()
        }
        return _canonical_fingerprint(stable_entries)


CURRENT_MAP_ARTIFACT_TURN: ContextVar[StagedMapArtifactTurn | None] = ContextVar(
    "current_map_artifact_turn",
    default=None,
)


@dataclass(frozen=True)
class MapArtifactStore:
    """在单个 ``map_artifacts.json`` 中保存一个会话的地图工具结果。"""

    project_root: Path
    session_id: str

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

    def locator(self, turn_id: str, entry_id: str) -> MapArtifactLocator:
        """创建指向聚合文件条目的定位器。"""
        return MapArtifactLocator(self.relative_ref, turn_id, entry_id)

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
        }
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
        current = document if document is not None else self._load_document()
        existing = current["turns"].get(staged.turn_id)
        if not isinstance(existing, dict):
            return True
        if existing.get("fingerprint") != staged.fingerprint:
            raise ValueError(
                f"conflicting committed map artifact turn: {staged.turn_id}"
            )
        return False

    def read_page(
        self,
        artifact_ref: str,
        *,
        turn_id: str = "",
        entry_id: str = "",
        field: str = "",
        offset: int = 0,
        limit: int = 50,
        staged: StagedMapArtifactTurn | None = None,
    ) -> dict[str, Any]:
        """优先读取当前事务暂存条目，再读取已提交或旧版单文件条目。"""
        path = self._resolve_ref(artifact_ref)
        if path == self.path.resolve():
            if not turn_id or not entry_id:
                raise ValueError(
                    "aggregated map artifact requires artifact_turn_id and artifact_entry_id"
                )
            entry, source = self._resolve_aggregated_entry(
                turn_id=turn_id,
                entry_id=entry_id,
                staged=staged,
            )
        else:
            entry, source = self._read_legacy_entry(path), "legacy"
        result = entry.get("result")
        if not isinstance(result, dict):
            raise ValueError("map artifact result must be an object")
        metadata: dict[str, Any] = {
            "artifact_ref": artifact_ref,
            "artifact_turn_id": turn_id,
            "artifact_entry_id": entry_id,
            "artifact_kind": entry.get("artifact_kind", "map_tool_result"),
            "source": source,
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
                "turns": {},
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
        if not isinstance(payload.get("turns"), dict):
            raise ValueError("map artifact turns must be an object")
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
            and staged.turn_id == turn_id
        ):
            entry = staged.entries.get(entry_id)
            if isinstance(entry, dict):
                return entry, "staged"
        document = self._load_document()
        turn = document["turns"].get(turn_id)
        if not isinstance(turn, dict):
            raise ValueError(f"map artifact turn was not found: {turn_id}")
        entries = turn.get("entries")
        if not isinstance(entries, dict) or not isinstance(entries.get(entry_id), dict):
            raise ValueError(f"map artifact entry was not found: {entry_id}")
        return entries[entry_id], "committed"

    def _read_legacy_entry(self, path: Path) -> dict[str, Any]:
        """只读兼容整改前的每次调用单独 artifact。"""
        if path.stat().st_size > MAX_MAP_ARTIFACT_FILE_BYTES:
            raise ValueError("legacy map artifact file exceeds size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("legacy map artifact payload must be an object")
        if str(payload.get("schema", "")).startswith("map_delegate_artifact"):
            raise ValueError("delegate artifact must be read with read_delegate_artifact")
        if not isinstance(payload.get("result"), dict):
            raise ValueError("unsupported legacy map artifact schema")
        return payload

    def _resolve_ref(self, artifact_ref: str) -> Path:
        """限定引用为当前会话目录下的 JSON，拒绝跨会话和 delegate 子目录。"""
        if not artifact_ref or Path(artifact_ref).is_absolute():
            raise ValueError("artifact_ref must be a project-relative path")
        session_root = self.session_root.resolve()
        candidate = (self.project_root / artifact_ref).resolve(strict=False)
        if candidate.parent != session_root or candidate.suffix.lower() != ".json":
            raise ValueError("artifact_ref is outside the current session map artifact directory")
        if candidate != self.path.resolve() and not candidate.is_file():
            raise ValueError("legacy map artifact file was not found")
        return candidate
