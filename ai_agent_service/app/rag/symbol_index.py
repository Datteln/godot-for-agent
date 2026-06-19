"""GDScript/C# 符号索引（正则兜底实现）。"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app.rag.models import SearchResult

logger = logging.getLogger(__name__)

_PATTERNS = {
    ".gd": [
        ("function", re.compile(r"^\s*func\s+([A-Za-z_]\w*)")),
        ("class", re.compile(r"^\s*class(?:_name)?\s+([A-Za-z_]\w*)")),
        ("signal", re.compile(r"^\s*signal\s+([A-Za-z_]\w*)")),
        ("constant", re.compile(r"^\s*(?:const|enum)\s+([A-Za-z_]\w*)")),
        ("scene_node", re.compile(r"^\s*@onready\s+var\s+([A-Za-z_]\w*)\s*=\s*\$")),
    ],
    ".cs": [
        ("class", re.compile(r"\bclass\s+([A-Za-z_]\w*)")),
        ("function", re.compile(r"\b(?:public|private|protected|internal)\s+(?:static\s+)?[\w<>,?\[\]]+\s+([A-Za-z_]\w*)\s*\(")),
        ("constant", re.compile(r"\bconst\s+[\w<>,?\[\]]+\s+([A-Za-z_]\w*)")),
        ("signal", re.compile(r"\b(?:event|delegate)\s+[\w<>,?\[\]]+\s+([A-Za-z_]\w*)")),
    ],
}

_TREE_NODE_TYPES = {
    ".gd": {
        "function_definition": "function",
        "class_definition": "class",
        "class_name_statement": "class",
        "signal_statement": "signal",
        "const_statement": "constant",
        "enum_definition": "constant",
        "variable_statement": "scene_node",
    },
    ".cs": {
        "method_declaration": "function",
        "class_declaration": "class",
        "enum_declaration": "constant",
        "event_declaration": "signal",
        "field_declaration": "constant",
    },
}


def _tree_sitter_language(suffix: str) -> Any | None:
    """加载可选 grammar；任何版本/API 不兼容均触发正则降级。"""
    try:
        from tree_sitter import Language

        if suffix == ".gd":
            import tree_sitter_gdscript as grammar  # type: ignore[import-not-found]
        elif suffix == ".cs":
            import tree_sitter_c_sharp as grammar  # type: ignore[import-not-found]
        else:
            return None
        raw_language = grammar.language()
        try:
            return Language(raw_language)
        except TypeError:
            return raw_language
    except (ImportError, AttributeError, RuntimeError):
        return None


def _extract_tree_sitter(text: str, suffix: str) -> list[dict[str, Any]] | None:
    language = _tree_sitter_language(suffix)
    if language is None:
        return None
    try:
        from tree_sitter import Parser

        try:
            parser = Parser(language)
        except TypeError:
            parser = Parser()
            parser.language = language
        source = text.encode("utf-8")
        tree = parser.parse(source)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None

    node_types = _TREE_NODE_TYPES.get(suffix, {})
    patterns = _PATTERNS.get(suffix, [])
    extracted: list[dict[str, Any]] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kind = node_types.get(node.type)
        if kind:
            node_text = source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
            pattern = next((candidate for candidate_kind, candidate in patterns if candidate_kind == kind), None)
            match = pattern.search(node_text) if pattern is not None else None
            if match:
                extracted.append({"name": match.group(1), "kind": kind, "line": node.start_point[0] + 1})
        stack.extend(reversed(node.children))
    return extracted


def _extract_regex(lines: list[str], suffix: str) -> list[dict[str, Any]]:
    extracted: list[dict[str, Any]] = []
    for line_no, line in enumerate(lines, 1):
        for kind, pattern in _PATTERNS.get(suffix, []):
            match = pattern.search(line)
            if match:
                extracted.append({"name": match.group(1), "kind": kind, "line": line_no})
                break
    return extracted


class SymbolIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.symbols: list[dict[str, Any]] = []

    def build(self, root: Path, files: list[Path], changed_paths: set[str] | None = None) -> int:
        started = time.perf_counter()
        logger.info(
            "Symbol index build start path=%s files=%d incremental=%s changed_files=%d",
            self.path,
            len(files),
            changed_paths is not None,
            len(changed_paths or ()),
        )
        if self.path.exists():
            self.load()
        if changed_paths is None:
            self.symbols = []
        else:
            self.symbols = [s for s in self.symbols if s.get("file_path") not in changed_paths]
        for file in files:
            rel = file.relative_to(root).as_posix()
            if changed_paths is not None and rel not in changed_paths:
                continue
            patterns = _PATTERNS.get(file.suffix.lower())
            if not patterns:
                continue
            text = file.read_text(encoding="utf-8", errors="ignore")
            lines = text.splitlines()
            tree_symbols = _extract_tree_sitter(text, file.suffix.lower())
            extracted = tree_symbols or []
            known = {(item["name"], item["kind"], item["line"]) for item in extracted}
            for item in _extract_regex(lines, file.suffix.lower()):
                key = (item["name"], item["kind"], item["line"])
                if key not in known:
                    extracted.append(item)
            backend = "tree-sitter" if tree_symbols is not None else "regex"
            for item in extracted:
                line_no = int(item["line"])
                lo, hi = max(0, line_no - 2), min(len(lines), line_no + 2)
                self.symbols.append(
                    {
                        "name": item["name"],
                        "kind": item["kind"],
                        "file_path": rel,
                        "line": line_no,
                        "content": "\n".join(lines[lo:hi]),
                        "backend": backend,
                    }
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema_version": 1, "symbols": self.symbols}, ensure_ascii=False), encoding="utf-8")
        backend_counts = Counter(str(item.get("backend", "legacy")) for item in self.symbols)
        logger.info(
            "Symbol index build complete symbols=%d backends=%s elapsed_ms=%.3f",
            len(self.symbols),
            dict(backend_counts),
            (time.perf_counter() - started) * 1000,
        )
        return len(self.symbols)

    def load(self) -> None:
        if not self.path.exists():
            self.symbols = []
            logger.debug("Symbol index missing path=%s", self.path)
            return
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.symbols = value.get("symbols", []) if isinstance(value, dict) else []
            logger.debug("Symbol index loaded path=%s symbols=%d", self.path, len(self.symbols))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Symbol index load failed path=%s error=%s", self.path, exc)
            self.symbols = []

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        if not self.symbols:
            self.load()
        words = re.findall(r"[A-Za-z_]\w*", query.lower())
        ranked: list[tuple[float, dict[str, Any]]] = []
        for symbol in self.symbols:
            name = str(symbol.get("name", "")).lower()
            score = max((1.0 if word == name else 0.8 if name.startswith(word) or word.startswith(name) else 0.0 for word in words), default=0.0)
            if score:
                ranked.append((score, symbol))
        ranked.sort(key=lambda item: (-item[0], item[1]["file_path"], item[1]["line"]))
        results = [SearchResult(
            id=f"symbol:{s['file_path']}:{s['line']}:{s['name']}", content=s["content"], source="sym",
            score=score, file_path=s["file_path"], span=(s["line"], s["line"]),
            graph_meta={"symbol": s["name"], "symbol_type": s["kind"]},
        ) for score, s in ranked[:limit]]
        logger.debug(
            "Symbol search complete query_length=%d candidates=%d results=%d",
            len(query),
            len(ranked),
            len(results),
        )
        return results

    def summary(self, limit: int = 100) -> str:
        if not self.symbols:
            self.load()
        return "\n".join(f"- {s['kind']} {s['name']} ({s['file_path']}:{s['line']})" for s in self.symbols[:limit])
