"""Session context compaction and RAG retrieval service."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.application.model_selection import model_for_effort
from app.config import AppSettings
from app.llm.cache_decision_engine import CacheDecisionEngine
from app.llm.provider import LLMProvider
from app.prompt.rag_context import build_rag_context
from app.query.compactor import SessionCompactor
from app.rag.factory import create_codebase_index
from app.security.settings import SecuritySettings
from app.sessions.store import Session, SessionStore


class SessionContextService:
    """Owns compact locking and bounded RAG retrieval."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        store: SessionStore,
        llm: LLMProvider,
        cache_engine: CacheDecisionEngine,
        emit: Callable[[str, str, dict[str, object]], int],
        available_tools: Callable[[], set[str]],
    ) -> None:
        self._settings = settings
        self._store = store
        self._compactor = SessionCompactor(
            settings,
            store,
            llm,
            cache_engine,
            emit,
            available_tools,
            lambda effort: model_for_effort(settings, effort),
        )

    def needs_auto_compact(self, session: Session) -> bool:
        return self._compactor.needs_auto_compact(session)

    async def compact(
        self,
        session_id: str,
        *,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, object]:
        """Acquire the Session lock and compact its context."""
        async with self._store.lock_for(session_id):
            return await self.compact_locked(
                session_id,
                keep_recent=keep_recent,
                triggered_by=triggered_by,
                use_llm=use_llm,
            )

    async def compact_locked(
        self,
        session_id: str,
        *,
        keep_recent: int = 12,
        triggered_by: str = "manual",
        use_llm: bool | None = None,
    ) -> dict[str, object]:
        """Compact while the caller owns the Session lock."""
        return await self._compactor.compact_locked(
            session_id,
            keep_recent,
            triggered_by,
            use_llm,
        )

    async def retrieve_rag(
        self,
        security: SecuritySettings,
        user_message: str,
    ) -> str:
        """Retrieve the L3 RAG segment without blocking the event loop."""
        index = create_codebase_index(self._settings, security)
        return await asyncio.to_thread(build_rag_context, index, user_message)
