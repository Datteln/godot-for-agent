## ADDED Requirements

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
