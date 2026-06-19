"""项目上下文层（L2）内容组装（§16.1 / 项目级缓存）。

从工程根目录读取一份"工程说明"文档（CLAUDE.md / AGENTS.md / README 等）作为
分层 prompt 的 L2 项目上下文：这类文档跨会话稳定，放进独立缓存层后能让同一
工程的多次会话复用同一段缓存前缀。仅读取已存在的文档、按字符数截断，工程没有
任何说明文档时返回空串（不产出 L2 层，行为与改造前一致）。
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 按优先级查找的工程说明文档；取第一个存在且非空的。
_PROJECT_DOC_CANDIDATES = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".ai_agent/PROJECT.md",
    "README.md",
)

# L2 项目上下文的字符上限：工程文档可能很长，但缓存层只需稳定的概览，过长
# 反而抬高每轮固定输入；超出部分截断。
_PROJECT_CONTEXT_MAX_CHARS = 4000


def build_project_context(project_root: Path) -> str:
    """组装 L2 项目上下文文本；无可用文档时返回空串。

    Args:
        project_root: 当前安全边界的工程根目录。

    Returns:
        形如 "项目背景（来自 CLAUDE.md）：\\n<bounded excerpt>" 的文本；
        没有任何工程说明文档时为空串。
    """
    for name in _PROJECT_DOC_CANDIDATES:
        path = project_root / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > _PROJECT_CONTEXT_MAX_CHARS:
            text = text[:_PROJECT_CONTEXT_MAX_CHARS] + "\n…（已截断）"
        logger.debug("Project context layer loaded source=%s chars=%d", name, len(text))
        base = f"项目背景（来自 {name}）：\n{text}"
        return _with_structure_summary(project_root, base)
    return _with_structure_summary(project_root, "")


def _with_structure_summary(project_root: Path, base: str) -> str:
    """追加确定性的 L2.5 场景图摘要；不包含任何运行时检索结果。"""
    graph_path = project_root / ".ai_agent_service" / "scene_graph.json"
    summaries: list[str] = [base] if base else []
    try:
        from app.rag.engine.scene_graph_index import SceneGraphIndex

        graph = SceneGraphIndex(graph_path)
        graph.load()
        summary = graph.summary()
    except Exception as exc:
        logger.debug("Scene graph summary unavailable: %s", exc)
        summary = ""
    if summary:
        summaries.append(summary)
    symbol_path = project_root / ".ai_agent_service" / "rag_symbols.json"
    if symbol_path.exists():
        try:
            from app.rag.symbol_index import SymbolIndex

            symbols = SymbolIndex(symbol_path)
            symbol_summary = symbols.summary(limit=100)
            if symbol_summary:
                summaries.append("项目符号摘要：\n" + symbol_summary)
        except Exception as exc:
            logger.debug("Symbol summary unavailable: %s", exc)
    context = "\n\n".join(summaries)
    logger.debug(
        "Project structure context built base=%s scene_summary=%s symbol_summary=%s chars=%d",
        bool(base),
        bool(summary),
        any(part.startswith("项目符号摘要：") for part in summaries),
        len(context),
    )
    return context
