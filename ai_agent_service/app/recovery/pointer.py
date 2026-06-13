"""最小恢复指针（§14.3）。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass(frozen=True)
class RecoveryPointer:
    """不含敏感信息的恢复指针。"""

    session_id: str
    last_event_seq: int
    pending_turn_id: str | None
    project_hash: str
    updated_at: str


def _project_hash(project_root: Path) -> str:
    raw = str(project_root.resolve()).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]


class RecoveryPointerStore:
    """恢复指针本地存储。"""

    def __init__(self, path: Path, project_root: Path) -> None:
        self._path = path
        self._project_root = project_root
        self._project_hash = _project_hash(project_root)

    def write(self, session_id: str, pending_turn_id: str | None, last_event_seq: int) -> None:
        """写入最新恢复指针。"""
        pointer = RecoveryPointer(
            session_id=session_id,
            pending_turn_id=pending_turn_id,
            last_event_seq=last_event_seq,
            project_hash=self._project_hash,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(asdict(pointer), ensure_ascii=False, indent=2), encoding="utf-8")

    def read(self) -> RecoveryPointer | None:
        """读取指针；工程不匹配或文件损坏时返回 None。"""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            pointer = RecoveryPointer(**data)
        except (OSError, TypeError, ValueError):
            return None
        if pointer.project_hash != self._project_hash:
            return None
        return pointer

    def clear(self) -> None:
        """清理恢复指针。"""
        if self._path.exists():
            self._path.unlink()
