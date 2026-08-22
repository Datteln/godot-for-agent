## ADDED Requirements

### Requirement: Renderers consume only typed transcript entries
The client SHALL choose a chat renderer solely from a transcript entry `kind` and typed payload. A renderer MUST NOT inspect raw transport messages, role/text append calls, Thought prefixes, or text fingerprints to determine semantics or identity.

#### Scenario: Rendering equal text in different entries
- **WHEN** two assistant entries have equal body text and different entry IDs
- **THEN** the renderer displays two independently addressable message controls in ordinal order

### Requirement: Entry revisions update one mounted control
The rendering host SHALL associate a mounted root control with one entry ID. A newer revision for that ID MUST update the existing control and MUST NOT append another root control; a non-newer revision MUST leave the control unchanged.

#### Scenario: Streaming assistant completion
- **WHEN** an assistant entry receives text revisions followed by a complete revision
- **THEN** one mounted message control displays the final Markdown body and complete state

#### Scenario: Completion without content change keeps the mounted control
- **WHEN** a streaming entry receives a complete revision whose body text equals the already-rendered text
- **THEN** the renderer keeps the same rich-text control without re-rendering it (no visible flicker), removes the streaming indicator, and preserves any active text selection

### Requirement: Transient system notices retain their trigger position
The client SHALL render each local transient system notice at the chat position that triggered it. It MUST NOT route all transient notices through a single container that is structurally placed after the entire durable transcript list. Transient notices remain outside the transcript Store, ordinal ordering, history snapshots, and WebSocket reconstruction, and may be discarded when their local state ends.

#### Scenario: Opening an empty historical session
- **WHEN** history hydration determines that the selected session has no durable entries
- **THEN** the local “no history” notice is displayed at that session's empty-history position, not after unrelated durable transcript content

#### Scenario: Sending a message while history is visible
- **WHEN** the client creates an optimistic user entry and shows a local “waiting for model” notice
- **THEN** the waiting notice appears immediately after that optimistic entry rather than below all pre-existing transcript entries

### Requirement: Historical tool summaries are bounded
The client SHALL preserve raw typed tool results in the transcript Store, but SHALL bound every synchronously rendered compact summary. In particular, a grep summary MUST render only a fixed limited number of matches and truncate each displayed path and match text; it MUST indicate omitted matches rather than concatenate their complete raw content into a `RichTextLabel` during hydration.

#### Scenario: Grep history contains model-request-sized log lines
- **WHEN** a persisted grep result contains several matches whose captured text includes a very large log line
- **THEN** history hydration renders a short bounded grep summary, identifies omitted matches, and does not create a rich-text body proportional to the raw result size

### Requirement: Thought summaries are expandable, copyable, and survive persisted reload
The renderer SHALL render a typed `kind=thought` entry as a collapsed, clickable summary. While its state is `thinking`, the summary MUST be `Thinking {token_count} Tokens >`; once its state is `complete`, it MUST be `Thought for {duration_seconds}s >`. Reaching a configured thinking-token budget MUST NOT create a distinct renderer state or change the completed summary. Clicking the summary SHALL toggle only that entry's persisted Thought content. A Thought entry's content SHALL be copyable like ordinary text entries once expanded (selection copy or the canonical full-copy action); the copied value MUST be the entry's persisted canonical Thought content without its summary, token count, duration, folding glyph, transcript metadata, or renderer markup. A valid revision update for a mounted entry MUST preserve its expansion state; a terminal Thought MUST NOT be replaced by a later `thinking` patch. A Thought restored from history or reconnect MUST expose the same persisted content and default to collapsed. The renderer MUST NOT create a Thought card from display text or raw transport data.

#### Scenario: Completing an expanded Thought
- **WHEN** a user expands a thinking Thought and a newer complete revision arrives
- **THEN** the same control remains expanded, displays the final persisted content, and changes its summary to `Thought for {duration_seconds}s >`

#### Scenario: Opening history with a completed Thought
- **WHEN** history contains a completed Thought entry
- **THEN** the renderer shows `Thought for {duration_seconds}s >` collapsed and reveals its persisted content when clicked

#### Scenario: Copying an expanded Thought
- **WHEN** a user expands a Thought entry and invokes copy (selection copy or canonical full-copy)
- **THEN** the clipboard receives its persisted canonical Thought content without the Thought summary or UI metadata

#### Scenario: Completing a Thought after a thinking-budget boundary
- **WHEN** a Thought reaches token count 1024, then the original model stream later ends
- **THEN** the renderer keeps the same entry and shows `Thought for {duration_seconds}s >` once its complete patch arrives

### Requirement: Markdown is safe, readable, and copyable
User and assistant text renderers SHALL render the supported Markdown subset consistently for history and live entries. Unsupported or malformed syntax MUST remain readable and MUST NOT create executable UI behavior. Each text entry SHALL expose a copy action that copies its canonical readable text without transcript metadata or renderer markup.

#### Scenario: Copying malformed Markdown after a live update
- **WHEN** a streamed assistant entry with malformed Markdown completes and the user invokes copy
- **THEN** the panel remains interactive and the clipboard receives readable canonical text

### Requirement: Oversized entries retain content and defer full rendering
The client SHALL retain every persisted entry's complete canonical content in the Store. It SHALL enforce one configurable initial per-entry display-character budget for all renderer kinds with long content. When content exceeds that budget, the renderer MUST initially show a readable preview and an explicit action to display the complete content. Only that explicit user action may create the complete rich-text control for that entry; copy SHALL return the complete canonical persisted content in either state. When a full-content entry leaves the virtual window or is reset, its complete control MUST be freed and its next mount MUST return to preview state.

#### Scenario: Opening an oversized persisted Thought
- **WHEN** history contains a Thought whose persisted content exceeds the configured display budget
- **THEN** its expanded card initially mounts a preview, offers a display-complete action, and its copy action returns the complete persisted Thought content

#### Scenario: Displaying then evicting oversized assistant content
- **WHEN** a user displays the complete content of an oversized assistant entry and later scrolls it outside the virtual window
- **THEN** the complete control is freed while the Store retains the full text, and a later remount starts with the preview

### Requirement: Tool records are compact and stateful
Tool renderers SHALL present concise operation, target, state and result information by default. Large structured arguments or raw results MUST remain collapsed unless explicitly expanded. A resolved tool record MUST retain its final state after history reload.

#### Scenario: Resolving a tool activity
- **WHEN** a tool entry changes from running to failed or complete
- **THEN** the same entry control updates to its final compact summary without creating an unkeyed duplicate card

### Requirement: Resolved approvals become plain text history
An approval renderer SHALL expose accept/reject controls only while its entry state is `pending` (the sole actionable state in the transcript contract); the confirmation host provides those controls while any approval entry is pending, and no entry in a resolved state SHALL display accept/reject controls. A resolved approval payload MUST persist `operation_summary`, `affected_paths`, and `resolution_summary`. After a user accepts, rejects, or submits the required result, the same entry ID MUST replace the approval card with one non-interactive one-line text node that expresses only the permission operation and outcome, for example `已确认：修改 res://player.gd`. When paths are numerous, the line MUST use a stable compact path list or count summary. A field that is objectively unavailable MUST be labelled as unavailable; the renderer MUST NOT infer it from UI state, prior raw transport packets, or display text. A user's supplemental typed input is a separate operation: it MUST be persisted and rendered as an independently ordered `kind=user` entry, never copied into the approval payload or permission-result line. A reloaded resolved approval MUST render the same one-line text-node form, with no card chrome, buttons, or expandable details.

#### Scenario: Reloading a completed approval
- **WHEN** history contains an approval entry whose decision is already resolved
- **THEN** the panel shows one one-line permission-result text node containing its operation summary/path and outcome, with no approval card or enabled confirmation control

#### Scenario: Confirming a file modification and later sending supplemental input
- **WHEN** a user confirms modification of `res://player.gd` and later sends `speed = 300`
- **THEN** the approval becomes the one-line text `已确认：修改 res://player.gd`, while `speed = 300` remains a separate user message entry after it

### Requirement: Transient notices are discardable local UI
Waiting, command-running, and comparable local transient notices MUST NOT be transcript entries and MUST NOT be included in Store ordering, viewport measurement, pagination, history snapshots, or WebSocket reconstruction. Their host MAY discard their nodes when the state changes, and it MUST NOT remount them after history hydration, resync, or virtual-window changes.

#### Scenario: Hydrating while a waiting notice exists
- **WHEN** a waiting notice is mounted and the panel hydrates a transcript snapshot
- **THEN** the notice node is discarded and only typed transcript entries are rendered from the snapshot

### Requirement: Errors retain actionable context
Error renderers SHALL show the failed operation or task context, a user-readable reason and the known modification status. A retry action MUST appear only when the typed payload explicitly declares the error retryable.

#### Scenario: Partial tool failure
- **WHEN** a failed tool result reports that files may already have changed
- **THEN** the error card warns of possible modification and does not present the task as successful
