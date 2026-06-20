"""Small project-local memory store."""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.storage.atomic import atomic_write_json

logger = logging.getLogger(__name__)

MAX_MEMORY_TEXT_CHARS = 8000
MAX_MEMORY_TAGS = 16
MAX_MEMORY_TAG_CHARS = 64


@dataclass(frozen=True)
class MemoryItem:
    """A user-approved memory entry."""

    id: str
    text: str
    scope: str = "project"
    tags: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class MemoryStore:
    """JSON-backed memory store; never writes secrets automatically."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def list(self) -> list[MemoryItem]:
        items: list[MemoryItem] = []
        for entry in self._items():
            try:
                items.append(MemoryItem(**entry))
            except TypeError:
                continue
        logger.debug("Memory listed path=%s count=%d", self._path, len(items))
        return items

    def save(self, text: str, tags: list[str] | None = None, scope: str = "project") -> MemoryItem:
        stripped = text.strip()
        if not stripped:
            raise ValueError("memory text cannot be empty")
        if len(stripped) > MAX_MEMORY_TEXT_CHARS:
            raise ValueError(f"memory text cannot exceed {MAX_MEMORY_TEXT_CHARS} characters")
        normalized_tags = [
            tag.strip()[:MAX_MEMORY_TAG_CHARS]
            for tag in (tags or [])
            if tag.strip()
        ][:MAX_MEMORY_TAGS]
        item = MemoryItem(id=str(uuid.uuid4()), text=stripped, tags=normalized_tags, scope=scope)
        items = self._items()
        items.append(asdict(item))
        self._write({"items": items})
        logger.info("Memory item saved id=%s scope=%s tags=%d", item.id, item.scope, len(item.tags))
        return item

    def delete(self, item_id: str) -> bool:
        items = self._items()
        kept = [entry for entry in items if entry.get("id") != item_id]
        self._write({"items": kept})
        deleted = len(kept) != len(items)
        logger.info("Memory item delete requested id=%s deleted=%s", item_id, deleted)
        return deleted

    def clear(self) -> int:
        count = len(self.list())
        self._write({"items": []})
        logger.info("Memory store cleared count=%d", count)
        return count

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"items": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Memory read failed path=%s error=%s", self._path, exc)
            return {"items": []}
        return data if isinstance(data, dict) else {"items": []}

    def _items(self) -> list[dict[str, Any]]:
        """读取 `items` 列表，对非列表（含合法 JSON 但 `items: null`）兜底为空。

        旧实现里 `_read().get("items", [])` 在文件内容为 `{"items": null}` 时
        会返回 `None`，随后 `for entry in None` 触发未捕获的 `TypeError`，使
        `/memory` 接口持续 500。这里统一判型，只保留 dict 元素。
        """
        raw = self._read().get("items")
        if not isinstance(raw, list):
            if raw is not None:
                logger.warning("Invalid memory items type path=%s; treating as empty", self._path)
            return []
        return [entry for entry in raw if isinstance(entry, dict)]

    def _write(self, data: dict[str, Any]) -> None:
        atomic_write_json(self._path, data)
        logger.debug("Memory store written path=%s items=%d", self._path, len(data.get("items", [])))
