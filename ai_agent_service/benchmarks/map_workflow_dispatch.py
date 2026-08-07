"""Repeatable Map workflow dispatch timing and allocation benchmark."""

from __future__ import annotations

import argparse
import copy
import json
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any

from app.orchestrator.map_progress import MapTaskState
from app.orchestrator.map_workflow import (
    dispatch_map_workflow_event,
    make_map_workflow_event,
    reduce_map_workflow,
    reducer_write_scope,
)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    """One benchmark case result in machine-readable units."""

    case: str
    events: int
    state_entries: int
    elapsed_ms: float
    mean_ms_per_event: float
    peak_allocated_bytes: int

    def to_dict(self) -> dict[str, Any]:
        """Return JSON-native benchmark data."""
        return {
            "case": self.case,
            "events": self.events,
            "state_entries": self.state_entries,
            "elapsed_ms": round(self.elapsed_ms, 3),
            "mean_ms_per_event": round(self.mean_ms_per_event, 6),
            "peak_allocated_bytes": self.peak_allocated_bytes,
        }


def _state(entries: int) -> MapTaskState:
    """Construct a representative state without relying on project data."""
    return MapTaskState(
        planning_contexts={
            f"context-{index}": {
                "target": "Map/Main",
                "revision": index,
                "cells": [{"x": index, "y": index % 32, "source_id": index % 8}],
            }
            for index in range(entries)
        }
    )


def run_case(
    case: str,
    *,
    entries: int,
    events: int,
    simulate_old_double_copy: bool = False,
) -> BenchmarkResult:
    """Dispatch sequential events and measure wall time plus peak traced allocation."""
    state = _state(entries)
    tracemalloc.start()
    started = time.perf_counter()
    for index in range(events):
        event = make_map_workflow_event(
            state,
            "progress_recorded",
            "Map/Main",
            1,
            {"category": "dispatch", "count": index + 1},
        )
        if simulate_old_double_copy:
            with reducer_write_scope():
                reduced = reduce_map_workflow(state, event)
                state.__dict__.clear()
                state.__dict__.update(copy.deepcopy(reduced.__dict__))
        else:
            dispatch_map_workflow_event(state, event)
    elapsed_ms = (time.perf_counter() - started) * 1_000.0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return BenchmarkResult(
        case=case,
        events=events,
        state_entries=entries,
        elapsed_ms=elapsed_ms,
        mean_ms_per_event=elapsed_ms / events,
        peak_allocated_bytes=peak,
    )


def main() -> int:
    """Run small and large deterministic cases and print one JSON document."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=200)
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be positive")
    results = [
        run_case(
            "small_old_double_copy",
            entries=8,
            events=args.events,
            simulate_old_double_copy=True,
        ),
        run_case("small_transfer", entries=8, events=args.events),
        run_case(
            "large_old_double_copy",
            entries=512,
            events=args.events,
            simulate_old_double_copy=True,
        ),
        run_case("large_transfer", entries=512, events=args.events),
    ]
    print(json.dumps([item.to_dict() for item in results], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
