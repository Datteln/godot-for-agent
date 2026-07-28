# Implementation baseline

Recorded on 2026-07-27 before applying this change.

## Python

- `python -m compileall -q ai_agent_service/app`: passed.
- `python -m pytest tests -q`: 177 passed, 9 failed.

Failure ownership:

| Failure | Ownership | Reason |
| --- | --- | --- |
| `test_auto_compact.py::AutoCompactSettingsTests::test_defaults` | Unrelated known issue | The runtime default is 160,000 while the legacy assertion expects 200,000. |
| `test_map_tool.py::test_edit_map_is_registered_as_previewed_map_write` | Superseded safety contract | `expected_revision` and `target_path` are now mandatory for revision-safe writes. |
| Five legacy prompt/tool assertions in `test_map_tool.py` | This change, stale expectations | The assertions require direct `edit_map` access and duplicated prompt rules that the planner/writer pipeline intentionally replaces. |
| `test_runtime_hardening.py::MapCompletionContinuationTests::test_blocked_final_is_converted_to_continuation_prompt` | This change, stale schema expectation | The test constructs a pre-schema-v2 `Session` through a removed constructor alias instead of the migration reader. |
| `test_runtime_hardening.py::MapPlanningProtocolTests::test_complex_map_delegate_requires_visible_plan_first` | This change, stale routing expectation | Planning is moving to the dependency-aware scheduler instead of name/text-based pre-delegation inference. |

## Godot

- Godot 4.6.2 headless editor startup and plugin load: passed with exit code 0.
- Environment-only diagnostics: unavailable Windows root certificate store, editor settings path not writable, and exit-time RID/ObjectDB leak warnings.
- No GDScript parse or plugin initialization failure was reported.

## OpenSpec

- Strict validation of `resolve-map-agent-remediation-backlog`: passed.
