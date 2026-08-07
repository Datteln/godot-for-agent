"""Route-facing tool-result submission use case."""

from __future__ import annotations

from dataclasses import dataclass

from app.api.schemas import ChatRequest, ChatResponse
from app.application.lifecycle import SessionLifecycleService
from app.application.submission.coordinator import SubmissionCoordinator


@dataclass(frozen=True, slots=True)
class ToolResultSubmissionUseCase:
    """Validate a tool-result command and enter its atomic transaction."""

    coordinator: SubmissionCoordinator
    lifecycle: SessionLifecycleService

    async def execute(self, command: ChatRequest) -> ChatResponse:
        if command.user_message is not None or not command.tool_results:
            raise ValueError("tool-result submission requires tool_results only")
        return await self.coordinator.submit_user_turn(command)

    async def discard_pending(self, session_id: str) -> ChatResponse:
        return await self.lifecycle.discard_pending(session_id)
