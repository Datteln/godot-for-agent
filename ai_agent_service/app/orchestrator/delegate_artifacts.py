"""地图子 Agent 完整结果的本地 artifact 存储与受控读取。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from app.storage.atomic import atomic_write_json

DELEGATE_ARTIFACT_SCHEMA: Final = "map_delegate_artifact_v1"
MAX_DELEGATE_ARTIFACT_BYTES: Final = 8 * 1024 * 1024
MAX_DELEGATE_ARTIFACT_PAGE_ITEMS: Final = 50
_FRAME_ID_RE = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class DelegateArtifactStore:
    """在工程根目录下保存并读取当前会话的地图委派结果。"""

    project_root: Path
    session_id: str
    session_epoch: str = ""

    @property
    def session_root(self) -> Path:
        """返回当前会话独占的 artifact 目录。"""
        session_digest = hashlib.sha256(self.session_id.encode("utf-8")).hexdigest()
        return self.project_root / ".ai_agent_service" / "artifacts" / session_digest / "delegates"

    def store(
        self,
        *,
        frame_id: str,
        agent_name: str,
        result_schema: str,
        result: dict[str, Any],
    ) -> str:
        """原子保存完整子 Agent 结果并返回工程相对引用。"""
        canonical = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        encoded = canonical.encode("utf-8")
        if len(encoded) > MAX_DELEGATE_ARTIFACT_BYTES:
            raise ValueError(f"delegate artifact exceeds {MAX_DELEGATE_ARTIFACT_BYTES} bytes")
        digest = hashlib.sha256(encoded).hexdigest()
        safe_frame = _FRAME_ID_RE.sub("_", frame_id).strip("._-") or "frame"
        path = self.session_root / f"{safe_frame}-{digest[:16]}.json"
        atomic_write_json(
            path,
            {
                "schema": DELEGATE_ARTIFACT_SCHEMA,
                "session_id": self.session_id,
                "session_epoch": self.session_epoch,
                "frame_id": frame_id,
                "agent": agent_name,
                "result_schema": result_schema,
                "digest": digest,
                "result": result,
            },
        )
        return path.relative_to(self.project_root).as_posix()

    def read_page(
        self,
        artifact_ref: str,
        *,
        field: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """安全读取 artifact 元数据或某个结果字段的分页内容。"""
        path = self._resolve_ref(artifact_ref)
        if path.stat().st_size > MAX_DELEGATE_ARTIFACT_BYTES:
            raise ValueError("delegate artifact file exceeds size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("delegate artifact payload must be an object")
        if payload.get("schema") != DELEGATE_ARTIFACT_SCHEMA:
            raise ValueError("unsupported delegate artifact schema")
        if payload.get("session_id") != self.session_id:
            raise ValueError("delegate artifact belongs to another session")
        if self.session_epoch and str(payload.get("session_epoch", "")) != self.session_epoch:
            raise ValueError("delegate artifact belongs to another session epoch")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise ValueError("delegate artifact result must be an object")

        metadata = {
            "artifact_ref": artifact_ref,
            "schema": payload["schema"],
            "result_schema": payload.get("result_schema"),
            "frame_id": payload.get("frame_id"),
            "agent": payload.get("agent"),
            "digest": payload.get("digest"),
            "session_epoch": self.session_epoch,
            "available_fields": sorted(str(key) for key in result),
        }
        if not field:
            return metadata
        if field not in result:
            raise ValueError(f"delegate artifact has no field: {field}")
        value = result[field]
        if isinstance(value, list):
            start = max(0, offset)
            page_limit = max(1, min(limit, MAX_DELEGATE_ARTIFACT_PAGE_ITEMS))
            end = min(len(value), start + page_limit)
            return {
                **metadata,
                "field": field,
                "value": value[start:end],
                "offset": start,
                "limit": page_limit,
                "total": len(value),
                "has_more": end < len(value),
            }
        return {**metadata, "field": field, "value": value, "has_more": False}

    def _resolve_ref(self, artifact_ref: str) -> Path:
        """把工程相对引用解析到当前会话目录内。"""
        if not artifact_ref or Path(artifact_ref).is_absolute():
            raise ValueError("artifact_ref must be a project-relative path")
        session_root = self.session_root.resolve()
        try:
            candidate = (self.project_root / artifact_ref).resolve(strict=True)
        except OSError as exc:
            raise ValueError("delegate artifact was not found") from exc
        if candidate.parent != session_root or candidate.suffix.lower() != ".json":
            raise ValueError("artifact_ref is outside the current session delegate directory")
        return candidate
