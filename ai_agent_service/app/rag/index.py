"""本地代码库 RAG 索引。"""

from __future__ import annotations

import json
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

SCHEMA_VERSION = 1
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

    def __init__(self, security: SecuritySettings, index_path: Path | None = None) -> None:
        """初始化索引读写器。"""
        self._security = security
        self._index_path = index_path or default_index_path(security)

    @property
    def path(self) -> Path:
        """返回索引文件路径。"""
        return self._index_path

    def status(self) -> dict[str, Any]:
        """返回当前索引文件状态。"""
        if not self._index_path.exists():
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
            return {
                "enabled": True,
                "mode": "local_tfidf",
                "tool": "search_codebase",
                "index_required": False,
                "index_exists": True,
                "index_path": str(self._index_path),
                "error": str(exc),
            }
        return {
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

    def build(self, include: str = "**/*", max_files: int = 4000) -> dict[str, Any]:
        """扫描工程并重建本地索引。"""
        root = self._security.project_root
        files = _iter_text_files(root, self._security, include)
        chunks: list[dict[str, Any]] = []
        indexed_files = 0
        for candidate in files[:max_files]:
            rel = candidate.relative_to(root).as_posix()
            text = _read_text(candidate)
            lines = text.splitlines()
            if not lines:
                continue
            indexed_files += 1
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
            "mode": "local_tfidf",
            "built_at": time.time(),
            "project_root": str(root),
            "include": include,
            "files": indexed_files,
            "chunks": chunks,
        }
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return {
            "mode": "local_tfidf",
            "index_rebuilt": True,
            "index_path": str(self._index_path),
            "files": indexed_files,
            "chunks": len(chunks),
            "truncated_files": len(files) > max_files,
        }

    def search(self, query: str, include: str = "**/*", max_results: int = 16) -> dict[str, Any]:
        """基于持久化索引搜索相关片段。"""
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
        results = self._rank_chunks(query_counts, chunks, include, max_results)
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
        return {
            "query": query,
            "mode": "live_tfidf_scan",
            "scanned_files": len(files),
            "results": [result.to_dict() for result in results],
            "truncated": len(results) >= max_results,
            "note": "未找到持久化索引，已即时扫描；可运行 /index rebuild 提升后续检索速度。",
        }

    def _load(self) -> dict[str, Any]:
        """读取并校验索引文件。"""
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取 RAG 索引：{exc}") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("RAG 索引 schema_version 不匹配，请重建索引")
        return data

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
