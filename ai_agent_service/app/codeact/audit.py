"""保存大小受限且已脱敏的 CodeAct 审计证据。"""

from __future__ import annotations

import re
import hashlib
import json
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SENSITIVE = re.compile(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s]+")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """描述可关联到单个任务执行的无控制语义审计事件。"""

    occurred_at: str
    task_execution_id: str
    kind: str
    payload: dict[str, Any]


class CodeActAuditLog:
    """按任务维护有界时间线，并在持久化前裁剪敏感输出。"""

    def __init__(
        self,
        max_payload_bytes: int,
        max_events_per_task: int = 512,
        storage_root: Path | None = None,
    ) -> None:
        self._max_payload_bytes = max_payload_bytes
        self._max_events_per_task = max_events_per_task
        self._storage_root = storage_root
        self._events: dict[str, deque[AuditEvent]] = defaultdict(
            lambda: deque(maxlen=max_events_per_task)
        )

    def record(self, task_execution_id: str, kind: str, payload: dict[str, Any]) -> None:
        """记录已脱敏、已截断的事件。"""
        self._events[task_execution_id].append(
            AuditEvent(
                occurred_at=datetime.now(UTC).isoformat(),
                task_execution_id=task_execution_id,
                kind=kind,
                payload=self._sanitize(payload),
            )
        )

    def timeline(self, task_execution_id: str) -> list[dict[str, Any]]:
        """返回任务审计的 JSON 安全快照。"""
        persisted = self._load_persisted(task_execution_id)
        active = self._events.get(task_execution_id)
        if active is None:
            return persisted
        return (persisted + [asdict(event) for event in active])[-self._max_events_per_task :]

    def active_execution_ids(self) -> tuple[str, ...]:
        """返回仍占用内存时间线的执行标识。"""
        return tuple(self._events)

    def persist(self, task_execution_id: str) -> None:
        """把一个任务的有界时间线原子写入服务审计目录。"""
        if self._storage_root is None:
            return
        events = self._load_persisted(task_execution_id)
        events.extend(asdict(event) for event in self._events.get(task_execution_id, ()))
        self._storage_root.mkdir(parents=True, exist_ok=True)
        target = self._timeline_path(task_execution_id)
        temporary = target.with_suffix(".tmp")
        payload = {
            "task_execution_id": task_execution_id,
            "events": events[-self._max_events_per_task :],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(target)

    def release(self, task_execution_id: str) -> None:
        """释放一个已持久化任务的内存时间线。"""
        self._events.pop(task_execution_id, None)

    def _timeline_path(self, task_execution_id: str) -> Path:
        """使用执行标识摘要生成不可路径穿越的审计文件名。"""
        assert self._storage_root is not None
        digest = hashlib.sha256(task_execution_id.encode("utf-8")).hexdigest()
        return self._storage_root / f"{digest}.json"

    def _load_persisted(self, task_execution_id: str) -> list[dict[str, Any]]:
        """读取并验证已持久化的任务时间线。"""
        if self._storage_root is None:
            return []
        target = self._timeline_path(task_execution_id)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or payload.get("task_execution_id") != task_execution_id:
            return []
        events = payload.get("events")
        if not isinstance(events, list):
            return []
        return [event for event in events if isinstance(event, dict)][-self._max_events_per_task :]

    def _sanitize(self, value: Any) -> dict[str, Any]:
        """递归处理文本敏感字段，并对整体负载设置字节上限。"""
        normalized = self._sanitize_value(value)
        rendered = repr(normalized).encode("utf-8", errors="replace")
        if len(rendered) <= self._max_payload_bytes:
            return normalized if isinstance(normalized, dict) else {"value": normalized}
        return {
            "truncated": True,
            "preview": rendered[: self._max_payload_bytes].decode("utf-8", errors="replace"),
        }

    def _sanitize_value(self, value: Any) -> Any:
        """递归替换 credential-like 文本，保留证据结构。"""
        if isinstance(value, str):
            return _SENSITIVE.sub(r"\1=[REDACTED]", value)
        if isinstance(value, dict):
            return {str(key): self._sanitize_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_value(item) for item in value]
        if isinstance(value, tuple):
            return [self._sanitize_value(item) for item in value]
        return value
