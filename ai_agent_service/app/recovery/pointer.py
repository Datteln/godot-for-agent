"""最小恢复指针（§14.3）。"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage.atomic import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecoveryPointer:
    """不含敏感信息的恢复指针。"""

    session_id: str
    last_event_seq: int
    pending_turn_id: str | None
    project_hash: str
    updated_at: str
    session_epoch: str = ""
    map_checkpoint: dict[str, Any] | None = None


def _project_hash(project_root: Path) -> str:
    raw = str(project_root.resolve()).encode("utf-8", errors="ignore")
    return hashlib.sha256(raw).hexdigest()[:16]


class RecoveryPointerStore:
    """按 session 保存恢复指针，并兼容读取旧版单指针文件。"""

    def __init__(self, path: Path, project_root: Path) -> None:
        self._path = path
        self._project_root = project_root
        self._project_hash = _project_hash(project_root)

    def write(
        self,
        session_id: str,
        pending_turn_id: str | None,
        last_event_seq: int,
        map_checkpoint: dict[str, Any] | None = None,
        session_epoch: str = "",
    ) -> None:
        """写入指定会话的最新恢复指针，不覆盖其他会话。"""
        pointer = RecoveryPointer(
            session_id=session_id,
            pending_turn_id=pending_turn_id,
            last_event_seq=last_event_seq,
            project_hash=self._project_hash,
            updated_at=datetime.now(timezone.utc).isoformat(),
            session_epoch=session_epoch,
            map_checkpoint=map_checkpoint,
        )
        # v2 格式：先读取全部已有指针，再按 session_id 覆盖/新增当前项，
        # 保证多会话并发写入时互不覆盖
        pointers = self._read_all()
        pointers[session_id] = pointer
        atomic_write_json(
            self._path,
            {
                "version": 2,
                "pointers": {key: asdict(value) for key, value in pointers.items()},
            },
        )
        logger.info(
            "Recovery pointer written session=%s pending_turn=%s last_event_seq=%d path=%s",
            session_id,
            pending_turn_id,
            last_event_seq,
            self._path,
        )

    def _read_all(self) -> dict[str, RecoveryPointer]:
        """读取当前工程的全部恢复指针。

        兼容两种磁盘格式：
        - v2：{"version": 2, "pointers": {session_id: {...}, ...}}，按 session_id 索引
        - v1（旧版）：顶层即为单条 RecoveryPointer 字段，无 "pointers" 键
        读取后只保留 project_hash 匹配当前工程的条目，丢弃其他工程或损坏数据。
        """
        if not self._path.exists():
            logger.debug("Recovery pointer missing path=%s", self._path)
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            # 判断磁盘格式：有 "pointers" 键为 v2，否则把整条 data 当作 v1 单指针
            raw_pointers = data.get("pointers") if isinstance(data, dict) else None
            if isinstance(raw_pointers, dict):
                candidates = raw_pointers.values()
            elif isinstance(data, dict):
                # v1 向后兼容：整个文件就是一条指针，包装成单元素列表统一处理
                candidates = [data]
            else:
                candidates = []
            pointers: dict[str, RecoveryPointer] = {}
            for raw_pointer in candidates:
                if not isinstance(raw_pointer, dict):
                    continue
                normalized = dict(raw_pointer)
                # 旧版可能没有 map_checkpoint 字段或其值不是 dict，统一归一化为 None
                if not isinstance(normalized.get("map_checkpoint"), dict):
                    normalized["map_checkpoint"] = None
                if not isinstance(normalized.get("session_epoch"), str):
                    normalized["session_epoch"] = ""
                pointer = RecoveryPointer(**normalized)
                # 按 project_hash 过滤：只保留属于当前工程的指针
                if pointer.project_hash == self._project_hash:
                    pointers[pointer.session_id] = pointer
        except (OSError, TypeError, ValueError) as exc:
            logger.warning("Recovery pointer read failed path=%s error=%s", self._path, exc)
            return {}
        return pointers

    def read(self, session_id: str | None = None) -> RecoveryPointer | None:
        """读取指定会话指针；未指定时返回最近更新的一项。

        Args:
            session_id: 指定会话 ID 时精确查找；为 None 时取 updated_at 最大者，
                        方便单会话场景下无需记住 session_id。
        """
        pointers = self._read_all()
        # 指定 session_id 则精确匹配；否则按 updated_at 取最新一条
        pointer = (
            pointers.get(session_id)
            if session_id is not None
            else max(pointers.values(), key=lambda item: item.updated_at, default=None)
        )
        if pointer is None:
            return None
        logger.debug(
            "Recovery pointer read session=%s pending_turn=%s",
            pointer.session_id,
            pointer.pending_turn_id,
        )
        return pointer

    def clear(self, session_id: str | None = None) -> None:
        """清理一个会话；未指定 session 时清理全部指针。

        Args:
            session_id: 指定时仅移除该会话的指针，其余会话保留；
                        为 None 时清空全部指针并删除文件。
        """
        if not self._path.exists():
            return
        pointers = self._read_all()
        if session_id is None:
            # 不传 session_id：清空全部指针
            pointers.clear()
        else:
            # 传 session_id：仅移除该会话，不影响其他会话的恢复状态
            pointers.pop(session_id, None)
        if pointers:
            # 仍有其他会话的指针：重写文件（v2 格式）
            atomic_write_json(
                self._path,
                {
                    "version": 2,
                    "pointers": {key: asdict(value) for key, value in pointers.items()},
                },
            )
        else:
            # 全部清空后直接删除文件
            self._path.unlink(missing_ok=True)
        logger.info("Recovery pointer cleared path=%s session=%s", self._path, session_id)
