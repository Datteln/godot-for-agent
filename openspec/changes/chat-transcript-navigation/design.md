## Context

The transcript Store can hold a long logical sequence cheaply, but Godot `Control` and `RichTextLabel` nodes are expensive to keep mounted. Long agent tasks can therefore cause layout churn, memory growth and editor instability even when the underlying transcript data is correct. Navigation must bound mounted UI without losing entry identity or user position.

This change depends on the authoritative transcript Store and typed renderers. It owns the view window only; it never changes transcript entries or reinterprets their content.

## Goals / Non-Goals

**Goals:**

- Bound mounted transcript controls independently of total session length.
- Preserve a reader's visual anchor while rows above it change height or are mounted/unmounted.
- Follow new tail entries only when the reader remains near the latest content.
- Load earlier transcript pages without duplicate IDs or losing the ability to remount/copy entries.

**Non-Goals:**

- Modify entry IDs, revisions, visible semantics or WebSocket sequence behavior.
- Replace typed entry renderers or Markdown parsing.
- Guarantee exact pixel measurement before a row has ever been mounted.

## Decisions

### 1. Windowed mounting with top/bottom spacers

The Store retains all loaded entries, while `TranscriptViewport` mounts only a contiguous ordinal window plus configurable overscan. Estimated heights for unmounted ranges are represented by top and bottom spacer controls. The mounted root count has a configurable hard maximum; reaching it evicts the farthest non-pinned controls first. Every long-content renderer mounts only its initial preview within a shared budget. A user may explicitly request complete rendering for one entry; the viewport records its actual measured height and mounted-character diagnostics, then frees that complete control on eviction so a remount returns to preview. The transcript viewport manages durable entries only; local transient notices are outside it and are never counted, measured, anchored, paged, or remounted.

### 2. Measurements are keyed by entry ID and content revision

The viewport caches measured height by `(entry_id, revision, width bucket, presentation_epoch, content_mode)`, where `content_mode` is preview or user-requested complete. A changed revision invalidates only that entry's height; a theme, font, UI-scale or effective-width change advances `presentation_epoch`. Before applying a reflow, the viewport records the first visible entry ID and intra-entry offset, then restores that anchor after spacers and measurements update. If that entry is no longer loaded, the fallback is the nearest loaded successor, then predecessor, then the estimated absolute position. This is preferred to absolute scrollbar preservation because content above the viewport can legitimately grow.

### 3. Follow mode is explicit reader intent

The viewport enters follow mode only at/near the tail or after an explicit “return to latest” action. Manual upward scroll exits follow mode. New tail entries and stream revisions scroll only while follow mode is enabled; otherwise the viewport keeps its anchor and exposes the return action. Active text selection, copying, or an expanded detail interaction suppresses automatic tail movement until the user explicitly returns to latest.

### 4. History is reverse-paginated by stable ordinal cursor

The history transcript API accepts a `before_ordinal`/cursor and `limit`, returning an ordered page, `session_id`, transcript `version`, `upto_event_seq`, `next_before_ordinal`/cursor and `has_more`. The initial tail snapshot remains the only operation that atomically replaces a Store. Once it is `READY` for the same session and generation, an older-page response only merges its range: unknown IDs are inserted by ordinal, known IDs are replaced only by a higher revision, and no page may regress a terminal Thought state. Overlapping or in-flight page requests are deduplicated by cursor; failed cursors expose retry rather than repeatedly requesting. Initial load starts near the newest page; reaching the leading threshold requests an older page without rebuilding already mounted roots.

### 5. Control pooling is opt-in and reset-safe

The first implementation may free off-window roots after recording measurements. `TranscriptViewport` alone selects roots for eviction; it invokes the renderer `reset(root)` before free or pool return. If pooling is used, a pooled renderer root must clear callbacks, text selection, expansion, approval actions and entry ID before binding another entry. Correctness takes priority over reuse rate.

### 6. Patch rejection is observable at navigation boundaries

Navigation introduces hydration, page merging and viewport lifecycle boundaries at which a live `transcript_patch` can legitimately be deferred or rejected. Every such outcome must be recorded as a redacted structured diagnostic: applied and visible, applied without visible revision change, or rejected with a reason such as projector-not-ready, generation/session mismatch, malformed payload, duplicate/non-newer revision or renderer rejection. The record includes event identity, sequence, session and generation where available, but never complete prompts, secrets or unbounded model text.

## Risks / Trade-offs

- [Incorrect estimates cause scroll jumps] → continuously replace estimates with measured heights and restore by entry anchor.
- [Mounted approval is actionable but evicted] → action state lives in Store; remount reconstructs the same enabled/disabled state.
- [Pagination races a live patch or hydration] → distinguish atomic initial replacement from READY-state older-page merge; deduplicate by cursor and accept only higher revisions for known IDs.
- [One entry is huge despite a bounded node count] → it initially mounts only a budgeted preview; complete rendering is an explicit user action, measured for anchoring and discarded on eviction.
- [Transient notices distort history layout or reappear after reload] → keep them outside the viewport and discard their nodes on state transition, hydration, resync, or session switch.
- [Pooling retains callbacks or rich text resources] → use explicit reset contract and begin with freeing roots if validation finds leaks.
- [Tail streaming forces readers down] → follow requires explicit tail proximity, never inferred from content updates alone.
- [Server emitted a stream but navigation discarded it silently] → record a redacted, structured outcome for every live patch at hydration/page/window boundaries.

## Migration Plan

1. Add transcript page cursor/limit/watermark contract and READY-state Store support for merging older pages.
2. Introduce a viewport window with spacers while retaining existing typed renderer roots.
3. Add measurement cache and anchor restoration before enabling root eviction.
4. Enable bounded mount count, preview/full-content diagnostics, discardable transient host, follow mode and return-to-latest control.
5. Test long-task load, stream updates, pagination, copy/remount and resource bounds under the editor.
6. Add patch-outcome diagnostics and fixtures showing that server-generated assistant streams cannot silently disappear while navigation changes state.
