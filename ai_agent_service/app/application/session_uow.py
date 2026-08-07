"""Per-session serialization and working-copy boundary for submissions."""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.sessions.store import Session, SessionStore


@dataclass(frozen=True, slots=True)
class SessionWorkingSet:
    """Original snapshot and isolated aggregate used by one submission."""

    snapshot: Session
    session: Session


@dataclass(frozen=True, slots=True)
class SessionUnitOfWork:
    """Own the Session lock and construction of rollback-safe working state."""

    store: SessionStore

    @asynccontextmanager
    async def serialize(self, session_id: str) -> AsyncIterator[None]:
        """Serialize all state-changing use cases for one Session identity."""
        async with self.store.lock_for(session_id):
            yield

    @staticmethod
    def working_set(session: Session, *, isolate: bool) -> SessionWorkingSet:
        """Create the rollback snapshot and, when required, an isolated aggregate."""
        snapshot = copy.deepcopy(session)
        working = copy.deepcopy(session) if isolate else session
        return SessionWorkingSet(snapshot=snapshot, session=working)

    def restore(self, session_id: str, snapshot: Session) -> None:
        """Restore the last committed in-memory aggregate after a failed attempt."""
        self.store.replace_in_memory(session_id, snapshot)
