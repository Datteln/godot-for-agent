"""RAG 能力状态。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.rag.index import CodebaseIndex
from app.security.settings import SecuritySettings

logger = logging.getLogger(__name__)


def rag_status(security: SecuritySettings, index_path: Path | None = None) -> dict[str, Any]:
    """返回当前 RAG 子系统状态。"""
    index = CodebaseIndex(security, index_path)
    status = index.status()
    status["strategy"] = "ears_v2.2"
    status["sub_indexes"] = {
        "embedding": {"exists": index.embedding_path.exists(), "path": str(index.embedding_path)},
        "symbol": {"exists": index.symbol_path.exists(), "path": str(index.symbol_path)},
        "scene_graph": {"exists": index.scene_graph_path.exists(), "path": str(index.scene_graph_path)},
        "signal_graph": {"exists": index.signal_graph_path.exists(), "path": str(index.signal_graph_path)},
        "asset": {"exists": index.asset_index_path.exists(), "path": str(index.asset_index_path)},
    }
    logger.debug(
        "RAG status collected index_exists=%s mode=%s sub_indexes=%s",
        status.get("index_exists"),
        status.get("mode"),
        {name: value["exists"] for name, value in status["sub_indexes"].items()},
    )
    return status
