"""GDScript 与 .tscn 信号关系索引。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.rag.models import GraphEdge, GraphNode, SearchResult

logger = logging.getLogger(__name__)

_SIGNAL = re.compile(r"^\s*signal\s+([A-Za-z_]\w*)")
_CONNECT = re.compile(
    r"([A-Za-z_$][\w/$]*)\.connect\(\s*(?:Callable\([^,]+,\s*)?"
    r"(?:[\"']([^\"']+)[\"']|(?:[A-Za-z_]\w*\.)?([A-Za-z_]\w*))"
)
_TSCN_CONNECTION = re.compile(r'^\[connection\s+signal="([^"]+)"\s+from="([^"]+)"\s+to="([^"]+)"\s+method="([^"]+)"')


class SignalGraphIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.signals: list[dict[str, Any]] = []
        self.file_signals: dict[str, list[dict[str, Any]]] = {}
        self.file_states: dict[str, dict[str, int]] = {}
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.version = "none"

    def build(self, root: Path, files: list[Path] | None = None, incremental: bool = True) -> int:
        started = time.perf_counter()
        logger.info(
            "Signal graph build start path=%s incremental=%s supplied_files=%s",
            self.path,
            incremental,
            len(files) if files is not None else "auto",
        )
        if incremental and self.path.exists():
            self.load()
        candidates = files or sorted([*root.rglob("*.gd"), *root.rglob("*.tscn")])
        current = {
            p.relative_to(root).as_posix(): p
            for p in candidates if p.suffix.lower() in {".gd", ".tscn"}
        }
        for stale in set(self.file_signals) - set(current):
            self.file_signals.pop(stale, None)
            self.file_states.pop(stale, None)
        for rel, file in current.items():
            stat = file.stat()
            state = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size}
            if incremental and self.file_states.get(rel) == state and rel in self.file_signals:
                continue
            self.file_signals[rel] = self._parse_file(file, rel)
            self.file_states[rel] = state
        self.signals = [signal for rel in sorted(self.file_signals) for signal in self.file_signals[rel]]
        self._rebuild_graph()
        self._save()
        logger.info(
            "Signal graph build complete signals=%d nodes=%d edges=%d version=%s elapsed_ms=%.3f",
            len(self.signals),
            len(self.nodes),
            len(self.edges),
            self.version,
            (time.perf_counter() - started) * 1000,
        )
        return len(self.signals)

    def _parse_file(self, file: Path, rel: str) -> list[dict[str, Any]]:
        signals: list[dict[str, Any]] = []
        lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
        if file.suffix.lower() == ".gd":
            for line_no, line in enumerate(lines, 1):
                match = _SIGNAL.match(line)
                if match:
                    signals.append({"name": match.group(1), "emitter": rel, "emitter_script": rel, "line": line_no, "connections": []})
                match = _CONNECT.search(line)
                if match:
                    name = match.group(1).split("/")[-1].lstrip("$")
                    target = next((s for s in signals if s["name"] == name and s["emitter_script"] == rel), None)
                    if target is None:
                        target = {"name": name, "emitter": rel, "emitter_script": rel, "line": line_no, "connections": []}
                        signals.append(target)
                    method = match.group(2) or match.group(3)
                    target["connections"].append({"receiver": rel, "receiver_script": rel, "method": method})
        elif file.suffix.lower() == ".tscn":
            for line in lines:
                match = _TSCN_CONNECTION.match(line)
                if match:
                    signals.append({"name": match.group(1), "emitter": match.group(2), "emitter_script": "", "line": 0, "scene": rel,
                                    "connections": [{"receiver": match.group(3), "receiver_script": "", "method": match.group(4)}]})
        return signals

    def _rebuild_graph(self) -> None:
        self.nodes, self.edges = {}, []
        for index, signal in enumerate(self.signals):
            sid = f"signal:{signal.get('scene') or signal['emitter_script']}:{signal['name']}:{index}"
            content = f"Signal {signal['name']} emitted by {signal['emitter']}"
            self.nodes[sid] = GraphNode(sid, "signal", signal["name"], signal.get("scene") or signal["emitter_script"], content, signal)
            for connection in signal.get("connections", []):
                receiver_id = f"receiver:{connection['receiver']}:{connection['method']}"
                self.nodes.setdefault(receiver_id, GraphNode(receiver_id, "receiver", connection["method"], connection.get("receiver_script", ""), f"Receiver {connection['receiver']}.{connection['method']}", connection))
                self.edges.append(GraphEdge(sid, receiver_id, "signal"))
        material = json.dumps(self.signals, sort_keys=True, ensure_ascii=False)
        self.version = hashlib.sha256(material.encode()).hexdigest()[:16] if self.signals else "none"

    def find_signal(self, name: str) -> list[SearchResult]:
        needle = name.lower()
        return [self._result(n) for n in self.nodes.values() if n.kind == "signal" and needle in n.label.lower()]

    def trace_signal_chain(self, signal_name: str) -> list[SearchResult]:
        seeds = {result.id for result in self.find_signal(signal_name)}
        ids = seeds | {e.target for e in self.edges if e.source in seeds}
        return [self._result(self.nodes[node_id]) for node_id in ids if node_id in self.nodes]

    def find_emitters(self, method_name: str) -> list[SearchResult]:
        receiver_ids = {n.id for n in self.nodes.values() if n.kind == "receiver" and method_name.lower() in n.label.lower()}
        source_ids = {edge.source for edge in self.edges if edge.target in receiver_ids}
        return [self._result(self.nodes[node_id]) for node_id in source_ids]

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        words = re.findall(r"[A-Za-z_]\w*", query.lower())
        ranked = [(sum(w in f"{n.label} {n.content}".lower() for w in words) / max(len(words), 1), n) for n in self.nodes.values()]
        ranked = [(s, n) for s, n in ranked if s > 0]
        ranked.sort(key=lambda item: (-item[0], item[1].id))
        results = [self._result(n, min(1.0, s + 0.2)) for s, n in ranked[:limit]]
        logger.debug(
            "Signal graph search complete query_length=%d nodes=%d results=%d",
            len(query),
            len(self.nodes),
            len(results),
        )
        return results

    def _result(self, node: GraphNode, score: float = 1.0) -> SearchResult:
        return SearchResult(node.id, node.content, "signal_graph", score, node.file_path, graph_meta={"edge_type": "signal"})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "version": self.version,
                    "signals": self.signals,
                    "file_signals": self.file_signals,
                    "file_states": self.file_states,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def load(self) -> None:
        if not self.path.exists():
            self.signals = []
            self.file_signals = {}
            self.file_states = {}
            self._rebuild_graph()
            logger.debug("Signal graph index missing path=%s", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.file_signals = data.get("file_signals", {}) if isinstance(data, dict) else {}
            self.file_states = data.get("file_states", {}) if isinstance(data, dict) else {}
            if self.file_signals:
                self.signals = [signal for rel in sorted(self.file_signals) for signal in self.file_signals[rel]]
            else:
                # Legacy schema_version=1 files predate per-file tracking; treat as full rebuild on next build().
                self.signals = data.get("signals", []) if isinstance(data, dict) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Signal graph load failed path=%s error=%s", self.path, exc)
            self.signals = []
            self.file_signals = {}
            self.file_states = {}
        self._rebuild_graph()
        logger.debug(
            "Signal graph loaded path=%s signals=%d nodes=%d version=%s",
            self.path,
            len(self.signals),
            len(self.nodes),
            self.version,
        )
