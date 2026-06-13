"""RAG 能力状态。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.rag.index import CodebaseIndex
from app.security.settings import SecuritySettings


def rag_status(security: SecuritySettings, index_path: Path | None = None) -> dict[str, Any]:
    """返回当前 RAG 子系统状态。"""
    return CodebaseIndex(security, index_path).status()
