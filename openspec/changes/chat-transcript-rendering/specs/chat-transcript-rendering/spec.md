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

### Requirement: Thought summaries are expandable, copyable, and survive persisted reload
The renderer SHALL render a typed `kind=thought` entry as a collapsed, clickable summary. While its state is `thinking`, the summary MUST be `Thinking {token_count} Tokens >`; once its state is `complete`, it MUST be `Thought for {duration_seconds}s >`. Reaching a configured thinking-token budget MUST NOT create a distinct renderer state or change the completed summary. Clicking the summary SHALL toggle only that entry's persisted Thought content. A Thought entry SHALL expose a copy action in both collapsed and expanded states; it MUST copy the entry's persisted canonical Thought content without its summary, token count, duration, folding glyph, transcript metadata, or renderer markup. A valid revision update for a mounted entry MUST preserve its expansion state; a terminal Thought MUST NOT be replaced by a later `thinking` patch. A Thought restored from history or reconnect MUST expose the same persisted content and default to collapsed. The renderer MUST NOT create a Thought card from display text or raw transport data.

#### Scenario: Completing an expanded Thought
- **WHEN** a user expands a thinking Thought and a newer complete revision arrives
- **THEN** the same control remains expanded, displays the final persisted content, and changes its summary to `Thought for {duration_seconds}s >`

#### Scenario: Opening history with a completed Thought
- **WHEN** history contains a completed Thought entry
- **THEN** the renderer shows `Thought for {duration_seconds}s >` collapsed and reveals its persisted content when clicked

#### Scenario: Copying a collapsed Thought
- **WHEN** a user invokes copy on a collapsed or expanded Thought entry
- **THEN** the clipboard receives its persisted canonical Thought content without the Thought summary or UI metadata

#### Scenario: Completing a Thought after a thinking-budget boundary
- **WHEN** a Thought reaches token count 1024, then the original model stream later ends
- **THEN** the renderer keeps the same entry and shows `Thought for {duration_seconds}s >` once its complete patch arrives

### Requirement: Markdown is safe, readable, and copyable
User and assistant text renderers SHALL render the supported Markdown subset consistently for history and live entries. Unsupported or malformed syntax MUST remain readable and MUST NOT create executable UI behavior. Each text entry SHALL expose a copy action that copies its canonical readable text without transcript metadata or renderer markup.

#### Scenario: Copying malformed Markdown after a live update
- **WHEN** a streamed assistant entry with malformed Markdown completes and the user invokes copy
- **THEN** the panel remains interactive and the clipboard receives readable canonical text

### Requirement: Tool records are compact and stateful
Tool renderers SHALL present concise operation, target, state and result information by default. Large structured arguments or raw results MUST remain collapsed unless explicitly expanded. A resolved tool record MUST retain its final state after history reload.

#### Scenario: Resolving a tool activity
- **WHEN** a tool entry changes from running to failed or complete
- **THEN** the same entry control updates to its final compact summary without creating an unkeyed duplicate card

### Requirement: Approvals become read-only history after resolution
An approval renderer SHALL expose accept/reject controls only while its entry state is `actionable`. A resolved, accepted, rejected, or reloaded approval MUST render as a non-actionable historical decision.

#### Scenario: Reloading a completed approval
- **WHEN** history contains an approval entry whose decision is already resolved
- **THEN** the panel shows the decision and no enabled confirmation control

### Requirement: Errors retain actionable context
Error renderers SHALL show the failed operation or task context, a user-readable reason and the known modification status. A retry action MUST appear only when the typed payload explicitly declares the error retryable.

#### Scenario: Partial tool failure
- **WHEN** a failed tool result reports that files may already have changed
- **THEN** the error card warns of possible modification and does not present the task as successful
