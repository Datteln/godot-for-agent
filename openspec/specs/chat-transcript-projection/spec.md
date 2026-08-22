# Chat Transcript Projection Spec

## Purpose

The Godot client projects server history snapshots and WebSocket transcript patches into one canonical session-scoped Store that drives rendered chat controls.

## Requirements

### Requirement: The client has one canonical transcript state
The Godot client SHALL render chat content only from a session-scoped transcript Store. HTTP command responses, WebSocket events, and history responses MUST be projected into that Store before affecting rendered chat controls.

#### Scenario: Receiving an HTTP final response
- **WHEN** an HTTP command response confirms an assistant final result
- **THEN** the client does not directly append or replace an assistant message and waits for the corresponding transcript patch or snapshot

### Requirement: Snapshot hydration atomically replaces rendered state
The client SHALL validate and atomically replace the current session transcript from a valid history snapshot. It MUST preserve only local optimistic entries that are explicitly matched by `client_message_id`.

#### Scenario: Refreshing a loaded session
- **WHEN** a session history snapshot is reloaded after the client already rendered entries
- **THEN** the Store contains exactly one ordered copy of each snapshot entry rather than an appended second copy

### Requirement: Patches are idempotent and revision-aware
The client MUST de-duplicate received patches by immutable event ID and MUST reject a patch whose transcript entry revision is not newer than the revision already accepted for that entry.

#### Scenario: Duplicate replay after reconnect
- **WHEN** the WebSocket replays a previously accepted entry patch
- **THEN** the transcript and rendered controls remain unchanged

#### Scenario: Late assistant update
- **WHEN** a lower-revision assistant patch arrives after a higher-revision completion patch
- **THEN** the completed assistant entry remains unchanged

### Requirement: Renderer choice depends only on entry kind
The client SHALL select a renderer using the transcript entry `kind` and typed payload. Renderers MUST NOT infer message identity, Thought state, tool state, or deduplication from display text.

#### Scenario: Two identical assistant answers
- **WHEN** two complete assistant entries have identical body text but different entry IDs
- **THEN** the client renders both entries in ordinal order

### Requirement: Thought projection preserves expandable persisted content
The client SHALL project `kind=thought` entries by entry ID and revision, retaining the typed content, token count, state, and completed duration received from snapshots or patches. It MUST NOT derive Thought state or content from a text prefix, and it MUST replace stale Thought revisions using the same revision rules as other entries.

#### Scenario: Reconnecting after a completed Thought
- **WHEN** a completed Thought is present in a history snapshot after the client reconnects
- **THEN** the Store contains its final content and duration so its renderer can recreate the completed expandable Thought card

#### Scenario: Receiving a late Thought delta after a terminal patch
- **WHEN** the Store contains a `complete` Thought and receives a higher-revision patch that changes it to `thinking`
- **THEN** the Projector rejects that invalid terminal-state regression and leaves the terminal entry unchanged

### Requirement: Hydration rejects stale sessions and generations
The client MUST associate each hydration with a session ID and generation. It MUST reject a snapshot or patch that does not match the active session and generation.

#### Scenario: Delayed response after switching sessions
- **WHEN** the previous session's history response arrives after the user switches to another session
- **THEN** the active transcript remains unchanged