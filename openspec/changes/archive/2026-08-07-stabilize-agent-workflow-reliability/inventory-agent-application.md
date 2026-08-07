# Post-Cut Characterization Inventory: `AgentApplication` / `application/service.py`

> **Task:** 8.10
> **Date:** 2026-08-06
> **File:** `ai_agent_service/app/application/service.py` (4180 lines)

## 1. Module-Level Classes and Functions

### Classes

| Class | Line | Description |
|-------|------|-------------|
| `_SubmissionPublicationBuffer` | 334 | Buffers events, artifacts, and workflow data for coordinated publication |
| `_TurnProgress` | 349 | Tracks turn progress state |
| `AgentApplication` | 957 | Main application facade (~3200 lines) |

### Standalone Functions

None at module level. All logic is in `AgentApplication` methods.

## 2. AgentApplication Methods

| Method | Line | Lines | Description |
|--------|------|-------|-------------|
| `__init__` | 958 | ~180 | Dependency injection constructor |
| `resume_pending_recoveries` | 1139 | ~256 | Resume pending durable task recoveries on startup |
| `submit_user_turn` | 1395 | ~642 | Main entry point for user submission |
| `_submit_with_backend_recovery` | 2037 | ~103 | Wraps submission with recovery retry |
| `_submit_locked` | 2140 | ~426 | Core submission logic under Session lock |
| `_run_agent_turn` | 2566 | ~144 | Orchestrates a single agent turn |
| `_enrich_front_image_result` | 2710 | ~91 | Enriches image tool results |
| `_append_tool_results` | 2801 | ~659 | Appends tool results to frame messages |
| `_run_verify` | 3460 | ~10 | Delegates to VerifyRunner |
| `_cancel_active_tasks` | 3470 | ~25 | Cancels active asyncio tasks |
| `reset` | 3495 | ~63 | Session reset with epoch change |
| `interrupt` | 3558 | ~119 | Interrupts active request |
| `discard_pending` | 3677 | ~38 | Discards pending tool results |
| `set_effort` | 3715 | ~13 | Sets effort level |
| `resume_paused_map_task` | 3728 | ~58 | Resumes paused Map workflow |
| `cancel_map_task` | 3786 | ~54 | Cancels Map task |
| `set_output_style` | 3840 | ~15 | Sets output style |
| `compact` | 3855 | ~26 | Entry point for compaction |
| `_compact_locked_async` | 3881 | ~10 | Compaction under lock |
| `_retrieve_rag_context` | 3891 | ~50 | RAG context retrieval |
| `session_history` | 3941 | ~50 | Returns session history |
| `_build_child_agent_prompt` | 2236 | ~330 | Nested function for building child agent prompts |

## 3. Forwarding-Only / Thin Wrapper Methods

These methods are thin wrappers that delegate to other methods with minimal logic:

| Method | Delegates To |
|--------|-------------|
| `_run_verify` (3460) | `VerifyRunner` |
| `_compact_locked_async` (3881) | `QueryEngine.compact` (now deleted) |
| `set_effort` (3715) | Direct Session field mutation |
| `set_output_style` (3840) | Direct Session field mutation |
| `_cancel_active_tasks` (3470) | `asyncio` task management |
| `_retrieve_rag_context` (3891) | `RagIndexBuildManager` |

## 4. Global Publication Context

- `_SubmissionPublicationBuffer` (line 334): Mutable buffer for events, artifacts, workflow data
- Created per-request in `submit_user_turn` and passed through the call chain
- No module-level `ContextVar` or global state found (task 8.12 already removed it)

## 5. Wildcard Helper Dependencies

None found. All imports are explicit.

## 6. Route Wiring

The `AgentApplication` is consumed by `ApplicationUseCases` in `use_cases.py`, which is injected into FastAPI routes in `routes.py`.

Route → UseCase → AgentApplication method mapping:
- `POST /chat` → `UserSubmissionUseCase.execute()` → `submit_user_turn()`
- `POST /chat` (tool_results) → `ToolResultSubmissionUseCase.execute()` → `submit_user_turn()`
- `POST /chat/discard_pending` → `ToolResultSubmissionUseCase.discard_pending()` → `discard_pending()`
- `POST /chat/reset` → `ResetUseCase.execute()` → `reset()`
- `POST /chat/interrupt` → `InterruptionUseCase.execute()` → `interrupt()`
- `GET /chat/history` → `HistoryUseCase.execute()` → `session_history()`
- `POST /chat/compact` → `CompactionUseCase.execute()` → `compact()`
- `POST /chat/map/resume` → `ResumeUseCase.execute()` → `resume_paused_map_task()`
- `POST /chat/map/cancel` → `MapTaskControlUseCase.execute()` → `cancel_map_task()`
- `POST /chat/settings/effort` → `SessionSettingsUseCase.set_effort()` → `set_effort()`
- `POST /chat/settings/output_style` → `SessionSettingsUseCase.set_output_style()` → `set_output_style()`

## 7. Transaction Boundaries

Session locking occurs in `_submit_locked()` (line 2140):
- Uses `asyncio.Lock` per session
- Creates working copy, applies mutations, persists

Publication buffering occurs in `_SubmissionPublicationBuffer`:
- Events, artifacts, and workflow data are buffered
- Published atomically on commit

## 8. Private Test Imports

Tests importing `AgentApplication`:
- `tests/test_coordinated_map_commit.py`
- `tests/test_durable_recovery_matrix.py`
- `tests/test_session_reset_recovery.py`
- `tests/test_map_owner_state.py`
- `tests/test_chat_event_streaming.py`

## 9. Dependencies

**External:** `asyncio`, `hashlib`, `json`, `logging`, `time`, `collections.abc`, `contextvars`, `dataclasses`, `typing`

**Internal (`app.*`):**
- `app.api.schemas` → `ChatRequest`, `ChatResponse`, `InterruptCause`, `InterruptResponse`, `ResetResponse`, `SessionHistoryResponse`
- `app.application.completed_turns` → `CompletedTurnLedger`, `TurnCompletionRecord`
- `app.application.response_mapping` → `chat_response_from_payload`
- `app.config` → `AppSettings`
- `app.events.store` → `EventStore`
- `app.llm.provider` → `LLMProvider`
- `app.memory.store` → `MemoryStore`
- `app.orchestrator.map_artifacts` → `DelegateArtifactStore`
- `app.orchestrator.map_turn_pipeline` → `MapTurnPolicy`
- `app.orchestrator.turn.contracts` → `TurnOutcome`
- `app.output_styles.catalog` → `OutputStyleCatalog`
- `app.query.helpers` → `_build_model_messages`
- `app.recovery.pointer` → `RecoveryPointerStore`
- `app.recovery.supervisor` → `RecoverySupervisor`
- `app.security.settings` → `SecuritySettings`
- `app.sessions.resource_registry` → `ResourceRegistry`
- `app.sessions.schema` → `Session`
- `app.sessions.store` → `SessionStore`
- `app.skills.catalog` → `SkillCatalog`
- `app.tools.context` → `ToolContext`
- `app.verify.runner` → `VerifyRunner`