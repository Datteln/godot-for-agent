"""RAG 索引自动/手动构建协调器。"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from app.config import AppSettings
from app.rag.factory import create_codebase_index
from app.rag.index import TEXT_SUFFIXES
from app.security.settings import SecuritySettings

logger = logging.getLogger(__name__)

_WATCH_EXCLUDED_DIRS = {".ai_agent_service", ".git", ".godot", "__pycache__"}
_WATCHED_ASSET_SUFFIXES = {
    ".anim",
    ".bmp",
    ".flac",
    ".jpeg",
    ".jpg",
    ".mp3",
    ".ogg",
    ".png",
    ".res",
    ".svg",
    ".wav",
    ".webp",
}


class RagIndexBuildManager:
    """串行化索引构建，避免启动构建与手动命令同时写同一组文件。"""

    def __init__(self, settings: AppSettings, security: SecuritySettings) -> None:
        self._settings = settings
        self._security = security
        self._lock = asyncio.Lock()
        self._last_result: dict[str, Any] | None = None

    @property
    def building(self) -> bool:
        return self._lock.locked()

    @property
    def last_result(self) -> dict[str, Any] | None:
        return self._last_result

    async def build(
        self,
        *,
        include: str = "**/*",
        max_files: int = 4000,
        incremental: bool = True,
        reason: str = "manual",
    ) -> dict[str, Any]:
        logger.info(
            "RAG coordinated build requested reason=%s incremental=%s include=%s max_files=%d waiting=%s",
            reason,
            incremental,
            include,
            max_files,
            self._lock.locked(),
        )
        async with self._lock:
            index = create_codebase_index(self._settings, self._security)
            result = await asyncio.to_thread(index.build, include, max_files, incremental)
            result["trigger"] = reason
            self._last_result = result
            logger.info(
                "RAG coordinated build complete reason=%s files=%s chunks=%s changed_files=%s",
                reason,
                result.get("files"),
                result.get("chunks"),
                result.get("changed_files"),
            )
            return result

    async def watch(self, *, poll_interval_s: float = 1.0, debounce_s: float = 0.75) -> None:
        """Monitor indexable project files and coalesce changes into incremental builds."""
        poll_interval_s = max(0.1, poll_interval_s)
        debounce_s = max(0.0, debounce_s)
        baseline = await asyncio.to_thread(self._scan_file_states)
        logger.info(
            "RAG file watcher started root=%s files=%d poll_interval_s=%.2f debounce_s=%.2f",
            self._security.project_root,
            len(baseline),
            poll_interval_s,
            debounce_s,
        )
        try:
            while True:
                await asyncio.sleep(poll_interval_s)
                current = await asyncio.to_thread(self._scan_file_states)
                if current == baseline:
                    continue

                if debounce_s:
                    await asyncio.sleep(debounce_s)
                    current = await asyncio.to_thread(self._scan_file_states)

                changed = {
                    path
                    for path in baseline.keys() | current.keys()
                    if baseline.get(path) != current.get(path)
                }
                deleted = set(baseline) - set(current)
                logger.info(
                    "RAG project changes detected changed=%d deleted=%d sample=%s",
                    len(changed),
                    len(deleted),
                    sorted(changed)[:8],
                )

                try:
                    await self.build(incremental=True, reason="project_files_changed")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Automatic incremental RAG rebuild failed; watcher will retry")
                else:
                    # Preserve the pre-build snapshot so changes made during build are
                    # detected by the next polling pass instead of being swallowed.
                    baseline = current
        except asyncio.CancelledError:
            logger.info("RAG file watcher stopped")
            raise

    def _scan_file_states(self) -> dict[str, tuple[int, int]]:
        root = self._security.project_root
        suffixes = set(TEXT_SUFFIXES)
        if self._settings.asset_understanding_enabled:
            suffixes.update(_WATCHED_ASSET_SUFFIXES)

        states: dict[str, tuple[int, int]] = {}
        for directory, dir_names, file_names in os.walk(root):
            dir_names[:] = [name for name in dir_names if name not in _WATCH_EXCLUDED_DIRS]
            directory_path = Path(directory)
            for file_name in file_names:
                candidate = directory_path / file_name
                if candidate.suffix.lower() not in suffixes:
                    continue
                try:
                    stat = candidate.stat()
                    relative = candidate.relative_to(root)
                except (OSError, ValueError):
                    continue
                states[relative.as_posix()] = (stat.st_mtime_ns, stat.st_size)
        return states
