"""Small project-local memory store."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


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
        for entry in self._read().get("items", []):
            if not isinstance(entry, dict):
                continue
            try:
                items.append(MemoryItem(**entry))
            except TypeError:
                continue
        return items

    def save(self, text: str, tags: list[str] | None = None, scope: str = "project") -> MemoryItem:
        stripped = text.strip()
        if not stripped:
            raise ValueError("memory text cannot be empty")
        item = MemoryItem(id=str(uuid.uuid4()), text=stripped, tags=tags or [], scope=scope)
        data = self._read()
        items = [entry for entry in data.get("items", []) if isinstance(entry, dict)]
        items.append(asdict(item))
        self._write({"items": items})
        return item

    def delete(self, item_id: str) -> bool:
        data = self._read()
        items = [entry for entry in data.get("items", []) if isinstance(entry, dict)]
        kept = [entry for entry in items if entry.get("id") != item_id]
        self._write({"items": kept})
        return len(kept) != len(items)

    def clear(self) -> int:
        count = len(self.list())
        self._write({"items": []})
        return count

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"items": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"items": []}
        return data if isinstance(data, dict) else {"items": []}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
