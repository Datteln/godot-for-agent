## Context

The service persists Thoughts and publishes transcript patches over a resumable WebSocket. The Godot client currently batches replaceable Thought revisions before visual work. In the observed failure, the service kept publishing one Thought while the client retained only earlier entries; generic event activity nevertheless prolonged the HTTP wait.

The user requires every thinking token actually emitted by the model provider to appear at the frontend without batching or filtering. The design must still avoid cancelling a healthy turn merely because the live delivery path needs recovery.

## Goals / Non-Goals

**Goals:**

- Guarantee front-end presentation of every model-emitted thinking token through live patches or snapshot recovery.
- Detect a Thought-specific delivery/projection stall while an active turn continues to publish that Thought.
- Make every loss, rejection and recovery diagnosable without logging Thought text or prompts.
- Deliver and render thinking updates token-by-token without client or service coalescing.

**Non-Goals:**

- Generating or inferring thinking content that the model provider did not return.
- Showing a user-facing delivery-failure or recovery notice.
- Changing tool approval, map-authoring, or model-selection behavior.

## Decisions

### Track a full-Thought delivery watermark

The service will expose metadata for the latest published Thought token (session, entry ID, revision and sequence). The client will separately record received, projected and rendered watermarks. This identifies whether failure is publication, transport, projection or renderer ownership without putting full Thought content into diagnostics.

Alternative: use any event or heartbeat as proof of visible progress. Rejected because tool and keepalive events can continue while a Thought is absent from the UI.

### Recover from stalled Thought delivery

While a turn is active, if the service's Thought watermark advances but the client does not advance its projected/rendered watermark within a bounded interval, the client will reconnect from the contiguous cursor and hydrate an atomic snapshot if replay cannot close the gap. It will retain the active request when recovery confirms the same turn. Recovery is silent to the user.

Alternative: reset the full session immediately. Rejected because it loses local state and unnecessarily interrupts healthy work.

### Make every token and terminal Thought visibility a postcondition

Every token and terminal Thought revision must be present in the canonical Store and route to the viewport after live delivery or recovery. If this cannot be achieved, the client records a typed diagnostic and continues silent automatic recovery rather than displaying a user-facing notice.

### Test the entire path token by token

Integration tests will simulate a long token-by-token Thought, a dropped live patch, a projector rejection and a history-based recovery. Assertions target exact token/revision order and watermarks, not complete prompts.

## Risks / Trade-offs

- [High event and render volume] → Preserve token order and correctness as the priority; measure and diagnose the resulting load rather than coalescing tokens.
- [Extra recovery requests during busy streams] → Require a monotonic Thought watermark and one bounded recovery attempt per stagnant watermark.
- [False stalls while the user reviews older content] → Measure Store projection and renderer routing, not auto-scroll position or follow mode.
- [Provider does not return thinking tokens] → Display only the thinking content that the provider actually supplies; do not fabricate missing content.
- [Snapshot recovery overwrites newer local state] → Reuse existing session/generation validation and `upto_event_seq` cursor rules.

## Migration Plan

1. Add compatible watermark metadata and client diagnostics behind existing transcript/event contracts.
2. Ship server and plugin together; older clients ignore additional metadata and retain current resume behavior.
3. Enable stalled-Thought recovery after integration coverage verifies exact token replay and snapshot paths.
4. Roll back by disabling the recovery trigger while preserving diagnostics and existing resumable delivery.

## Open Questions

- The bounded interval should be configurable; its default needs measurement against slow editor frames and provider cadence.
- Confirm whether watermark metadata belongs in a dedicated transport event or redacted fields on existing diagnostics/keepalives.
