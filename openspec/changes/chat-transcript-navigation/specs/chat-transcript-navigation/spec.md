## ADDED Requirements

### Requirement: Mounted chat controls are bounded
The client SHALL retain transcript entry state independently of UI controls and SHALL mount only a bounded, contiguous ordinal window with configurable overscan. It MUST expose spacer height for unloaded visual ranges and MUST NOT permanently create one Godot control per loaded entry.

#### Scenario: Opening a long session
- **WHEN** a loaded transcript contains more entries than the configured mounted-window limit
- **THEN** the viewport mounts no more than that limit plus configured fixed infrastructure controls while the Store retains every loaded entry

### Requirement: Virtualization preserves a visual anchor
Before a window, measurement, or revision update changes content above the viewport, the client SHALL record the visible anchor entry ID and intra-entry offset. After reflow it MUST restore that anchor when the entry remains loaded.

#### Scenario: An earlier streamed entry grows
- **WHEN** a revised entry above the reader becomes taller while follow mode is disabled
- **THEN** the reader continues viewing the same anchored content rather than jumping by the height difference

### Requirement: Follow mode expresses user intent
The viewport SHALL follow new tail entries only while follow mode is enabled. Manual scrolling away from the tail MUST disable follow mode, and a return-to-latest action MUST re-enable it and scroll to the current tail.

#### Scenario: Reviewing old tool output during streaming
- **WHEN** the user scrolls upward while a task emits new assistant or tool entries
- **THEN** the viewport remains anchored to the reviewed content and exposes a return-to-latest action

### Requirement: Heights are revision-aware
The viewport MUST cache or estimate entry heights using entry ID, revision and effective width. A transcript revision MUST invalidate only that entry's cached measurement and MUST NOT invalidate unrelated entries solely because their ordinal is nearby.

#### Scenario: Markdown completion changes one row height
- **WHEN** an assistant entry transitions from streaming text to completed Markdown
- **THEN** the viewport remeasures that entry and retains measurements for unchanged entries

### Requirement: Earlier history pages merge without duplicates
The history API SHALL support fetching an older ordered transcript page through a stable cursor or `before_ordinal` and limit. The client MUST merge pages by entry ID and ordinal, preserving existing entries and rejecting duplicate insertion.

#### Scenario: Reaching the oldest mounted threshold
- **WHEN** the reader scrolls near the leading edge of the earliest loaded page and older entries exist
- **THEN** the client fetches and merges the older page without recreating a second copy of entries already loaded

### Requirement: Remounted entries retain their interaction semantics
When an entry leaves and later re-enters the virtual window, the client SHALL recreate its renderer from Store state with the same readable content, copy behavior, resolved approval state, tool state and error context.

#### Scenario: Copying a remounted assistant entry
- **WHEN** an assistant entry is evicted from the mounted window, then remounted after scrolling back
- **THEN** its copy action returns the same canonical text as before eviction
