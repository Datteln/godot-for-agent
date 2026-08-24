## ADDED Requirements

### Requirement: Stream projection coalesces replaceable revisions before visual work
The client SHALL validate and acknowledge contiguous realtime event delivery independently from visual rendering. It MUST batch replaceable growing Thought and assistant revisions by entry ID, apply at most the newest eligible revision for an entry in one projection window, and apply non-streaming or terminal revisions no later than the next frame boundary.

#### Scenario: Several revisions arrive during one editor frame
- **WHEN** multiple valid streaming revisions for the same assistant entry arrive before the next projection window
- **THEN** the client acknowledges their contiguous delivery and renders only the newest revision while retaining the final Store state for that revision

#### Scenario: Terminal revision arrives with pending stream revisions
- **WHEN** a complete terminal revision for an entry arrives while earlier streaming revisions are pending visual projection
- **THEN** the client projects the terminal revision and does not later regress the entry to a streaming state

#### Scenario: Empty-answer recovery continues a logical Thought
- **WHEN** the service continues a visible Thought through a recovery response attempt
- **THEN** the client keeps the entry in its non-terminal Thinking presentation until it receives the single logical terminal revision, and renders the server-provided final duration without independently recomputing it
