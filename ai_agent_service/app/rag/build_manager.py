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

_WATCH_EXCLUDED_DIRS = {".ai_agent_service", ".git", ".godot", "__pycache__", "logs"}
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
        # 当前在后台线程里运行的文件扫描任务；超时只放弃 await，不丢弃任务本身，
        # 这样下一轮 poll 不会再提交一个新的扫描线程（见 `_scan_with_timeout`）。
        self._scan_task: asyncio.Task[dict[str, tuple[int, int]]] | None = None

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

    async def watch(
        self,
        *,
        poll_interval_s: float = 1.0,
        debounce_s: float = 0.75,
        scan_timeout_s: float = 10.0,
    ) -> None:
        """Monitor indexable project files and coalesce changes into incremental builds."""
        poll_interval_s = max(0.1, poll_interval_s)
        debounce_s = max(0.0, debounce_s)
        scan_timeout_s = max(1.0, scan_timeout_s)
        baseline = await self._scan_with_timeout(scan_timeout_s)
        if baseline is None:
            baseline = {}
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
                current = await self._scan_with_timeout(scan_timeout_s)
                if current is None:
                    # 扫描超时：跳过本轮，绝不能让事件循环被一次卡住的 os.walk/stat
                    # 永久阻塞——下一轮 poll 会重试。
                    continue
                if current == baseline:
                    continue

                if debounce_s:
                    await asyncio.sleep(debounce_s)
                    rescanned = await self._scan_with_timeout(scan_timeout_s)
                    if rescanned is not None:
                        current = rescanned

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

    async def _scan_with_timeout(self, timeout_s: float) -> dict[str, tuple[int, int]] | None:
        """对 `_scan_file_states` 加超时保护，且保证同时只有一个后台扫描。

        `asyncio.to_thread` 本身不会阻塞事件循环，但若底层 `os.walk`/`stat`
        因杀毒软件扫描、网络盘等原因长时间不返回，`await` 仍会一直挂起，导致
        watcher 协程（以及依赖它调度的其它任务）形同卡死。这里用
        `asyncio.wait_for` 让协程按时放弃等待，把控制权交还事件循环。

        关键点：取消 `wait_for` 的等待并不会终止已经在运行的后台线程。旧实现
        每轮 poll 都新建一个 `to_thread`，于是一个卡住的扫描会让后续每轮都再
        堆一个线程，最终塞满线程池、把所有 `to_thread` 工作拖垮。这里改为持有
        一个长期存活的扫描任务并用 `asyncio.shield` 等待它：超时只是本轮放弃
        等待，任务继续在后台跑；下一轮 poll 复用同一个任务，绝不重复提交。任务
        完成后清空引用，下次才会发起新的扫描。
        """
        if self._scan_task is None or self._scan_task.done():
            self._scan_task = asyncio.create_task(
                asyncio.to_thread(self._scan_file_states),
                name="rag-file-scan",
            )

        task = self._scan_task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.warning(
                "RAG file watcher scan still running after %.1fs; reusing it, no duplicate scan scheduled",
                timeout_s,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            # 扫描任务自身抛错：本轮当作跳过，下一轮 poll 会重启一个新扫描，
            # 绝不能让一次扫描异常掀翻整个 watcher 协程。
            logger.exception("RAG file watcher scan failed; skipping this cycle")
            return None
        finally:
            # 任务已结束则清空引用，下一轮才会发起新的扫描；未结束（超时）时保留，
            # 让后续 poll 复用同一个后台任务。
            if self._scan_task is not None and self._scan_task.done():
                self._scan_task = None

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
