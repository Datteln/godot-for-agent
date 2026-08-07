"""Tests for the closed, domain-neutral TurnDriver state machine."""

from __future__ import annotations

import unittest

from app.orchestrator.turn.contracts import (
    CompleteTurn,
    ContinueModel,
    FailTurn,
    FinalTurnOutcome,
    PauseWorkflow,
    SuspendForFrontend,
)
from app.orchestrator.turn.driver import TurnDriver


class TurnDriverStateMachineTests(unittest.IsolatedAsyncioTestCase):
    async def test_continue_reuses_one_bounded_loop(self) -> None:
        visited: list[int] = []

        async def transition(index: int):
            visited.append(index)
            if index < 2:
                return ContinueModel(reason="next_model_cycle")
            return CompleteTurn(text="done", metadata={"cycles": len(visited)})

        outcome = await TurnDriver.drive(
            maximum=4,
            transition=transition,
            exhausted=lambda: FinalTurnOutcome(text="exhausted"),
        )

        self.assertEqual(visited, [0, 1, 2])
        self.assertEqual(outcome.kind, "final")
        self.assertEqual(outcome.metadata, {"cycles": 3})

    async def test_exact_budget_returns_domain_exhaustion_outcome(self) -> None:
        calls = 0

        async def transition(_index: int):
            nonlocal calls
            calls += 1
            return ContinueModel(reason="again")

        outcome = await TurnDriver.drive(
            maximum=2,
            transition=transition,
            exhausted=lambda: FinalTurnOutcome(text="exhausted"),
        )

        self.assertEqual(calls, 2)
        self.assertEqual(outcome.text, "exhausted")

    async def test_frontend_suspension_preserves_identity(self) -> None:
        async def transition(_index: int):
            return SuspendForFrontend(
                frame_id="frame-1",
                turn_id="turn-1",
                calls=({"id": "call-1", "frame_id": "frame-1"},),
                text="approve",
            )

        outcome = await TurnDriver.drive(
            maximum=1,
            transition=transition,
            exhausted=lambda: FinalTurnOutcome(text="exhausted"),
        )

        self.assertEqual(outcome.kind, "tool_calls")
        self.assertEqual(outcome.turn_id, "turn-1")
        self.assertEqual(outcome.calls[0]["id"], "call-1")

    async def test_pause_preserves_checkpoint(self) -> None:
        async def transition(_index: int):
            return PauseWorkflow(
                reason_code="awaiting_user",
                checkpoint={"step": 3},
                user_text="waiting",
            )

        outcome = await TurnDriver.drive(
            maximum=1,
            transition=transition,
            exhausted=lambda: FinalTurnOutcome(text="exhausted"),
        )

        self.assertEqual(outcome.kind, "paused")
        self.assertEqual(outcome.checkpoint, {"step": 3})

    async def test_failure_preserves_retry_details(self) -> None:
        async def transition(_index: int):
            return FailTurn(
                error_code="provider_exhausted",
                text="all attempts failed",
                retryable=True,
                details={"wire_attempts": 5},
            )

        outcome = await TurnDriver.drive(
            maximum=1,
            transition=transition,
            exhausted=lambda: FinalTurnOutcome(text="exhausted"),
        )

        self.assertEqual(outcome.kind, "error")
        self.assertTrue(outcome.retryable)
        self.assertEqual(outcome.details, {"wire_attempts": 5})

    async def test_non_positive_budget_fails_closed(self) -> None:
        async def transition(_index: int):
            return CompleteTurn(text="unreachable")

        with self.assertRaisesRegex(ValueError, "maximum must be positive"):
            await TurnDriver.drive(
                maximum=0,
                transition=transition,
                exhausted=lambda: FinalTurnOutcome(text="exhausted"),
            )


if __name__ == "__main__":
    unittest.main()
