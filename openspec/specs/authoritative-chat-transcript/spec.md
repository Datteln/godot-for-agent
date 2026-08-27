# Authoritative Chat Transcript Spec

## Purpose

The service maintains a versioned, authoritative visible transcript per chat session, which is the sole source for history responses and visible live patches.

## Requirements

### Requirement: The service maintains an authoritative visible transcript
The service SHALL create and persist a versioned visible transcript for every new chat session. Each visible transcript entry MUST contain a stable `entry_id`, immutable display `ordinal`, typed `kind`, `state`, monotonic `revision`, and typed payload. The transcript MUST be the sole source for new-session history presentation. The service MUST maintain model conversation context independently from this visible transcript; compacting or excluding an entry from a later LLM request MUST NOT delete, alter, or omit the entry from transcript history.

#### Scenario: Persisting a visible workflow
- **WHEN** a user message, Thought, assistant answer, tool result, approval, task progress, verification result, or error becomes visible
- **THEN** the service records a typed transcript entry or revision before it can be returned in history or emitted as a visible live patch

#### Scenario: Model context compaction preserves visible history
- **WHEN** an older turn or completed tool group is consolidated out of model context
- **THEN** history responses continue to contain its user-visible transcript entries in their original order and revisions

### Requirement: Assistant streaming and completion share one entry
The service SHALL create one assistant transcript entry for one assistant response identity. Stream updates and the final completion MUST update that entry with increasing revision and MUST NOT create a separate final-body entry.

#### Scenario: Completing a streamed answer
- **WHEN** an assistant emits text deltas and later completes
- **THEN** history contains exactly one complete assistant entry with the final body and the same entry identity used by the live updates

### Requirement: A successful assistant final has non-empty body text
The service MUST NOT finish a no-tool model response as a successful assistant final when its body is empty. It MUST first consume the original response stream to completion and wait for its `content` or tool calls, even if the reasoning token count reaches a configured thinking budget. Only after that completed stream has neither body nor tool calls may the service make exactly one bounded recovery request with thinking disabled and no tools, instructing the model to return only the final user-facing answer. If recovery produces non-empty body text, it SHALL complete one assistant entry; otherwise the service SHALL record a typed error and MUST NOT create a successful empty assistant entry or empty final response.

#### Scenario: Model stops after reasoning only
- **WHEN** a model response has no tool calls, non-empty reasoning, and empty content after its stream has ended
- **THEN** the service attempts one no-thinking final-answer recovery and either persists its non-empty assistant body or persists an error explaining that no final answer was produced

### Requirement: User-visible Thought is a durable transcript entry
The service SHALL persist each explicitly user-visible Thought as one `kind=thought` transcript entry. Its typed payload MUST contain the accumulated Thought content, current `token_count`, a start time, and terminal `duration_seconds`. After the original model response stream ends, the Thought enters `complete`, including when its reasoning reached a configured token budget. `complete` is terminal: the Writer MUST reject any later delta that would return the entry to `thinking` or change its payload/revision. Thought content and its summary fields MUST be included in history snapshots and revision-aware WebSocket patches. Internal reasoning not explicitly designated user-visible MUST NOT be written as a Thought entry.

#### Scenario: Completing a visible Thought before an answer
- **WHEN** a turn emits a user-visible Thought with content/token updates and later completes before emitting assistant text
- **THEN** history contains one complete Thought entry with its final content, token count, and duration, followed by the independently identified assistant entry

#### Scenario: Late final reasoning delta after completion
- **WHEN** a Thought has reached `complete` and a cumulative reasoning delta for the same entry arrives later
- **THEN** the persisted entry, its revision, and its complete state remain unchanged

#### Scenario: Thinking budget boundary before a final answer
- **WHEN** a reasoning token count reaches the configured thinking budget but the original response stream remains open
- **THEN** the service continues consuming that stream and waits for content or tool calls; it does not yet complete Thought or issue a recovery request

### Requirement: History returns an atomic transcript snapshot
The history API SHALL return a transcript `version`, session ID, ordered entries, and `upto_event_seq`. The returned entries MUST represent all visible transcript changes at or before `upto_event_seq`.

#### Scenario: Loading during an active session
- **WHEN** a client requests history while a session has already emitted visible patches
- **THEN** the snapshot provides one ordered transcript state and a cursor from which later patches can be resumed without replaying an entry already represented by the snapshot

### Requirement: Legacy history conversion is stable and non-inventive
For a session without a persisted transcript, the service SHALL perform at most one compatibility conversion from durable legacy records and persist the converted result. It MUST NOT fabricate a visible entry from unavailable or ambiguous legacy data.

#### Scenario: Reloading a converted legacy session
- **WHEN** a legacy session is loaded a second time
- **THEN** the service returns the previously persisted converted transcript instead of recomputing a different block sequence

### Requirement: Durable visible entries exclude unbounded tool source material
The service SHALL include every persisted user-visible transcript entry and its latest valid revision in the session history snapshot through `upto_event_seq`, regardless of whether the entry is a Thought, assistant message, tool activity, approval, verification result, progress entry, or error. A complete ClassDB/API document returned by `read_class_docs`, an unbounded search-match line, or raw runtime log content MUST NOT be persisted in a session frame, transcript entry, or history snapshot; the visible tool activity MAY retain only bounded class/query metadata and normalized search excerpts.

#### Scenario: Recovering after a ClassInfo entry
- **WHEN** the service has persisted a Thought, bootstrap approval, and tool activity after a `read_class_docs` result but the client has not accepted their live patches
- **THEN** the history snapshot through its `upto_event_seq` contains those ordered entries with their latest valid revisions and no complete ClassDB/API document

#### Scenario: Snapshot excludes later concurrent entries
- **WHEN** a later visible entry is persisted after the history response selects `upto_event_seq`
- **THEN** the response contains a consistent transcript only through that cursor and the later entry remains available through resume from that cursor

#### Scenario: Historical Grep result does not revive a service log payload
- **WHEN** a code search previously matched a runtime log line whose original text exceeded the search excerpt limit
- **THEN** the history snapshot retains only the path, line number, bounded excerpt, truncation indicator, and counts; it contains no remainder of that log line
