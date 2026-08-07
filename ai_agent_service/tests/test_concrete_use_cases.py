"""Tests for the two concrete submission command boundaries."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from app.api.schemas import ChatFinalResponse, ChatRequest, ToolResult
from app.application.submission.tool_result_submission import ToolResultSubmissionUseCase
from app.application.submission.user_submission import UserSubmissionUseCase


class UserSubmissionUseCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_only_user_message_commands(self) -> None:
        coordinator = AsyncMock()
        coordinator.submit_user_turn.return_value = ChatFinalResponse(text="done")
        use_case = UserSubmissionUseCase(coordinator)
        command = ChatRequest(session_id="s1", request_id="r1", user_message="hello")

        response = await use_case.execute(command)

        self.assertEqual(response.text, "done")
        coordinator.submit_user_turn.assert_awaited_once_with(command)

    async def test_rejects_tool_result_commands(self) -> None:
        use_case = UserSubmissionUseCase(AsyncMock())
        command = ChatRequest(
            session_id="s1",
            request_id="r1",
            tool_results=[
                ToolResult(
                    tool_use_id="c1",
                    frame_id="f1",
                    turn_id="t1",
                    status="applied",
                )
            ],
        )

        with self.assertRaisesRegex(ValueError, "user submission"):
            await use_case.execute(command)


class ToolResultSubmissionUseCaseTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def command() -> ChatRequest:
        return ChatRequest(
            session_id="s1",
            request_id="r1",
            tool_results=[
                ToolResult(
                    tool_use_id="c1",
                    frame_id="f1",
                    turn_id="t1",
                    status="applied",
                    result={"ok": True},
                )
            ],
        )

    async def test_accepts_only_tool_result_commands(self) -> None:
        coordinator = AsyncMock()
        lifecycle = AsyncMock()
        coordinator.submit_user_turn.return_value = ChatFinalResponse(text="done")
        use_case = ToolResultSubmissionUseCase(coordinator, lifecycle)
        command = self.command()

        response = await use_case.execute(command)

        self.assertEqual(response.text, "done")
        coordinator.submit_user_turn.assert_awaited_once_with(command)

    async def test_discard_has_one_lifecycle_owner(self) -> None:
        coordinator = AsyncMock()
        lifecycle = AsyncMock()
        lifecycle.discard_pending.return_value = ChatFinalResponse(text="discarded")
        use_case = ToolResultSubmissionUseCase(coordinator, lifecycle)

        response = await use_case.discard_pending("s1")

        self.assertEqual(response.text, "discarded")
        lifecycle.discard_pending.assert_awaited_once_with("s1")

    async def test_rejects_user_message_commands(self) -> None:
        use_case = ToolResultSubmissionUseCase(AsyncMock(), AsyncMock())
        command = ChatRequest(session_id="s1", request_id="r1", user_message="hello")

        with self.assertRaisesRegex(ValueError, "tool-result submission"):
            await use_case.execute(command)


if __name__ == "__main__":
    unittest.main()
