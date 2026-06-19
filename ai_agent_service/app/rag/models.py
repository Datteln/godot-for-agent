"""EARS 检索管线使用的统一数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchResult:
    """所有检索通道共享的结果格式。"""

    id: str
    content: str
    source: str
    score: float
    file_path: str
    span: tuple[int, int] = (0, 0)
    graph_meta: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.score = max(0.0, min(1.0, float(self.score)))

    def to_dict(self, *, legacy: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "id": self.id,
            "content": self.content,
            "source": self.source,
            "score": round(self.score, 6),
            "file_path": self.file_path,
            "span": list(self.span),
            "graph_meta": self.graph_meta,
        }
        if legacy:
            value.update(
                {
                    "path": self.file_path,
                    "start_line": self.span[0],
                    "end_line": self.span[1],
                    "snippet": self.content,
                    "partial_view": True,
                }
            )
        return value


@dataclass
class GraphNode:
    id: str
    kind: str
    label: str
    file_path: str = ""
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    edge_type: str
    strength: float = 1.0
