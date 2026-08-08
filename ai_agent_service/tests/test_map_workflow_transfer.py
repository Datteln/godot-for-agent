"""Map workflow reducer ownership-transfer and isolation regression tests."""

from __future__ import annotations

from app.orchestrator.map_state import MapTaskState

from app.orchestrator.map_workflow import (
    WorkflowEvent,
    dispatch_map_workflow_event,
    reduce_map_workflow,
    reducer_write_scope,
)


def test_reducer_result_is_nested_mutation_isolated_from_input() -> None:
    """The reducer's returned state owns independent nested structures."""
    original = MapTaskState(pending_batches=[{"cells": [{"x": 1, "y": 2}]}])
    event = WorkflowEvent(
        event_seq=1,
        event_type="owned_field_replaced",
        target="Map/Main",
        revision=0,
        payload={"field": "completed_goals", "value": ["goal-1"]},
    )
    with reducer_write_scope():
        reduced = reduce_map_workflow(original, event)
    reduced.pending_batches[0]["cells"][0]["x"] = 99
    assert original.pending_batches[0]["cells"][0]["x"] == 1


def test_dispatch_transfers_reducer_state_without_aliasing_old_input() -> None:
    """Dispatch avoids a second whole-state copy while preserving input isolation."""
    state = MapTaskState(pending_batches=[{"cells": [{"x": 1, "y": 2}]}])
    old_nested = state.pending_batches
    event = WorkflowEvent(
        event_seq=1,
        event_type="owned_field_replaced",
        target="Map/Main",
        revision=0,
        payload={"field": "completed_goals", "value": ["goal-1"]},
    )
    dispatch_map_workflow_event(state, event)
    state.pending_batches[0]["cells"][0]["x"] = 7
    assert old_nested[0]["cells"][0]["x"] == 1
    assert state.completed_goals == ["goal-1"]
