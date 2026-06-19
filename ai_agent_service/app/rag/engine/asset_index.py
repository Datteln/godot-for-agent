"""确定性资产结构索引与可选语义描述层。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from app.rag.asset_llm_client import AssetLLMClient
from app.rag.models import GraphEdge, GraphNode, SearchResult

logger = logging.getLogger(__name__)

_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp"}
_AUDIO = {".wav", ".mp3", ".ogg", ".flac"}
_RESOURCE = {".tscn", ".tres", ".res", ".anim"}
_TEXT = {".gd", ".cs", ".json", ".cfg", ".md", ".txt", ".ini", ".gdshader", ".import"}
_REF = re.compile(r'\[ext_resource[^\]]*path="res://([^"]+)"')


def _token_counts(text: str) -> Counter[str]:
    return Counter(token.lower() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}|[\u4e00-\u9fff]{2,}", text))


def classify_asset(path: Path | str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in _IMAGE:
        return "image"
    if suffix in _AUDIO:
        return "audio"
    if suffix in _RESOURCE:
        return "scene" if suffix == ".tscn" else "resource"
    return "binary"


class AssetIndex:
    def __init__(self, path: Path, llm_client: AssetLLMClient | None = None, enabled: bool = True) -> None:
        self.path = path
        self.llm_client = llm_client or AssetLLMClient()
        self.enabled = enabled
        self.assets: dict[str, dict[str, Any]] = {}
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.version = "none"

    def build(self, root: Path, incremental: bool = True, max_files: int = 10000) -> int:
        if not self.enabled:
            logger.debug("Asset index build skipped reason=disabled path=%s", self.path)
            return 0
        started = time.perf_counter()
        logger.info(
            "Asset index build start path=%s incremental=%s max_files=%d semantic_enabled=%s",
            self.path,
            incremental,
            max_files,
            self.llm_client.available,
        )
        if incremental and self.path.exists():
            self.load()
        files = [p for p in sorted(root.rglob("*")) if p.is_file() and ".ai_agent_service" not in p.parts]
        asset_files = {p.relative_to(root).as_posix(): p for p in files if p.suffix.lower() not in _TEXT}
        usage: dict[str, list[str]] = {}
        for scene in (p for p in files if p.suffix.lower() == ".tscn"):
            rel_scene = scene.relative_to(root).as_posix()
            text = scene.read_text(encoding="utf-8", errors="ignore")
            for ref in _REF.findall(text):
                usage.setdefault(ref, []).append(rel_scene)
        self.assets = {key: value for key, value in self.assets.items() if key in asset_files}
        for rel, file in list(asset_files.items())[:max_files]:
            stat = file.stat()
            old = self.assets.get(rel)
            if old and incremental and old.get("mtime_ns") == stat.st_mtime_ns and old.get("size_bytes") == stat.st_size:
                old["used_by"] = sorted(usage.get(rel, []))
                continue
            type_hint = classify_asset(file)
            description = self.llm_client.describe(file, type_hint)
            self.assets[rel] = {
                "asset": rel, "extension": file.suffix.lower(), "type_hint": type_hint,
                "used_by": sorted(usage.get(rel, [])), "size_bytes": stat.st_size,
                "mtime_ns": stat.st_mtime_ns, "description": description,
            }
        self._rebuild_graph()
        self._save()
        described = sum(bool(asset.get("description")) for asset in self.assets.values())
        logger.info(
            "Asset index build complete assets=%d described=%d edges=%d version=%s "
            "elapsed_ms=%.3f truncated=%s",
            len(self.assets),
            described,
            len(self.edges),
            self.version,
            (time.perf_counter() - started) * 1000,
            len(asset_files) > max_files,
        )
        return len(self.assets)

    def _rebuild_graph(self) -> None:
        self.nodes, self.edges = {}, []
        structural: list[str] = []
        for rel, asset in sorted(self.assets.items()):
            node_id = f"asset:{rel}"
            content = f"{rel} {asset['type_hint']} {asset.get('description', '')}".strip()
            self.nodes[node_id] = GraphNode(node_id, "asset", Path(rel).name, rel, content, asset)
            structural.append(f"{rel}:{asset['size_bytes']}:{asset['mtime_ns']}:{','.join(asset.get('used_by', []))}")
            for scene in asset.get("used_by", []):
                self.edges.append(GraphEdge(f"scene:{scene}", node_id, "asset_ref"))
        self.version = hashlib.sha256("\n".join(structural).encode()).hexdigest()[:16] if structural else "none"

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        query_tokens = _token_counts(query)
        ranked: list[tuple[float, dict[str, Any]]] = []
        for asset in self.assets.values():
            hay = f"{asset['asset']} {asset['type_hint']} {' '.join(asset.get('used_by', []))} {asset.get('description', '')}"
            tokens = _token_counts(hay)
            hits = sum(min(count, tokens.get(token, 0)) for token, count in query_tokens.items())
            if hits:
                ranked.append((min(1.0, hits / max(sum(query_tokens.values()), 1)), asset))
        ranked.sort(key=lambda item: (-item[0], item[1]["asset"]))
        results = [SearchResult(
            id=f"asset:{asset['asset']}", content=f"{asset['asset']} ({asset['type_hint']}) used by: {', '.join(asset.get('used_by', [])) or '-'}\n{asset.get('description', '')}".strip(),
            source="asset", score=score, file_path=asset["asset"], graph_meta={"edge_type": "asset_ref", "used_by": asset.get("used_by", [])},
        ) for score, asset in ranked[:limit]]
        logger.debug(
            "Asset search complete query_length=%d assets=%d results=%d",
            len(query),
            len(self.assets),
            len(results),
        )
        return results

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema_version": 1, "version": self.version, "assets": self.assets}, ensure_ascii=False), encoding="utf-8")

    def load(self) -> None:
        if not self.path.exists():
            self.assets = {}
            self._rebuild_graph()
            logger.debug("Asset index missing path=%s", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.assets = data.get("assets", {})
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Asset index load failed path=%s error=%s", self.path, exc)
            self.assets = {}
        self._rebuild_graph()
        logger.debug(
            "Asset index loaded path=%s assets=%d version=%s",
            self.path,
            len(self.assets),
            self.version,
        )
