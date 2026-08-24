# Chat Transcript Navigation Spec

## Purpose

The Godot client virtualizes the transcript viewport over the session-scoped Store so it mounts only a bounded window of durable entries, preserves a stable visual anchor across reflows, and keeps user intent (follow mode, interaction semantics, outcome labels) observable across streaming, pagination, and remounting.

## Requirements

### Requirement: Mounted chat controls are bounded
The client SHALL retain transcript entry state independently of UI controls and SHALL mount only a bounded, contiguous ordinal window with configurable overscan. It MUST expose spacer height for unloaded visual ranges and MUST NOT permanently create one Godot control per loaded entry. It SHALL consume each renderer's initial preview budget and record mounted rich-text character diagnostics. An explicitly requested complete-content control is permitted for its mounted entry, but MUST be freed on eviction so its next mount returns to preview.

#### Scenario: Opening a long session
- **WHEN** a loaded transcript contains more entries than the configured mounted-window limit
- **THEN** the viewport mounts no more than that limit plus configured fixed infrastructure controls while the Store retains every loaded entry

### Requirement: Virtualization preserves a visual anchor
Before a window, measurement, or revision update changes content above the viewport, the client SHALL record the visible anchor entry ID and intra-entry offset. After reflow it MUST restore that anchor when the entry remains loaded. If unavailable, it MUST use the nearest loaded successor, then predecessor, then the best estimated position.

#### Scenario: An earlier streamed entry grows
- **WHEN** a revised entry above the reader becomes taller while follow mode is disabled
- **THEN** the reader continues viewing the same anchored content rather than jumping by the height difference

### Requirement: Follow mode expresses user intent
The viewport SHALL follow new tail entries only while follow mode is enabled. Manual scrolling away from the tail, active text selection, copying, or expanded-detail interaction MUST disable/suppress follow mode, and a return-to-latest action MUST re-enable it and scroll to the current tail.

#### Scenario: Reviewing old tool output during streaming
- **WHEN** the user scrolls upward while a task emits new assistant or tool entries
- **THEN** the viewport remains anchored to the reviewed content and exposes a return-to-latest action

### Requirement: Heights are revision-aware
The viewport MUST cache or estimate entry heights using entry ID, revision, effective width, presentation epoch and preview/complete content mode. Theme, font or UI-scale changes MUST advance that epoch. A transcript revision MUST invalidate only that entry's cached measurement and MUST NOT invalidate unrelated entries solely because their ordinal is nearby.

#### Scenario: Markdown completion changes one row height
- **WHEN** an assistant entry transitions from streaming text to completed Markdown
- **THEN** the viewport remeasures that entry and retains measurements for unchanged entries

### Requirement: Live patch outcomes are observable across navigation state
For every live `transcript_patch`, the navigation/projection path SHALL either apply the patch and route its current entry to the viewport, apply it with no visible revision change, or emit a redacted structured diagnostic explaining rejection. Diagnostics MUST distinguish at least projector-not-ready, generation mismatch, session mismatch, malformed payload, duplicate/non-newer revision, and renderer rejection. They MUST include event identity and sequence where available, but MUST NOT include secrets, complete prompts, or unbounded model text.

#### Scenario: Patch arrives while history hydration is incomplete
- **WHEN** a live assistant patch arrives before the active session/generation has reached READY
- **THEN** the client does not render it against stale navigation state and records a diagnostic identifying the hydration-state rejection

#### Scenario: Accepted patch updates the mounted viewport entry
- **WHEN** a valid newer same-session patch reaches a READY viewport
- **THEN** the Store accepts it, the viewport updates or mounts the corresponding entry, and no rejection diagnostic is emitted

### Requirement: Earlier history pages merge without duplicates
The history API SHALL support fetching an older ordered transcript page through a stable cursor or `before_ordinal` and limit. Each page MUST include the active session ID, transcript version, `upto_event_seq`, a next cursor and `has_more`. Atomic snapshot hydration/resynchronization SHALL replace the Store, whereas an older page received after the Store is READY for the same session and generation SHALL merge only its range. The client MUST deduplicate in-flight cursors, merge unknown entries by ordinal, accept only higher revisions for known IDs, and reject a page that would regress a terminal state.

#### Scenario: Reaching the oldest mounted threshold
- **WHEN** the reader scrolls near the leading edge of the earliest loaded page and older entries exist
- **THEN** the client fetches and merges the older page without recreating a second copy of entries already loaded

#### Scenario: Older page arrives after a newer live patch
- **WHEN** an older-page response contains a lower revision of an entry already updated by a WebSocket patch
- **THEN** the Store retains the newer live revision and merges only entries not already known

### Requirement: Remounted entries retain their interaction semantics
When an entry leaves and later re-enters the virtual window, the client SHALL recreate its renderer from Store state with the same readable content, copy behavior, resolved approval text state, tool state and error context. An oversized entry previously displayed in complete mode SHALL remount in preview mode while retaining its full canonical Store content.

#### Scenario: Copying a remounted assistant entry
- **WHEN** an assistant entry is evicted from the mounted window, then remounted after scrolling back
- **THEN** its copy action returns the same canonical text as before eviction

### Requirement: Transient notices are not virtual transcript rows
The viewport SHALL manage only durable typed transcript entries. Waiting, command-running, and comparable local notices MUST remain outside viewport ordering, spacers, measurement, anchoring, pagination, and remounting. Their host MAY directly discard their controls when the local state changes.

#### Scenario: Replacing a transcript snapshot while a command notice exists
- **WHEN** a transient command-running notice is present and history hydration replaces the transcript
- **THEN** the viewport renders only durable transcript rows and the notice is discarded rather than recreated

### Requirement: Execution-preview control handoff is lifetime-safe
When an inline tool-confirmation host supplies an execution-before preview for a later `approval` or `tool_activity` transcript entry, the client SHALL establish exactly one current owner for that `Control` before the confirmation host is disposed. The preview cache MUST NOT retain a reference to a control owned by a host scheduled for disposal. A renderer SHALL consume a transferred preview at most once; if it is unavailable, it MUST render from durable entry data without accessing a freed instance. Reset, eviction, interruption, session switch, and confirmation replacement MUST free only controls owned by their respective host or renderer.

#### Scenario: Approval patch follows confirmation disposal
- **WHEN** the user applies or rejects a workflow tool call and the confirmation host is cleared before the corresponding approval patch is rendered
- **THEN** the preview is either transferred safely to the approval renderer or omitted in favor of the durable approval summary, and the update produces no access to a previously freed Godot instance

#### Scenario: Delayed approval patch updates an existing entry
- **WHEN** an approval entry is already mounted and a newer patch later consumes a registered execution-before preview
- **THEN** the renderer accepts only a live transferred preview, consumes it once, and otherwise retains a valid summary-only entry without a renderer error

### Requirement: Tool confirmation and execution outcomes remain distinct
The client SHALL use `rejected` only when the user explicitly rejects a pending call or leaves that call unselected when submitting a mixed batch. A call selected for execution SHALL preserve the executor's returned status: validation or execution failure SHALL be `error`, not `rejected`. The confirmation UI MUST use distinct labels for per-call selection, batch execution, and explicit rejection. Durable approval and tool-activity transcript entries MUST present approved, rejected, and error outcomes distinctly, including a bounded error code or summary for an error. Structured diagnostics MUST record a redacted decision source (`explicit_reject`, `unselected`, or `execute`) and outcome status.

#### Scenario: An allowed call fails validation
- **WHEN** the user selects a pending call and submits the batch execution action, but the frontend tool rejects its input as invalid
- **THEN** the model and transcript receive `status=error` with the bounded validation reason, and the UI does not label the call as user-rejected

#### Scenario: User explicitly rejects a call
- **WHEN** the user presses the explicit rejection action, or leaves one call unselected in a mixed batch
- **THEN** that call receives `status=rejected` and the transcript labels it as user-rejected

### Requirement: Live stream rendering has bounded frame work
The transcript navigation path SHALL batch live replaceable revisions so that a projection window performs at most one viewport update and one automatic follow-mode scroll request for a given set of changed entries. It MUST preserve final entry state, viewport anchor behavior, and return-to-latest semantics while avoiding one synchronous reflow per received stream packet.

#### Scenario: Long response while following the tail
- **WHEN** a long assistant response emits many realtime updates while follow mode is enabled
- **THEN** the viewport displays advancing recent content with bounded per-window rendering work and does not enqueue one independent bottom-scroll operation per update

#### Scenario: Reader is reviewing earlier content
- **WHEN** a long assistant response emits many realtime updates while follow mode is disabled
- **THEN** batched updates preserve the reader's existing anchor and expose the current return-to-latest affordance