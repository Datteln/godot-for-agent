"""RAG 临时上下文层（L3）内容组装（§16.1 / RAG 段缓存）。

在用户提问到达时，用本地代码库索引检索与问题最相关的若干片段，拼成分层 prompt
的 L3 临时上下文层。该层在一整轮 agent 循环（多次工具往返）内保持不变，因此为它
单独标记 `cache_control` 后，循环内的多次 LLM 调用能复用同一段 L3 缓存前缀；下一轮
用户提问换了检索结果时，L3 随 `rag_fingerprint`/内容变化而失效，而 L0/L2 仍命中。

仅在持久化索引存在时检索（避免每条消息都触发整库即时扫描）；检索失败或无结果时
返回空串，不产出 L3 层，行为与未启用 RAG 段时一致。
"""

from __future__ import annotations

import logging

from app.rag.index import CodebaseIndex

logger = logging.getLogger(__name__)

# L3 RAG 段最多注入的片段数与字符上限：片段直接进入每轮固定输入，过多反而抬高
# 成本、稀释注意力；超出预算即截断。
_MAX_SNIPPETS = 4
_MAX_CONTEXT_CHARS = 3000

_HEADER = "相关代码片段（自动检索，仅供参考；写文件前必须用 read_file 读取完整文件）："


def build_rag_context(index: CodebaseIndex, query: str) -> str:
    """检索并组装 L3 RAG 上下文文本；无索引/无结果/出错时返回空串。

    Args:
        index: 本地代码库索引。
        query: 当前用户提问（用于检索）。

    Returns:
        形如 "相关代码片段……\\n--- path:start-end ---\\n<snippet>" 的有界文本；
        没有持久化索引或检索不到相关片段时为空串。
    """
    if not query.strip():
        return ""
    # 只用已构建的持久化索引，不为单条消息触发整库即时扫描（成本不可控）。
    if not index.path.exists():
        return ""
    try:
        result = index.search(query, max_results=_MAX_SNIPPETS)
    except Exception as exc:  # 检索是非关键增强，任何失败都不应阻断聊天主流程
        logger.debug("RAG context retrieval skipped query_len=%d error=%s", len(query), exc)
        return ""

    results = result.get("results", [])
    if not isinstance(results, list) or not results:
        return ""

    parts: list[str] = [_HEADER]
    used = len(_HEADER)
    for item in results[:_MAX_SNIPPETS]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", ""))
        start = item.get("start_line", "")
        end = item.get("end_line", "")
        snippet = str(item.get("snippet", "")).strip()
        if not path or not snippet:
            continue
        block = f"\n--- {path}:{start}-{end} ---\n{snippet}"
        if used + len(block) > _MAX_CONTEXT_CHARS:
            break
        parts.append(block)
        used += len(block)

    if len(parts) == 1:
        return ""
    logger.debug("RAG context layer built snippets=%d chars=%d", len(parts) - 1, used)
    return "\n".join(parts)
