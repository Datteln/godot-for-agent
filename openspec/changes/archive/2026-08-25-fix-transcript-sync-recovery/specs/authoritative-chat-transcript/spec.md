## ADDED Requirements

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
