"""EARS 动态上下文（L3）检索与 token-aware 打包。"""

from __future__ import annotations

import logging
import re
from collections import defaultdict

from app.rag.index import CodebaseIndex
from app.rag.models import SearchResult

logger = logging.getLogger(__name__)
_MAX_SNIPPETS = 4
_TOKEN_BUDGET = 1500
_HEADER = "相关代码片段（自动检索，仅供参考；写文件前必须用 read_file 读取完整文件）："


def _tokens(text: str) -> int:
    try:
        import tiktoken  # type: ignore[import-not-found]

        return len(tiktoken.get_encoding("cl100k_base").encode(text))
    except Exception:
        # Unicode-aware local tokenizer is deterministic and more precise than a character ratio.
        return len(re.findall(r"[A-Za-z_]\w*|[\u4e00-\u9fff]|[^\s]", text))


def _similarity(left: str, right: str) -> float:
    a = set(re.findall(r"\w+", left.lower()))
    b = set(re.findall(r"\w+", right.lower()))
    return len(a & b) / len(a | b) if a or b else 1.0


def pack_results(results: list[SearchResult], token_budget: int = _TOKEN_BUDGET) -> str:
    """按相关度去重、按文件聚合，并严格遵守 token 预算。"""
    selected: list[SearchResult] = []
    for item in sorted(results, key=lambda value: (-value.score, value.id)):
        if any(_similarity(item.content, previous.content) >= 0.85 for previous in selected):
            continue
        selected.append(item)
        if len(selected) >= _MAX_SNIPPETS:
            break
    groups: defaultdict[str, list[SearchResult]] = defaultdict(list)
    for item in selected:
        groups[item.file_path or item.id].append(item)
    ordered_groups = sorted(groups.items(), key=lambda pair: -max(item.score for item in pair[1]))
    parts = [_HEADER]
    used = _tokens(_HEADER)
    for path, items in ordered_groups:
        blocks = []
        for item in sorted(items, key=lambda value: (-value.score, value.span[0])):
            location = f"{item.span[0]}-{item.span[1]}" if item.span != (0, 0) else item.source
            blocks.append(f"[{location}]\n{item.content.strip()}")
        block = f"\n--- {path} ---\n" + "\n\n".join(blocks)
        cost = _tokens(block)
        if used + cost > token_budget:
            continue
        parts.append(block)
        used += cost
    packed = "\n".join(parts) if len(parts) > 1 else ""
    logger.debug(
        "RAG context packed candidates=%d selected=%d deduplicated=%d files=%d "
        "tokens_used=%d token_budget=%d output_chars=%d",
        len(results),
        len(selected),
        len(results) - len(selected),
        max(0, len(parts) - 1),
        used if packed else 0,
        token_budget,
        len(packed),
    )
    return packed


def build_rag_context(index: CodebaseIndex, query: str) -> str:
    """保持原接口不变，内部切换为 Hybrid + Graph EARS 管线。"""
    if not query.strip():
        logger.debug("RAG context skipped reason=blank_query")
        return ""
    if not index.path.exists():
        logger.debug("RAG context skipped reason=index_missing path=%s", index.path)
        return ""
    retrieval_mode = "hybrid"
    try:
        results = index.hybrid_search(query, max_results=_MAX_SNIPPETS)
    except Exception as exc:
        logger.warning("Hybrid RAG failed; falling back to keyword search: %s", exc)
        retrieval_mode = "keyword_fallback"
        try:
            results = index.keyword_results(query, _MAX_SNIPPETS)
        except Exception as fallback_exc:
            logger.warning("RAG keyword fallback failed error=%s", fallback_exc)
            return ""
    context = pack_results(results, token_budget=index.token_budget)
    logger.info(
        "RAG context build complete mode=%s query_length=%d results=%d context_chars=%d",
        retrieval_mode,
        len(query),
        len(results),
        len(context),
    )
    return context
