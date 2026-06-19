"""Godot .tscn 场景树索引。"""

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

_EXT = re.compile(r'^\[ext_resource\s+type="(?P<type>[^"]+)"\s+path="res://(?P<path>[^"]+)"\s+id="(?P<id>[^"]+)"')
_NODE = re.compile(r'^\[node\s+name="(?P<name>[^"]+)"(?:\s+type="(?P<type>[^"]+)")?(?:\s+parent="(?P<parent>[^"]+)")?(?:\s+instance=ExtResource\("(?P<instance>[^"]+)"\))?')
_SCRIPT = re.compile(r'^script\s*=\s*ExtResource\("(?P<id>[^"]+)"\)')


class SceneGraphIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.scenes: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.version = "none"

    def build(self, root: Path, files: list[Path] | None = None, incremental: bool = True) -> dict[str, int]:
        started = time.perf_counter()
        logger.info(
            "Scene graph build start path=%s incremental=%s supplied_files=%s",
            self.path,
            incremental,
            len(files) if files is not None else "auto",
        )
        if incremental and self.path.exists():
            self.load()
        scene_files = files if files is not None else sorted(root.rglob("*.tscn"))
        current = {p.relative_to(root).as_posix(): p for p in scene_files if p.suffix.lower() == ".tscn"}
        for stale in set(self.scenes) - set(current):
            self.scenes.pop(stale, None)
        for rel, file in current.items():
            stat = file.stat()
            old = self.scenes.get(rel, {})
            if incremental and old.get("mtime_ns") == stat.st_mtime_ns and old.get("size") == stat.st_size:
                continue
            self.scenes[rel] = self._parse(file, rel, stat.st_mtime_ns, stat.st_size)
        self._rebuild_graph()
        self._save()
        result = {"scenes": len(self.scenes), "nodes": len(self.nodes), "edges": len(self.edges)}
        logger.info(
            "Scene graph build complete scenes=%d nodes=%d edges=%d version=%s elapsed_ms=%.3f",
            result["scenes"],
            result["nodes"],
            result["edges"],
            self.version,
            (time.perf_counter() - started) * 1000,
        )
        return result

    def _parse(self, file: Path, rel: str, mtime_ns: int, size: int) -> dict[str, Any]:
        resources: dict[str, dict[str, str]] = {}
        nodes: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        root_path = ""
        for line in file.read_text(encoding="utf-8", errors="ignore").splitlines():
            match = _EXT.match(line)
            if match:
                resources[match["id"]] = {"path": match["path"], "type": match["type"]}
                continue
            match = _NODE.match(line)
            if match:
                parent = match["parent"] or ""
                name = match["name"]
                if not root_path and not parent:
                    root_path = name
                    node_path = name
                elif parent in {"", "."}:
                    node_path = f"{root_path}/{name}" if root_path else name
                else:
                    relative_parent = parent if parent.startswith(root_path) else f"{root_path}/{parent}"
                    node_path = f"{relative_parent}/{name}"
                current = {"path": node_path, "name": name, "type": match["type"] or "Node", "script": "", "instance": "", "children": []}
                if match["instance"] in resources:
                    current["instance"] = resources[match["instance"]]["path"]
                nodes.append(current)
                continue
            match = _SCRIPT.match(line)
            if current is not None and match and match["id"] in resources:
                current["script"] = resources[match["id"]]["path"]
        by_path = {node["path"]: node for node in nodes}
        for node in nodes:
            parent = node["path"].rsplit("/", 1)[0] if "/" in node["path"] else ""
            if parent in by_path:
                by_path[parent]["children"].append(node["path"])
        return {"scene": rel, "mtime_ns": mtime_ns, "size": size, "resources": resources, "nodes": nodes}

    def _rebuild_graph(self) -> None:
        self.nodes, self.edges = {}, []
        for scene, data in self.scenes.items():
            scene_id = f"scene:{scene}"
            self.nodes[scene_id] = GraphNode(scene_id, "scene", scene, scene)
            for item in data.get("nodes", []):
                node_id = f"node:{scene}:{item['path']}"
                content = f"Node {item['path']} type={item['type']} script={item['script'] or '-'}"
                self.nodes[node_id] = GraphNode(node_id, "scene_node", item["name"], scene, content, dict(item))
                parent_path = item["path"].rsplit("/", 1)[0] if "/" in item["path"] else ""
                parent_id = f"node:{scene}:{parent_path}" if parent_path else scene_id
                self.edges.append(GraphEdge(parent_id, node_id, "child"))
                if item.get("script"):
                    script_id = f"file:{item['script']}"
                    self.nodes.setdefault(script_id, GraphNode(script_id, "script", Path(item["script"]).name, item["script"]))
                    self.edges.append(GraphEdge(node_id, script_id, "script"))
                if item.get("instance"):
                    self.edges.append(GraphEdge(node_id, f"scene:{item['instance']}", "instance"))
        material = json.dumps(self.to_dict(include_version=False), sort_keys=True, ensure_ascii=False)
        self.version = hashlib.sha256(material.encode()).hexdigest()[:16] if self.scenes else "none"

    def find_node(self, name: str) -> list[SearchResult]:
        needle = name.lower().strip().split("/")[-1]
        return [self._result(node) for node in self.nodes.values() if node.kind == "scene_node" and (needle == node.label.lower() or needle in node.metadata.get("path", "").lower())]

    def find_script_nodes(self, script_file: str) -> list[SearchResult]:
        needle = script_file.lower()
        return [self._result(n) for n in self.nodes.values() if n.kind == "scene_node" and needle in str(n.metadata.get("script", "")).lower()]

    def trace_path(self, node_path: str) -> list[SearchResult]:
        needle = node_path.removeprefix("$").lower()
        return [self._result(n) for n in self.nodes.values() if n.kind == "scene_node" and str(n.metadata.get("path", "")).lower().endswith(needle)]

    def get_subtree(self, scene_path: str) -> dict[str, Any] | None:
        return self.scenes.get(scene_path)

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        words = [w.lower() for w in re.findall(r"[A-Za-z_]\w*|[\u4e00-\u9fff]+", query)]
        ranked: list[tuple[float, GraphNode]] = []
        for node in self.nodes.values():
            if node.kind != "scene_node":
                continue
            hay = f"{node.label} {node.content} {node.metadata.get('path', '')}".lower()
            hits = sum(word in hay for word in words)
            if hits:
                ranked.append((min(1.0, hits / max(len(words), 1) + 0.3), node))
        ranked.sort(key=lambda item: (-item[0], item[1].id))
        results = [self._result(node, score) for score, node in ranked[:limit]]
        logger.debug(
            "Scene graph search complete query_length=%d nodes=%d results=%d",
            len(query),
            len(self.nodes),
            len(results),
        )
        return results

    def _result(self, node: GraphNode, score: float = 1.0) -> SearchResult:
        return SearchResult(node.id, node.content, "scene_graph", score, node.file_path, graph_meta={"node_path": node.metadata.get("path", "")})

    def summary(self, limit: int = 100) -> str:
        lines = ["场景图摘要："]
        for scene in sorted(self.scenes):
            roots = [n["path"] for n in self.scenes[scene].get("nodes", []) if "/" not in n["path"]]
            lines.append(f"- {scene}: {', '.join(roots) or '(empty)'}")
            if len(lines) >= limit:
                break
        return "\n".join(lines) if self.scenes else ""

    def to_dict(self, include_version: bool = True) -> dict[str, Any]:
        value = {"schema_version": 1, "scenes": self.scenes}
        if include_version:
            value["version"] = self.version
        return value

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.to_dict(), ensure_ascii=False), encoding="utf-8")

    def load(self) -> None:
        if not self.path.exists():
            self.scenes = {}
            self._rebuild_graph()
            logger.debug("Scene graph index missing path=%s", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.scenes = data.get("scenes", {})
            self._rebuild_graph()
            logger.debug(
                "Scene graph loaded path=%s scenes=%d nodes=%d version=%s",
                self.path,
                len(self.scenes),
                len(self.nodes),
                self.version,
            )
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Scene graph load failed path=%s error=%s", self.path, exc)
            self.scenes = {}
            self._rebuild_graph()
