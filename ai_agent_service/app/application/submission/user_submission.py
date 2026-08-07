"""Route-facing user-submission use case."""

from __future__ import annotations

from dataclasses import dataclass

from app.api.schemas import ChatRequest, ChatResponse
from app.application.submission.coordinator import SubmissionCoordinator


@dataclass(frozen=True, slots=True)
class UserSubmissionUseCase:
    """Validate the command kind and enter the atomic submission transaction."""

    coordinator: SubmissionCoordinator

    async def execute(self, command: ChatRequest) -> ChatResponse:
        if command.user_message is None or command.tool_results:
            raise ValueError("user submission requires user_message only")
        return await self.coordinator.submit_user_turn(command)
