## Context

The provider already requests an OpenAI-compatible stream and invokes `on_delta` while iterating chunks. The loss of streaming happens later: for a `/chat` request containing `tool_results`, `submit_user_turn()` installs `_SubmissionPublicationBuffer`; `_emit()` then stages every event, including `agent_text_delta` and `agent_reasoning_delta`, and `_flush_submission_publications()` appends them only after `_store.save()` succeeds.

The captured logs demonstrate this boundary. The upstream response is `text/event-stream`, the provider finishes a streamed tool-call response at 19:20:46.970, and at 19:20:47.040 the service begins appending the previously staged sequence. The next `/chat/events` query returns 203 events in one response; a later query returns 41. The Godot client emits each response as one array and the chat panel drains it synchronously, while deferred layout changes can move the scrollbar away from the new bottom and trigger `_auto_scroll = false`.

Atomic tool-result submission is still required. A failed or interrupted working copy must not expose grants, pending-tool transitions, map artifact locators, or recoverable history as committed facts.

## Goals / Non-Goals

**Goals:**

- Make model text and reasoning visible during the provider stream even when a tool-result transaction is active.
- Preserve Session, tool, grant, workflow, and artifact atomicity.
- Give provisional preview output an explicit commit or discard lifecycle.
- Bound server batches and client per-frame work so a backlog cannot freeze layout or disable follow mode.
- Keep `/chat` request/response compatibility and the existing event cursor model.
- Add measurable first-preview latency, batch-size, backlog, commit, and discard diagnostics.

**Non-Goals:**

- Replacing `POST /chat` with an SSE response.
- Persisting token-by-token preview events as a second authoritative conversation history.
- Changing model selection, retry, fallback, or tool-result idempotency rules.
- Forcing the viewport to the bottom after the user intentionally scrolls upward.

## Decisions

### 1. Split presentation previews from transactional publications

Introduce an explicit delivery classification in the submission publication layer:

- `provisional_preview`: `agent_text_delta` and `agent_reasoning_delta` generated while a tool-result working copy is active.
- `transactional`: events that describe tool results, grants, workflow state, artifacts, pending calls, final state, or any other recoverable Session fact.
- `out_of_band_liveness`: the existing `turn_progress` snapshot.

`provisional_preview` events are appended immediately to the process-local EventStore with a stable `preview_id`, `request_id`, `turn_id`, and `provisional=true`. Their history representation is still applied only to the isolated working Session and becomes durable solely through the existing Session save. They are not added a second time during publication flush.

Transactional events and map artifacts remain staged exactly as today.

Alternative considered: flush all staged events immediately. Rejected because it would expose tool and workflow state that may later roll back.

Alternative considered: send only `turn_progress` heartbeats until commit. Rejected because it prevents timeout but does not provide actual streaming text or fix burst rendering.

### 2. Give each preview a commit/discard boundary

The publication buffer tracks every provisional preview stream and its emitted sequence range. After Session persistence and transactional publication succeed, the service emits `submission_preview_committed`. If the working copy is cancelled, rejected, or fails persistence, it emits `submission_preview_discarded` before clearing the active submission.

The frontend may display provisional text immediately, but it must keep enough identity to:

- mark it committed without duplicating text;
- remove or visibly invalidate it on discard;
- ignore a late boundary from an older request;
- avoid restoring an uncommitted preview from Session history.

If the process dies before either boundary, startup/history reconciliation treats previews without a matching committed Session request identity as discarded. EventStore remains a process-local transport buffer, not a recovery source of truth.

Alternative considered: silently leave rolled-back preview text in the panel. Rejected because it presents output from a transaction that the service explicitly says never happened.

### 3. Bound event responses and expose backlog state

`GET /chat/events` returns at most a configured limit, with a conservative default such as 50 and a hard maximum. The response includes `has_more` and the returned cursor. Ordering remains by sequence number. The client advances its cursor only through events actually accepted for processing.

When `has_more=true`, the client schedules another poll immediately after the current HTTPRequest disconnects instead of waiting for the normal one-second timer. Normal idle polling and liveness heartbeats remain unchanged.

This is application-level backpressure rather than transport-level SSE. It fits Godot's existing HTTPRequest integration and fixes the observed 203-event single response without changing `/chat`.

### 4. Drain UI events with a frame budget and preserve explicit follow intent

The chat panel enqueues accepted events in sequence order but processes only a bounded number or bounded elapsed time per frame. Delta coalescing happens before enqueue and must preserve append-only fragments.

Auto-scroll becomes an explicit follow state:

- sending a message or returning manually to the bottom enables follow;
- mouse wheel, touchpad, scrollbar drag, or another identified user upward action disables follow;
- a scrollbar value change caused by content growth, virtualization, or a deferred programmatic scroll does not disable follow;
- while follow is enabled, each processed preview batch requests one bottom scroll after layout, rather than one request per event;
- final/milestone layout stabilization keeps the existing multi-frame bottom correction.

The implementation must not infer user intent only from `value < max_value - page`, because `max_value` can grow before the deferred scroll catches up.

### 5. Observe latency at the boundaries that failed

Add structured diagnostics for provider first chunk, first preview event publication, first client receipt, batch count/`has_more`, UI drain duration, and preview commit/discard. Tests assert bounded behavior without depending on production wall-clock timing.

## Risks / Trade-offs

- [Users briefly see text from a transaction that later fails] → Mark it provisional and deterministically discard or annotate it on rollback.
- [Immediate preview ordering differs from later transactional event ordering] → Preserve sequence within each delivery class, carry frame/message identity, and use lifecycle boundaries rather than replaying previews.
- [Append-only deltas overflow the 500-event process buffer] → Keep provider-side coalescing intervals, bound client lag, immediately drain backlog, and test overflow behavior; do not silently advance past missing fragments.
- [Rapid polling increases request overhead] → Immediate repoll occurs only while `has_more`; idle cadence remains configurable.
- [Frame budgeting increases time to render a very large backlog] → Favor editor responsiveness and show the newest output progressively; coalesce compatible snapshots before enqueue.
- [A crash leaves no discard marker] → Session history is authoritative on restart; unmatched process-local previews are never restored as committed history.

## Migration Plan

1. Add preview metadata and bounded read support in a backward-compatible response shape.
2. Implement backend event classification and lifecycle boundaries behind the existing event-stream setting.
3. Update the Godot client to understand `has_more` and preview boundaries while tolerating older responses that omit them.
4. Add frame-budgeted draining and explicit follow-state input handling.
5. Run backend unit/integration tests and headless Godot chat-panel regressions, then reproduce against a long tool-result continuation.

Rollback can restore the previous frontend and backend together. No persisted Session or artifact migration is required; older clients ignore unknown event types and absent new fields retain their defaults.

## Open Questions

- Choose the initial server batch limit and UI per-frame budget from automated stress results; start with 50 events and a small millisecond budget unless profiling indicates otherwise.
- Decide whether discarded preview text should disappear or remain as a clearly marked failed attempt; tests should encode the selected UX consistently.
