"""Boundary tests for conversion into the five canonical turn directives."""

from __future__ import annotations

import unittest

from app.orchestrator.turn.contracts import (
    CompleteTurn,
    ContinueModel,
    ErrorTurnOutcome,
    FailTurn,
    FinalTurnOutcome,
    PausedTurnOutcome,
    PauseWorkflow,
    SuspendForFrontend,
    ToolCallsTurnOutcome,
    directive_from_outcome,
)


class DirectiveBoundaryTests(unittest.TestCase):
    def test_closed_directive_union_has_only_five_runtime_variants(self) -> None:
        variants = {
            type(ContinueModel(reason="continue")),
            type(SuspendForFrontend(frame_id="f", turn_id="t", calls=())),
            type(PauseWorkflow(reason_code="pause", checkpoint={}, user_text="wait")),
            type(CompleteTurn(text="done")),
            type(FailTurn(error_code="failed", text="failed")),
        }

        self.assertEqual(
            {variant.__name__ for variant in variants},
            {"ContinueModel", "SuspendForFrontend", "PauseWorkflow", "CompleteTurn", "FailTurn"},
        )

    def test_final_outcome_becomes_complete_turn(self) -> None:
        directive = directive_from_outcome(
            FinalTurnOutcome(text="done", metadata={"source": "map"})
        )
        self.assertEqual(directive, CompleteTurn(text="done", metadata={"source": "map"}))

    def test_frontend_outcome_becomes_suspension_with_frame_identity(self) -> None:
        directive = directive_from_outcome(
            ToolCallsTurnOutcome(
                turn_id="t1",
                calls=({"id": "c1", "frame_id": "f1"},),
                text="approve",
            )
        )
        self.assertEqual(directive.kind, "suspend_frontend")
        self.assertEqual(directive.frame_id, "f1")

    def test_paused_outcome_becomes_pause(self) -> None:
        directive = directive_from_outcome(
            PausedTurnOutcome(reason_code="awaiting_user", checkpoint={"step": 1}, text="wait")
        )
        self.assertEqual(directive.kind, "pause_workflow")
        self.assertEqual(directive.checkpoint, {"step": 1})

    def test_error_outcome_becomes_failure(self) -> None:
        directive = directive_from_outcome(
            ErrorTurnOutcome(
                error_code="budget_exhausted",
                text="exhausted",
                retryable=False,
                details={"limit": 3},
            )
        )
        self.assertEqual(directive.kind, "fail_turn")
        self.assertEqual(directive.details, {"limit": 3})


if __name__ == "__main__":
    unittest.main()
