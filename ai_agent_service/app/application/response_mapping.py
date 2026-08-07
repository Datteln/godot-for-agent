"""Canonical mapping between Turn outcomes, durable payloads, and API responses."""

from __future__ import annotations

from typing import Any

from app.api.schemas import (
    ChatErrorResponse,
    ChatFinalResponse,
    ChatResponse,
    ChatToolCallsResponse,
    FrontToolCallDTO,
)
from app.orchestrator.turn.contracts import (
    ErrorTurnOutcome,
    FinalTurnOutcome,
    ToolCallsTurnOutcome,
    TurnOutcome,
)


def chat_response_from_payload(data: dict[str, Any]) -> ChatResponse:
    """Validate a durable response payload as one current API response variant."""
    response_type = data.get("type")
    if response_type == "tool_calls":
        return ChatToolCallsResponse.model_validate(data)
    if response_type == "final":
        return ChatFinalResponse.model_validate(data)
    return ChatErrorResponse.model_validate(data)


def map_turn_outcome(step: TurnOutcome) -> ChatResponse:
    """Project one closed TurnOutcome to the closed chat response contract."""
    if isinstance(step, ToolCallsTurnOutcome):
        return ChatToolCallsResponse(
            turn_id=step.turn_id,
            text=step.text,
            calls=[
                FrontToolCallDTO(
                    id=str(call["id"]),
                    name=str(call["name"]),
                    input=dict(call["input"]),
                    needs_confirm=bool(call["needs_confirm"]),
                    frame_id=str(call["frame_id"]),
                    agent=str(call["agent"]),
                    render_kind=(
                        str(call["render_kind"])
                        if call.get("render_kind") is not None
                        else None
                    ),
                )
                for call in step.calls
            ],
        )
    if isinstance(step, FinalTurnOutcome):
        return ChatFinalResponse(text=step.text)
    if isinstance(step, ErrorTurnOutcome):
        return ChatErrorResponse(text=step.text, error_code=step.error_code)
    raise TypeError(f"unknown TurnOutcome type: {type(step)!r}")
