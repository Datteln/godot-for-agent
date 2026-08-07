"""Closed commands, directives, and outcomes for the turn state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

JsonObject: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class TurnCommand:
    """Start or continue one bounded turn under an explicit identity."""

    session_id: str
    session_epoch: str
    request_id: str
    user_text: str | None = None
    tool_results: tuple[JsonObject, ...] = ()
    context: JsonObject = field(default_factory=dict)
    effort: str | None = None
    permission_mode: str | None = None
    output_style: str | None = None

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.session_epoch.strip():
            raise ValueError("turn command requires session_id and session_epoch")
        if not self.request_id.strip():
            raise ValueError("turn command requires request_id")
        has_text = self.user_text is not None and bool(self.user_text.strip())
        if has_text == bool(self.tool_results):
            raise ValueError("turn command requires exactly one of user_text or tool_results")


@dataclass(frozen=True, slots=True)
class ContinueModel:
    reason: str
    kind: Literal["continue_model"] = "continue_model"


@dataclass(frozen=True, slots=True)
class SuspendForFrontend:
    frame_id: str
    turn_id: str
    calls: tuple[JsonObject, ...]
    text: str | None = None
    kind: Literal["suspend_frontend"] = "suspend_frontend"


@dataclass(frozen=True, slots=True)
class PauseWorkflow:
    reason_code: str
    checkpoint: JsonObject
    user_text: str
    kind: Literal["pause_workflow"] = "pause_workflow"


@dataclass(frozen=True, slots=True)
class CompleteTurn:
    text: str
    metadata: JsonObject = field(default_factory=dict)
    kind: Literal["complete_turn"] = "complete_turn"


@dataclass(frozen=True, slots=True)
class FailTurn:
    error_code: str
    text: str
    retryable: bool = False
    details: JsonObject = field(default_factory=dict)
    kind: Literal["fail_turn"] = "fail_turn"


TurnDirective: TypeAlias = (
    ContinueModel
    | SuspendForFrontend
    | PauseWorkflow
    | CompleteTurn
    | FailTurn
)


@dataclass(frozen=True, slots=True)
class FinalTurnOutcome:
    text: str
    metadata: JsonObject = field(default_factory=dict)
    kind: Literal["final"] = "final"


@dataclass(frozen=True, slots=True)
class ToolCallsTurnOutcome:
    turn_id: str
    calls: tuple[JsonObject, ...]
    text: str | None = None
    kind: Literal["tool_calls"] = "tool_calls"


@dataclass(frozen=True, slots=True)
class PausedTurnOutcome:
    reason_code: str
    checkpoint: JsonObject
    text: str
    kind: Literal["paused"] = "paused"


@dataclass(frozen=True, slots=True)
class ErrorTurnOutcome:
    error_code: str
    text: str
    retryable: bool = False
    details: JsonObject = field(default_factory=dict)
    kind: Literal["error"] = "error"


TurnOutcome: TypeAlias = (
    FinalTurnOutcome | ToolCallsTurnOutcome | PausedTurnOutcome | ErrorTurnOutcome
)


def directive_from_outcome(outcome: TurnOutcome) -> TurnDirective:
    """Convert a terminal domain transition into the closed directive union."""
    if isinstance(outcome, FinalTurnOutcome):
        return CompleteTurn(text=outcome.text, metadata=outcome.metadata)
    if isinstance(outcome, ToolCallsTurnOutcome):
        return SuspendForFrontend(
            frame_id=str(outcome.calls[0].get("frame_id", "")) if outcome.calls else "",
            turn_id=outcome.turn_id,
            calls=outcome.calls,
            text=outcome.text,
        )
    if isinstance(outcome, PausedTurnOutcome):
        return PauseWorkflow(
            reason_code=outcome.reason_code,
            checkpoint=outcome.checkpoint,
            user_text=outcome.text,
        )
    return FailTurn(
        error_code=outcome.error_code,
        text=outcome.text,
        retryable=outcome.retryable,
        details=outcome.details,
    )
