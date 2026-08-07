"""Turn progress registry: owns the non-persisted active-turn state.

Replaces the ``_turn_progress: dict[str, _TurnProgress]`` field in
``AgentApplication`` with a cohesive service that use cases inject.
Holds only per-request heartbeat and phase state — no Session or
domain state.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnProgress:
    """Non-persisted state for one active ``/chat`` request."""

    owner_id: int
    request_id: str | None
    turn_id: str | None
    phase: str
    heartbeat_seq: int = 0


@dataclass(slots=True)
class TurnProgressRegistry:
    """Thread-safe registry of active turn progress entries.

    Use cases register progress when a turn starts and remove it when
    the turn completes.  The ``/doctor`` and heartbeat endpoints read
    progress from this registry.
    """

    _entries: dict[str, TurnProgress] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def register(
        self,
        session_id: str,
        owner_id: int,
        request_id: str | None,
        phase: str,
    ) -> TurnProgress:
        """Register a new turn progress entry for a session."""
        with self._lock:
            entry = TurnProgress(
                owner_id=owner_id,
                request_id=request_id,
                turn_id=None,
                phase=phase,
            )
            self._entries[session_id] = entry
            return entry

    def get(self, session_id: str) -> TurnProgress | None:
        """Get the active turn progress for a session."""
        with self._lock:
            return self._entries.get(session_id)

    def update(
        self,
        session_id: str,
        *,
        turn_id: str | None = None,
        phase: str | None = None,
        heartbeat_seq: int | None = None,
    ) -> None:
        """Update the turn progress for a session."""
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return
            if turn_id is not None:
                entry.turn_id = turn_id
            if phase is not None:
                entry.phase = phase
            if heartbeat_seq is not None:
                entry.heartbeat_seq = heartbeat_seq

    def upsert_owned(
        self,
        session_id: str,
        *,
        owner_id: int,
        request_id: str | None,
        turn_id: str | None,
        phase: str,
    ) -> None:
        """更新指定 owner 的进度；owner 变化时重置心跳。"""
        with self._lock:
            current = self._entries.get(session_id)
            heartbeat_seq = (
                current.heartbeat_seq
                if current is not None and current.owner_id == owner_id
                else 0
            )
            self._entries[session_id] = TurnProgress(
                owner_id=owner_id,
                request_id=request_id,
                turn_id=turn_id,
                phase=phase,
                heartbeat_seq=heartbeat_seq,
            )

    def heartbeat_snapshot(self, session_id: str) -> dict[str, Any] | None:
        """原子推进心跳并返回 transport-neutral 快照。"""
        with self._lock:
            entry = self._entries.get(session_id)
            if entry is None:
                return None
            entry.heartbeat_seq += 1
            return {
                "type": "turn_progress",
                "session_id": session_id,
                "request_id": entry.request_id,
                "turn_id": entry.turn_id,
                "phase": entry.phase,
                "heartbeat_seq": entry.heartbeat_seq,
            }

    def remove_owned(self, session_id: str, owner_id: int) -> None:
        """仅删除仍属于指定请求的进度。"""
        with self._lock:
            current = self._entries.get(session_id)
            if current is not None and current.owner_id == owner_id:
                del self._entries[session_id]

    def remove(self, session_id: str) -> None:
        """Remove the turn progress entry for a completed turn."""
        with self._lock:
            self._entries.pop(session_id, None)

    def active_sessions(self) -> list[str]:
        """Return session IDs with active turns (for diagnostics)."""
        with self._lock:
            return list(self._entries.keys())


@dataclass(slots=True)
class TurnActivityRegistry:
    """跟踪每个 Session 正在运行或等待锁的提交任务。"""

    _tasks: dict[str, set[asyncio.Task[Any]]] = field(default_factory=dict)

    def add(self, session_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks.setdefault(session_id, set()).add(task)

    def remove(self, session_id: str, task: asyncio.Task[Any]) -> None:
        tasks = self._tasks.get(session_id)
        if tasks is None:
            return
        tasks.discard(task)
        if not tasks:
            del self._tasks[session_id]

    async def cancel_others(self, session_id: str) -> bool:
        """取消并等待指定 Session 的全部其它活跃提交。"""
        current = asyncio.current_task()
        tasks = {
            task
            for task in self._tasks.get(session_id, set())
            if not task.done() and task is not current
        }
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Cancelled task raised after cancel session=%s", session_id)
        return bool(tasks)
