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

The Store retains all loaded entries, while `TranscriptViewport` mounts only a contiguous ordinal window plus configurable overscan. Estimated heights for unmounted ranges are represented by top and bottom spacer controls. The mounted root count has a configurable hard maximum; reaching it evicts the farthest non-pinned controls first.

### 2. Measurements are keyed by entry ID and content revision

The viewport caches measured height by `(entry_id, revision, width bucket)`. A changed revision invalidates only that entry's height. Before applying a reflow, the viewport records the first visible entry ID and intra-entry offset, then restores that anchor after spacers and measurements update. This is preferred to absolute scrollbar preservation because content above the viewport can legitimately grow.

### 3. Follow mode is explicit reader intent

The viewport enters follow mode only at/near the tail or after an explicit “return to latest” action. Manual upward scroll exits follow mode. New tail entries and stream revisions scroll only while follow mode is enabled; otherwise the viewport keeps its anchor and exposes the return action.

### 4. History is reverse-paginated by stable ordinal cursor

The history transcript API accepts a `before_ordinal`/cursor and `limit`, returning an ordered page, the cursor for older entries and the same entry identities. The Store merges older pages by ID and ordinal. Initial load starts near the newest page; reaching the leading threshold requests an older page without rebuilding already mounted roots.

### 5. Control pooling is opt-in and reset-safe

The first implementation may free off-window roots after recording measurements. If pooling is used, a pooled renderer root must clear callbacks, text selection, expansion, approval actions and entry ID before binding another entry. Correctness takes priority over reuse rate.

## Risks / Trade-offs

- [Incorrect estimates cause scroll jumps] → continuously replace estimates with measured heights and restore by entry anchor.
- [Mounted approval is actionable but evicted] → action state lives in Store; remount reconstructs the same enabled/disabled state.
- [Pagination overlaps entries] → merge by stable ID and ordinal, reject conflicting payload revisions.
- [Pooling retains callbacks or rich text resources] → use explicit reset contract and begin with freeing roots if validation finds leaks.
- [Tail streaming forces readers down] → follow requires explicit tail proximity, never inferred from content updates alone.

## Migration Plan

1. Add transcript page cursor/limit contract and Store support for merging older pages.
2. Introduce a viewport window with spacers while retaining existing typed renderer roots.
3. Add measurement cache and anchor restoration before enabling root eviction.
4. Enable bounded mount count, follow mode and return-to-latest control.
5. Test long-task load, stream updates, pagination, copy/remount and resource bounds under the editor.
