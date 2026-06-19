"""本地持久化向量索引；优先 FAISS，缺失时使用确定性余弦搜索。"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from app.rag.embedding_client import EmbeddingClient
from app.rag.models import SearchResult

logger = logging.getLogger(__name__)


class EmbeddingIndex:
    def __init__(self, path: Path, client: EmbeddingClient | None = None) -> None:
        self.path = path
        self.faiss_path = path.with_suffix(".faiss")
        self.client = client or EmbeddingClient()
        self.records: list[dict[str, Any]] = []

    def build(self, chunks: list[dict[str, Any]], changed_paths: set[str] | None = None) -> int:
        started = time.perf_counter()
        logger.info(
            "Embedding index build start path=%s chunks=%d incremental=%s changed_files=%d",
            self.path,
            len(chunks),
            changed_paths is not None,
            len(changed_paths or ()),
        )
        if self.path.exists():
            self.load()
        if changed_paths is None:
            self.records = []
        else:
            self.records = [r for r in self.records if r.get("file_path") not in changed_paths]
        selected = chunks if changed_paths is None else [c for c in chunks if c.get("path") in changed_paths]
        vectors = self.client.embed([str(c.get("snippet", "")) for c in selected])
        if len(vectors) != len(selected):
            log = logger.warning if self.client.available else logger.debug
            log(
                "Embedding index vector generation unavailable requested=%d received=%d "
                "provider_available=%s; preserving keyword retrieval fallback",
                len(selected), len(vectors), self.client.available,
            )
            self.save()
            return len(self.records)
        for chunk, vector in zip(selected, vectors, strict=True):
            self.records.append(
                {
                    "id": f"chunk:{chunk['path']}:{chunk['start_line']}",
                    "file_path": chunk["path"],
                    "span": [chunk["start_line"], chunk["end_line"]],
                    "content": chunk["snippet"],
                    "vector": vector,
                }
            )
        self.save()
        logger.info(
            "Embedding index build complete records=%d updated=%d elapsed_ms=%.3f faiss=%s",
            len(self.records),
            len(selected),
            (time.perf_counter() - started) * 1000,
            self.faiss_path.exists(),
        )
        return len(self.records)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"schema_version": 1, "records": self.records}, ensure_ascii=False), encoding="utf-8")
        self._save_faiss()
        logger.debug("Embedding index persisted path=%s records=%d", self.path, len(self.records))

    def _save_faiss(self) -> None:
        if not self.records:
            self.faiss_path.unlink(missing_ok=True)
            return
        try:
            import faiss  # type: ignore[import-not-found]
            import numpy as np

            matrix = np.asarray([record["vector"] for record in self.records], dtype="float32")
            faiss.normalize_L2(matrix)
            index = faiss.IndexFlatIP(matrix.shape[1])
            index.add(matrix)
            faiss.write_index(index, str(self.faiss_path))
        except (ImportError, ValueError) as exc:
            # Portable cosine fallback remains available when optional FAISS/numpy are absent.
            logger.debug("FAISS persistence unavailable; cosine fallback active error=%s", exc)
            return

    def load(self) -> None:
        if not self.path.exists():
            self.records = []
            logger.debug("Embedding index missing path=%s", self.path)
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.records = data.get("records", []) if isinstance(data, dict) else []
            logger.debug("Embedding index loaded path=%s records=%d", self.path, len(self.records))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Embedding index load failed path=%s error=%s", self.path, exc)
            self.records = []

    def search(self, query: str, limit: int = 10) -> list[SearchResult]:
        started = time.perf_counter()
        if not self.records:
            self.load()
        vectors = self.client.embed([query])
        if not vectors:
            logger.debug("Embedding search skipped query_length=%d reason=no_query_vector", len(query))
            return []
        query_vector = vectors[0]
        faiss_scores = self._search_faiss(query_vector, limit)
        if faiss_scores is not None:
            results = [
                SearchResult(
                    id=str(self.records[index]["id"]), content=str(self.records[index]["content"]),
                    source="vec", score=(score + 1.0) / 2.0,
                    file_path=str(self.records[index]["file_path"]),
                    span=tuple(self.records[index].get("span", [0, 0])),
                )
                for score, index in faiss_scores
            ]
            logger.debug(
                "Embedding search complete backend=faiss query_length=%d records=%d "
                "results=%d elapsed_ms=%.3f",
                len(query),
                len(self.records),
                len(results),
                (time.perf_counter() - started) * 1000,
            )
            return results
        scored: list[tuple[float, dict[str, Any]]] = []
        for record in self.records:
            vector = record.get("vector", [])
            if len(vector) != len(query_vector) or not vector:
                continue
            dot = sum(a * b for a, b in zip(query_vector, vector, strict=True))
            norm = math.sqrt(sum(a * a for a in query_vector) * sum(b * b for b in vector)) or 1.0
            scored.append(((dot / norm + 1.0) / 2.0, record))
        scored.sort(key=lambda item: (-item[0], item[1].get("id", "")))
        results = [
            SearchResult(
                id=str(r["id"]), content=str(r["content"]), source="vec", score=s,
                file_path=str(r["file_path"]), span=tuple(r.get("span", [0, 0])),
            )
            for s, r in scored[:limit]
        ]
        logger.debug(
            "Embedding search complete backend=cosine query_length=%d records=%d "
            "results=%d elapsed_ms=%.3f",
            len(query),
            len(self.records),
            len(results),
            (time.perf_counter() - started) * 1000,
        )
        return results

    def _search_faiss(self, vector: list[float], limit: int) -> list[tuple[float, int]] | None:
        if not self.faiss_path.exists():
            return None
        try:
            import faiss  # type: ignore[import-not-found]
            import numpy as np

            query = np.asarray([vector], dtype="float32")
            faiss.normalize_L2(query)
            index = faiss.read_index(str(self.faiss_path))
            distances, indexes = index.search(query, min(limit, len(self.records)))
            return [
                (float(score), int(record_index))
                for score, record_index in zip(distances[0], indexes[0], strict=True)
                if record_index >= 0
            ]
        except (ImportError, ValueError, OSError) as exc:
            logger.debug("FAISS search unavailable; using cosine fallback error=%s", exc)
            return None
