# Implementation Baseline

Recorded on 2026-08-06 before the clean-cut implementation.

## Validation and test environment

- `openspec validate stabilize-agent-workflow-reliability --strict --json`: passed with no issues.
- Service virtual environment: CPython 3.14.3.
- `python -m unittest discover -s tests`: discovered 179 tests and completed in 9.989 seconds; 10 test modules failed import because `pytest` is not installed in the existing virtual environment. No dependency was installed as part of this baseline.
- Godot: `4.6.2.stable.official.71f334935` is available.
- Godot test inventory: 13 scripts under `ai_agent_frontend/tests` and `ai_agent_frontend/addons/ai_agent/tests`.

## Current transport, settings, and architecture inventory

- FastAPI route inventory: 16 routes in `app/api/routes.py`.
- Legacy event delivery is `GET /chat/events`; the Godot client builds that path in `agent_http_client.gd`.
- Polling configuration exists as `ai_agent/event_poll_interval_sec` in `config_migrations.gd` and is consumed by `agent_http_client.gd`.
- `agent.py`: 5,803 lines / 237,085 bytes; top-level `run_turn()` starts at line 4,707.
- `query/engine.py`: 4,092 lines / 186,752 bytes; `QueryEngine` starts at line 963.
- `chat_panel.gd`: 3,155 lines / 126,532 bytes.
- A boolean Verify response remains in `app/api/schemas.py`.
- Embedded workflow persistence remains in `MapTaskState.workflow_events`, and the reducer slices it to 512 entries.

## Persistence sizes

The checked local runtime data contained no Session files and five planning artifacts:

- three repair documents of 728 bytes each;
- planning snapshots of 6,229 bytes and 3,618 bytes.

These files are inventory only; their content was not used or modified.

## Performance baseline

A release-build-equivalent benchmark harness did not exist. A local CPython 3.14.3 reducer micro-benchmark applied 200 `progress_recorded` events to `MapTaskState` using the current deepcopy reducer:

- total: 89.490 ms;
- mean: 0.447 ms/event;
- retained embedded events: 200.

This measurement is a directional baseline for the later snapshot/segment benchmark, not a release acceptance threshold.

## Line endings

- `agent.py`: mixed (`5,803` LF, `5,792` CRLF).
- `query/engine.py`: CRLF (`4,092` CRLF).
- `chat_panel.gd`: CRLF (`3,155` CRLF).
- OpenSpec task artifacts: LF.

New and replaced source files use LF. Unrelated existing line endings are not normalized by this change.
