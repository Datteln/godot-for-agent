# Post-Cut Characterization Inventory: `map_turn_pipeline.py`

> **Task:** 7.10
> **Date:** 2026-08-06
> **File:** `ai_agent_service/app/orchestrator/map_turn_pipeline.py` (5599 lines)

## 1. Module-Level Functions and Classes

### Classes

| Class | Line | Description |
|-------|------|-------------|
| `FrontToolCall` | 256 | Frontend tool call data class |
| `_PendingToolMessage` | 292 | Pending tool message container |
| `_PendingServerCall` | 299 | Pending server call container |
| `MapTurnPolicy` | 4492 | Main turn policy class with `run()` static method containing nested `advance()` closure |

### Standalone Functions (70+ total)

**Frame Lifecycle:**
| Function | Line | Lines |
|----------|------|-------|
| `_find_frame` | 339 | ~8 |
| `_frame_in_active_map_edit` | 347 | ~12 |
| `_finish_frame` | 2295 | ~276 |
| `_handle_frame_turns_exhausted` | 2571 | ~65 |
| `_frame_objective` | 1883 | ~10 |
| `_frame_semantic_operation` | 1893 | ~36 |
| `_map_frame_exhausted_payload` | 1929 | ~50 |

**Delegation:**
| Function | Line | Lines |
|----------|------|-------|
| `_delegate_child_frame` | 359 | ~229 |
| `_start_delegate_frame` | 3600 | ~104 |
| `_start_delegate_group` | 3759 | ~376 |
| `_continue_delegate_group` | 988 | ~355 |
| `_map_delegate_result_summary` | 892 | ~34 |
| `_map_delegate_result_payload` | 926 | ~62 |
| `_record_macro_owner` | 3704 | ~55 |
| `_agent_name_has_role` | 2753 | ~15 |
| `_map_agent_targets_from_delegate_call` | 2768 | ~25 |

**Planning:**
| Function | Line | Lines |
|----------|------|-------|
| `_handle_create_plan` | 3399 | ~201 |
| `_requires_create_plan_before_map_delegate` | 2793 | ~16 |
| `_normalize_plan_steps` | 3291 | ~108 |
| `_with_plan_runtime_metadata` | 588 | ~18 |
| `_plan_step_started` | 606 | ~54 |
| `_plan_step_completed` | 660 | ~232 |

**Structured Completion:**
| Function | Line | Lines |
|----------|------|-------|
| `_map_output_schema_for_frame` | 1387 | ~19 |
| `_map_structured_output_error` | 1603 | ~102 |
| `_repair_map_structured_output` | 1705 | ~114 |
| `_apply_reader_structured_completion` | 2037 | ~85 |
| `_apply_map_structured_completion_result` | 2122 | ~173 |
| `_json_object_from_text` | 1530 | ~21 |
| `_json_parse_offset` | 1551 | ~14 |

**Tool Arguments:**
| Function | Line | Lines |
|----------|------|-------|
| `_normalize_tool_args` | 2687 | ~15 |
| `_load_tool_args` | 2702 | ~15 |
| `_normalized_map_layers` | 1343 | ~34 |
| `_normalized_map_layer_value` | 1377 | ~10 |
| `_coerce_schema_value` | 2636 | ~51 |

**Tool Guards / Protocol Errors:**
| Function | Line | Lines |
|----------|------|-------|
| `_planner_route_guard` | 1406 | ~113 |
| `_map_route_contract_error` | 1519 | ~11 |
| `_append_map_write_protocol_errors` | 2809 | ~52 |
| `_append_map_plan_protocol_errors` | 2861 | ~29 |
| `_append_reader_fallback_protocol_errors` | 2890 | ~41 |
| `_append_delegate_protocol_errors` | 2717 | ~18 |
| `_append_create_plan_protocol_errors` | 2735 | ~18 |
| `_append_map_write_followup_protocol_errors` | 3124 | ~67 |
| `_append_map_blocker_once` | 2018 | ~19 |
| `_clear_map_blockers` | 1997 | ~21 |
| `_is_delegate_map_followup` | 3094 | ~30 |
| `_has_pending_map_write_validation` | 2931 | ~15 |
| `_map_validation_arg_error` | 2946 | ~22 |

**Tool Dispatch:**
| Function | Line | Lines |
|----------|------|-------|
| `_map_stage_contract` | 172 | ~84 |
| `_map_stage_for_frame` | 1819 | ~16 |
| `_stage_effective_tools` | 2975 | ~15 |
| `_route_unvalidated_platform_writes_to_validator` | 1835 | ~48 |

**Budgets:**
| Function | Line | Lines |
|----------|------|-------|
| `_uses_persistent_map_budget` | 2968 | ~7 |
| `_sync_map_progress_budget` | 2997 | ~8 |
| `_latest_map_progress_revision` | 2990 | ~7 |

**Events / Callbacks:**
| Function | Line | Lines |
|----------|------|-------|
| `_emit_orchestration_event` | 4229 | ~10 |
| `_event_tool_args` | 4135 | ~25 |
| `_event_result_count` | 4160 | ~16 |
| `_event_result_summary` | 4176 | ~33 |
| `_event_match_items` | 4209 | ~20 |
| `_delta_callback` | 4270 | ~62 |
| `_record_cache_metrics` | 4332 | ~32 |
| `_emit_cache_hit_event` | 4364 | ~36 |
| `_emit_context_usage_event` | 4400 | ~28 |
| `_fallback_callback` | 4428 | ~38 |

**Map Read/Write:**
| Function | Line | Lines |
|----------|------|-------|
| `_payload_revision` | 1979 | ~6 |
| `_same_payload_target` | 1985 | ~6 |
| `_blocker_required_revision` | 1991 | ~6 |
| `_slim_map_delegate_value` | 1565 | ~38 |
| `_region_contains` | 3005 | ~15 |
| `_cached_map_region_summary` | 3020 | ~44 |
| `_resumed_full_map_read_error` | 3064 | ~30 |
| `_with_map_write_metadata` | 3193 | ~98 |
| `_map_turn_exhausted` | 4466 | ~26 |

**Tool Helpers:**
| Function | Line | Lines |
|----------|------|-------|
| `_queued_front_call` | 278 | ~14 |
| `_tool_message` | 310 | ~29 |
| `_history_timeline_payload` | 4239 | ~12 |
| `_estimate_stream_token_count` | 4251 | ~19 |

## 2. TurnDirective/TurnRuntime/TurnOutcome Consumers

The `MapTurnPolicy.run()` method (line 4496) is the primary consumer:
- Returns `TurnOutcome` (FinalTurnOutcome, ToolCallsTurnOutcome, ErrorTurnOutcome)
- Internal `advance()` closure returns `ContinueModel | TurnOutcome`
- Uses `_map_turn_exhausted()` for turn-limit errors
- Uses `_map_route_contract_error()` for route violations
- Uses `_finish_frame()` for frame completion
- Uses `_start_delegate_frame()` / `_start_delegate_group()` for delegation

## 3. Largest Functions (span > 100 lines)

| Function | Line Range | ~Lines |
|----------|------------|--------|
| `MapTurnPolicy.run()` (with `advance` closure) | 4496-5599 | ~1100 |
| `_start_delegate_group` | 3759-4135 | ~376 |
| `_continue_delegate_group` | 988-1343 | ~355 |
| `_finish_frame` | 2295-2571 | ~276 |
| `_plan_step_completed` | 660-892 | ~232 |
| `_delegate_child_frame` | 359-588 | ~229 |
| `_handle_create_plan` | 3399-3600 | ~201 |
| `_apply_map_structured_completion_result` | 2122-2295 | ~173 |
| `_repair_map_structured_output` | 1705-1819 | ~114 |
| `_planner_route_guard` | 1406-1519 | ~113 |

## 4. Dependencies

**External:** `asyncio`, `copy`, `hashlib`, `json`, `logging`, `time`, `re`, `collections.abc`, `dataclasses`, `pathlib`, `typing`

**Internal (`app.*`):**
- `app.agents.bundled` → `get_agent`
- `app.agents.types` → `AgentDefinition`, `EffortLevel`, `Frame`
- `app.history_bounds` → `bounded_tool_message_body`, `summarize_history_text`
- `app.llm.cache_decision_engine` → `CacheDecision`, `CacheDecisionEngine`
- `app.llm.cache_observability` → `CacheMetricsCollector`, `CacheMetricsSnapshot`
- `app.llm.provider` → `AssistantTurn`, `LLMError`, `LLMProvider`, `ResponseContract`, `ToolCallRequest`
- `app.orchestrator.map_artifacts` → `DelegateArtifactStore`
- `app.orchestrator.map_capabilities` → multiple
- `app.orchestrator.map_contracts` → multiple
- `app.orchestrator.map_progress` → multiple
- `app.orchestrator.map_routing` → `MapTaskRoutingAssessment`, `assess_map_task`
- `app.orchestrator.runtime_contracts` → `MapWorkflowEvent`
- `app.orchestrator.turn.contracts` → `TurnDirective`, `TurnOutcome`, etc.
- `app.orchestrator.turn.model_policy` → `EFFORT_TEMPERATURE`, `resolve_thinking_budget`
- `app.orchestrator.turn.runtime` → `TurnRuntime`
- `app.orchestrator.turn.tool_execution` → `ToolExecutionResult`, `execute_tool_calls`
- `app.query.helpers` → `_build_model_messages`, `_parse_model_response`
- `app.security.settings` → `SecuritySettings`
- `app.sessions.schema` → `Session`, `SessionAllowGrant`
- `app.tools.context` → `ToolContext`
- `app.verify.runner` → `VerifyRunner`
- `app.workflow.contracts` → `WorkflowEvent`

## 5. Map-Specific Logic

Nearly every function in this file is Map-specific:
- Concrete Map tool names referenced: `edit_map`, `read_map`, `create_plan`, `validate_map`, `preview_map`, `confirm_map`, `cancel_map`, `list_map_artifacts`, `delegate`, `delegate_many`
- Map-specific Session fields: `map_task_state`, `agent_stack`, `map_stage`, `map_plan`, `map_output_schema`
- Map workflow concepts: `map_task_state.scopes`, `map_task_state.executed_batches`, `map_task_state.transaction_journals`

## 6. Private Test Imports

None found directly in the file. Tests import `MapTurnPolicy` from this module:
- `tests/test_map_planner_pipeline.py`
- `tests/test_map_structured_results.py`
- `tests/test_map_workflow_hardening.py`
- `tests/test_coordinated_map_commit.py`
- `tests/test_plan_scheduler_hardening.py`
- `tests/test_runtime_hardening.py`

## 7. Current Behavior Summary

The `MapTurnPolicy.run()` method is the main entry point. It:
1. Validates the frame stack and route contracts
2. Enters a model/tool loop (the `advance` closure):
   - Builds model messages and invokes LLM
   - Handles cache decisions and metrics
   - Classifies responses (tool calls, delegation, structured output, final)
   - Executes server tools or stages frontend tools
   - Handles frame completion, delegation, planning, and exhaustion
   - Emits events for progress tracking
3. Returns a `TurnOutcome`