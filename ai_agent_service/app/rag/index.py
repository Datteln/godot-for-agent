"""本地代码库 RAG 索引。"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from app.security.paths import path_ok
from app.security.settings import SecuritySettings

SCHEMA_VERSION = 2
MAX_FILE_BYTES = 256 * 1024
MAX_SNIPPET_CHARS = 1200
CHUNK_LINES = 48
TEXT_SUFFIXES = {
    ".gd",
    ".cs",
    ".tscn",
    ".tres",
    ".gdshader",
    ".json",
    ".cfg",
    ".md",
    ".txt",
    ".ini",
    ".import",
}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchResult:
    """单条 RAG 检索结果。"""

    path: str
    score: float
    start_line: int
    end_line: int
    snippet: str
    partial_view: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转换为可 JSON 序列化的字典。"""
        return {
            "path": self.path,
            "score": round(self.score, 6),
            "start_line": self.start_line,
            "end_line": self.end_line,
            "snippet": self.snippet,
            "partial_view": self.partial_view,
        }


def default_index_path(security: SecuritySettings) -> Path:
    """返回工程内默认 RAG 索引路径。"""
    return security.project_root / ".ai_agent_service" / "rag_index.json"


def validate_glob(include: str) -> None:
    """校验 glob 不越过工程根。"""
    path = Path(include)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("include 不允许使用绝对路径或 '..'")


def token_counts(text: str) -> Counter[str]:
    """把文本拆为检索 token 频次。"""
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}", text)
    return Counter(token.lower() for token in tokens)


def _read_text(path: Path) -> str:
    """读取一个文本文件的有限前缀。"""
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        data = data[:MAX_FILE_BYTES]
    return data.decode("utf-8", errors="ignore")


def _chunk_lines(lines: list[str]) -> list[tuple[int, int, str]]:
    """按固定行数把文件切成检索片段。"""
    chunks: list[tuple[int, int, str]] = []
    for start in range(0, len(lines), CHUNK_LINES):
        end = min(len(lines), start + CHUNK_LINES)
        snippet = "\n".join(lines[start:end])
        if len(snippet) > MAX_SNIPPET_CHARS:
            snippet = snippet[:MAX_SNIPPET_CHARS] + "\n..."
        chunks.append((start + 1, end, snippet))
    return chunks


def _iter_text_files(root: Path, security: SecuritySettings, include: str) -> list[Path]:
    """枚举安全边界内的文本文件。"""
    validate_glob(include)
    candidates: list[Path] = []
    for candidate in sorted(root.glob(include)):
        if not candidate.is_file() or candidate.suffix.lower() not in TEXT_SUFFIXES:
            continue
        rel = candidate.relative_to(root).as_posix()
        if rel == ".ai_agent_service" or rel.startswith(".ai_agent_service/"):
            continue
        if path_ok(rel, security, write=False):
            candidates.append(candidate)
    return candidates


class CodebaseIndex:
    """工程内纯本地 TF-IDF 风格检索索引。"""

    def __init__(
        self,
        security: SecuritySettings,
        index_path: Path | None = None,
        embedding_client: Any | None = None,
        asset_llm_client: Any | None = None,
        asset_enabled: bool = False,
        *,
        query_router_enabled: bool = True,
        graph_max_depth: int = 2,
        graph_max_neighbors: int = 5,
        rerank_model: str = "",
        rerank_timeout_s: float = 2.0,
        token_budget: int = 1500,
    ) -> None:
        """初始化索引读写器。"""
        self._security = security
        self._index_path = index_path or default_index_path(security)
        self._embedding_client = embedding_client
        self._asset_llm_client = asset_llm_client
        self._asset_enabled = asset_enabled
        self._query_router_enabled = query_router_enabled
        self._graph_max_depth = graph_max_depth
        self._graph_max_neighbors = graph_max_neighbors
        self._rerank_model = rerank_model
        self._rerank_timeout_s = rerank_timeout_s
        self.token_budget = token_budget

    @property
    def index_dir(self) -> Path:
        return self._index_path.parent

    @property
    def embedding_path(self) -> Path:
        return self.index_dir / "rag_embeddings.json"

    @property
    def symbol_path(self) -> Path:
        return self.index_dir / "rag_symbols.json"

    @property
    def scene_graph_path(self) -> Path:
        return self.index_dir / "scene_graph.json"

    @property
    def signal_graph_path(self) -> Path:
        return self.index_dir / "signal_graph.json"

    @property
    def asset_index_path(self) -> Path:
        return self.index_dir / "asset_index.json"

    @property
    def path(self) -> Path:
        """返回索引文件路径。"""
        return self._index_path

    def status(self) -> dict[str, Any]:
        """返回当前索引文件状态。"""
        if not self._index_path.exists():
            logger.debug("RAG index status missing path=%s", self._index_path)
            return {
                "enabled": True,
                "mode": "local_tfidf",
                "tool": "search_codebase",
                "index_required": False,
                "index_exists": False,
                "index_path": str(self._index_path),
            }
        try:
            data = self._load()
        except ValueError as exc:
            logger.warning("RAG index status invalid path=%s error=%s", self._index_path, exc)
            return {
                "enabled": True,
                "mode": "local_tfidf",
                "tool": "search_codebase",
                "index_required": False,
                "index_exists": True,
                "index_path": str(self._index_path),
                "error": str(exc),
            }
        status = {
            "enabled": True,
            "mode": data.get("mode", "local_tfidf"),
            "tool": "search_codebase",
            "index_required": False,
            "index_exists": True,
            "index_path": str(self._index_path),
            "built_at": data.get("built_at"),
            "files": data.get("files", 0),
            "chunks": len(data.get("chunks", [])),
            "schema_version": data.get("schema_version"),
        }
        logger.debug(
            "RAG index status path=%s files=%s chunks=%s",
            self._index_path,
            status.get("files"),
            status.get("chunks"),
        )
        return status

    def build(self, include: str = "**/*", max_files: int = 4000, incremental: bool = True) -> dict[str, Any]:
        """扫描工程并增量更新代码、符号、向量及引擎图索引。"""
        root = self._security.project_root
        logger.info("RAG index build start root=%s include=%s max_files=%d", root, include, max_files)
        files = _iter_text_files(root, self._security, include)
        old_data: dict[str, Any] = {}
        if incremental and self._index_path.exists():
            try:
                old_data = self._load(allow_legacy=True)
            except ValueError:
                old_data = {}
        old_chunks = [c for c in old_data.get("chunks", []) if isinstance(c, dict)]
        old_states = old_data.get("file_states", {}) if isinstance(old_data.get("file_states", {}), dict) else {}
        current_states: dict[str, dict[str, int]] = {}
        file_map = {candidate.relative_to(root).as_posix(): candidate for candidate in files[:max_files]}
        for rel, candidate in file_map.items():
            stat = candidate.stat()
            current_states[rel] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
        changed_paths = {
            rel for rel, state in current_states.items()
            if not incremental or old_states.get(rel) != state
        } | (set(old_states) - set(current_states))
        chunks: list[dict[str, Any]] = [c for c in old_chunks if c.get("path") not in changed_paths] if incremental else []
        indexed_files = 0
        for rel, candidate in file_map.items():
            indexed_files += 1
            if incremental and rel not in changed_paths:
                continue
            text = _read_text(candidate)
            lines = text.splitlines()
            if not lines:
                continue
            stat = candidate.stat()
            for start_line, end_line, snippet in _chunk_lines(lines):
                counts = token_counts(snippet)
                if not counts:
                    continue
                chunks.append(
                    {
                        "path": rel,
                        "start_line": start_line,
                        "end_line": end_line,
                        "snippet": snippet,
                        "tokens": dict(counts),
                        "mtime_ns": stat.st_mtime_ns,
                        "size": stat.st_size,
                    }
                )

        data = {
            "schema_version": SCHEMA_VERSION,
            "mode": "ears_hybrid",
            "built_at": time.time(),
            "project_root": str(root),
            "include": include,
            "files": indexed_files,
            "file_states": current_states,
            "chunks": chunks,
        }
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        result = {
            "mode": "ears_hybrid",
            "index_rebuilt": True,
            "index_path": str(self._index_path),
            "files": indexed_files,
            "chunks": len(chunks),
            "truncated_files": len(files) > max_files,
            "changed_files": len(changed_paths),
        }
        # Optional sub-index failures are isolated: BM25 remains usable.
        try:
            from app.rag.symbol_index import SymbolIndex

            result["symbols"] = SymbolIndex(self.symbol_path).build(root, list(file_map.values()), changed_paths if incremental else None)
        except Exception as exc:
            logger.warning("Symbol index build skipped: %s", exc)
        try:
            from app.rag.embedding_index import EmbeddingIndex

            embedding = EmbeddingIndex(self.embedding_path, self._embedding_client)
            result["vectors"] = embedding.build(chunks, changed_paths if incremental else None)
        except Exception as exc:
            logger.warning("Embedding index build skipped: %s", exc)
        try:
            from app.rag.engine.scene_graph_index import SceneGraphIndex
            from app.rag.engine.signal_graph_index import SignalGraphIndex

            result["scene_graph"] = SceneGraphIndex(self.scene_graph_path).build(root, list(file_map.values()), incremental)
            result["signals"] = SignalGraphIndex(self.signal_graph_path).build(root, list(file_map.values()), incremental)
        except Exception as exc:
            logger.warning("Engine graph build skipped: %s", exc)
        if self._asset_enabled:
            try:
                from app.rag.engine.asset_index import AssetIndex

                result["assets"] = AssetIndex(self.asset_index_path, self._asset_llm_client, enabled=True).build(root, incremental=incremental)
            except Exception as exc:
                logger.warning("Asset index build skipped: %s", exc)
        logger.info(
            "RAG index build complete path=%s files=%d chunks=%d truncated=%s",
            self._index_path,
            indexed_files,
            len(chunks),
            result["truncated_files"],
        )
        return result

    def search(self, query: str, include: str = "**/*", max_results: int = 16) -> dict[str, Any]:
        """基于持久化索引搜索相关片段。"""
        logger.info(
            "RAG search start mode=auto include=%s max_results=%d query_length=%d",
            include,
            max_results,
            len(query),
        )
        query_counts = token_counts(query)
        if not query_counts:
            raise ValueError("query 至少需要包含一个可检索词")
        validate_glob(include)
        if not self._index_path.exists():
            return self.search_live(query, include, max_results)

        data = self._load()
        chunks = data.get("chunks", [])
        if not isinstance(chunks, list):
            raise ValueError("索引 chunks 格式不正确")
        # Public API remains a dict with legacy fields, while ranking now uses EARS.
        try:
            unified = self.hybrid_search(query, max_results=max_results)
            if include != "**/*":
                unified = [item for item in unified if fnmatch(item.file_path, include)]
            logger.info("RAG search complete mode=ears_hybrid results=%d", len(unified))
            return {
                "query": query,
                "mode": "ears_hybrid",
                "index_path": str(self._index_path),
                "results": [item.to_dict(legacy=True) for item in unified[:max_results]],
                "truncated": len(unified) >= max_results,
                "note": "这是索引片段视图；写文件前必须用 read_file 读取完整文件。",
            }
        except Exception as exc:
            logger.warning("EARS search degraded to keyword index: %s", exc)
        results = self._rank_chunks(query_counts, chunks, include, max_results)
        logger.info(
            "RAG search complete mode=index path=%s chunks=%d results=%d",
            self._index_path,
            len(chunks),
            len(results),
        )
        return {
            "query": query,
            "mode": "local_tfidf_index",
            "index_path": str(self._index_path),
            "results": [result.to_dict() for result in results],
            "truncated": len(results) >= max_results,
            "note": "这是索引片段视图；写文件前必须用 read_file 读取完整文件。",
        }

    def search_live(self, query: str, include: str = "**/*", max_results: int = 16) -> dict[str, Any]:
        """在无索引时即时扫描工程并搜索。"""
        logger.info(
            "RAG live search start include=%s max_results=%d query_length=%d",
            include,
            max_results,
            len(query),
        )
        query_counts = token_counts(query)
        if not query_counts:
            raise ValueError("query 至少需要包含一个可检索词")
        files = _iter_text_files(self._security.project_root, self._security, include)
        chunks: list[dict[str, Any]] = []
        for candidate in files:
            rel = candidate.relative_to(self._security.project_root).as_posix()
            lines = _read_text(candidate).splitlines()
            for start_line, end_line, snippet in _chunk_lines(lines):
                counts = token_counts(snippet)
                if counts:
                    chunks.append(
                        {
                            "path": rel,
                            "start_line": start_line,
                            "end_line": end_line,
                            "snippet": snippet,
                            "tokens": dict(counts),
                        }
                    )
        results = self._rank_chunks(query_counts, chunks, include, max_results)
        logger.info(
            "RAG live search complete scanned_files=%d chunks=%d results=%d",
            len(files),
            len(chunks),
            len(results),
        )
        return {
            "query": query,
            "mode": "live_tfidf_scan",
            "scanned_files": len(files),
            "results": [result.to_dict() for result in results],
            "truncated": len(results) >= max_results,
            "note": "未找到持久化索引，已即时扫描；可运行 /index rebuild 提升后续检索速度。",
        }

    def _load(self, allow_legacy: bool = False) -> dict[str, Any]:
        """读取并校验索引文件。"""
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("RAG index load failed path=%s error=%s", self._index_path, exc)
            raise ValueError(f"无法读取 RAG 索引：{exc}") from exc
        valid_versions = {SCHEMA_VERSION, 1} if allow_legacy else {SCHEMA_VERSION}
        if not isinstance(data, dict) or data.get("schema_version") not in valid_versions:
            logger.warning("RAG index schema mismatch path=%s", self._index_path)
            raise ValueError("RAG 索引 schema_version 不匹配，请重建索引")
        return data

    def keyword_results(self, query: str, limit: int = 10) -> list[Any]:
        """返回统一 SearchResult 格式的 BM25/TF-IDF 结果。"""
        from app.rag.models import SearchResult as UnifiedResult

        query_counts = token_counts(query)
        if not query_counts or not self._index_path.exists():
            return []
        data = self._load()
        ranked = self._rank_chunks(query_counts, data.get("chunks", []), "**/*", limit)
        maximum = max((item.score for item in ranked), default=1.0) or 1.0
        return [UnifiedResult(
            id=f"chunk:{item.path}:{item.start_line}", content=item.snippet, source="kw",
            score=item.score / maximum, file_path=item.path, span=(item.start_line, item.end_line),
        ) for item in ranked]

    def hybrid_search(self, query: str, max_results: int = 4, router_enabled: bool = True) -> list[Any]:
        """执行完整 EARS 管线并返回统一结果。"""
        from app.rag.embedding_index import EmbeddingIndex
        from app.rag.engine.asset_index import AssetIndex
        from app.rag.engine.scene_graph_index import SceneGraphIndex
        from app.rag.engine.signal_graph_index import SignalGraphIndex
        from app.rag.graph_fusion import GraphFusion, merge_graphs
        from app.rag.graph_reranker import GraphAwareReranker
        from app.rag.hybrid import HybridRetriever
        from app.rag.query_router import QueryRouter
        from app.rag.symbol_index import SymbolIndex

        embedding = EmbeddingIndex(self.embedding_path, self._embedding_client)
        symbols = SymbolIndex(self.symbol_path)
        scene = SceneGraphIndex(self.scene_graph_path)
        scene.load()
        signal = SignalGraphIndex(self.signal_graph_path)
        signal.load()
        asset = AssetIndex(
            self.asset_index_path, self._asset_llm_client, enabled=self._asset_enabled
        )
        asset.load()
        nodes, edges = merge_graphs(scene, signal, asset)
        channels = {
            "kw": lambda q, n: self.keyword_results(q, n),
            "vec": embedding.search,
            "sym": symbols.search,
            "scene_graph": scene.search,
            "signal_graph": signal.search,
            "asset": asset.search,
        }
        return HybridRetriever(
            channels,
            router=QueryRouter(router_enabled and self._query_router_enabled),
            graph_fusion=GraphFusion(
                nodes,
                edges,
                max_depth=self._graph_max_depth,
                max_neighbors=self._graph_max_neighbors,
            ),
            reranker=GraphAwareReranker(
                model=self._rerank_model,
                timeout_s=self._rerank_timeout_s,
                top_n=10,
                top_k=max_results,
                alpha=0.7,
                beta=0.2,
                gamma=0.1,
            ),
            final_limit=max_results,
        ).search(query)

    def _rank_chunks(
        self,
        query_counts: Counter[str],
        chunks: list[Any],
        include: str,
        max_results: int,
    ) -> list[SearchResult]:
        """对索引片段进行 TF-IDF 风格排序。"""
        valid_chunks = [chunk for chunk in chunks if isinstance(chunk, dict)]
        document_frequency: Counter[str] = Counter()
        for chunk in valid_chunks:
            tokens = chunk.get("tokens", {})
            if isinstance(tokens, dict):
                document_frequency.update(tokens.keys())

        total = max(len(valid_chunks), 1)
        query_norm = math.sqrt(sum(count * count for count in query_counts.values())) or 1.0
        ranked: list[SearchResult] = []
        for chunk in valid_chunks:
            path = str(chunk.get("path", ""))
            if include != "**/*" and not fnmatch(path, include):
                continue
            if not path_ok(path, self._security, write=False):
                continue
            raw_tokens = chunk.get("tokens", {})
            if not isinstance(raw_tokens, dict):
                continue
            chunk_counts = Counter({str(k): int(v) for k, v in raw_tokens.items()})
            dot = 0.0
            chunk_norm = 0.0
            for token, count in chunk_counts.items():
                idf = math.log((total + 1) / (document_frequency[token] + 1)) + 1.0
                weighted = count * idf
                chunk_norm += weighted * weighted
                if token in query_counts:
                    dot += query_counts[token] * weighted
            if dot <= 0:
                continue
            path_bonus = sum(0.15 for token in query_counts if token in path.lower())
            score = (dot / ((math.sqrt(chunk_norm) or 1.0) * query_norm)) + path_bonus
            ranked.append(
                SearchResult(
                    path=path,
                    score=score,
                    start_line=int(chunk.get("start_line", 1)),
                    end_line=int(chunk.get("end_line", 1)),
                    snippet=str(chunk.get("snippet", "")),
                )
            )

        ranked.sort(key=lambda item: (-item.score, item.path, item.start_line))
        return ranked[:max_results]
