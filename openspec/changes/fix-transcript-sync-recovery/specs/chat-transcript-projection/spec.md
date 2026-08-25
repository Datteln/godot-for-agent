## ADDED Requirements

### Requirement: Projection failures converge to authoritative transcript state
The client SHALL treat transcript patch decoding errors, entry/revision continuity failures, projector rejections, and renderer-routing failures as recoverable synchronization failures. It MUST record a typed redacted diagnostic, prevent the failed patch from advancing the contiguous cursor, and invoke the bounded resume/snapshot recovery path. After recovery, it MUST rebuild rendered controls solely from the canonical Store.

#### Scenario: Projector rejects a visible Thought revision
- **WHEN** a validly delivered Thought patch cannot be applied by the Projector
- **THEN** the client does not silently leave the viewport at the preceding entry and instead recovers the authoritative session state

### Requirement: Recovery renders every visible entry kind in order
After a successful replay or snapshot hydration, the client SHALL project and render each recovered user-visible entry in ordinal order using its typed `kind`. It MUST include Thought, assistant, tool activity, approval, progress, verification, and error entries, and MUST NOT rely on the HTTP command response to fill omitted entries.

#### Scenario: Recovering a mixed map-agent workflow
- **WHEN** a snapshot contains a ClassInfo result followed by a completed Thought, assistant bootstrap notice, approval cards, and tool activities
- **THEN** the viewport recreates all of those entries in ordinal order with the appropriate renderer for each kind

### Requirement: Rendered progress is not advanced before viewport acceptance
The client SHALL advance `projected_seq` only after the Projector has accepted the revision into the canonical Store, and SHALL advance `rendered_seq` and the transport `committed_seq` only after the renderer/viewport has accepted the corresponding entry revision for presentation. A streaming patch held by a projection batcher is neither projected, rendered, nor committed. A renderer rejection, an undrained projection batch, or a mismatch between Store and viewport watermarks MUST preserve the uncommitted event for replay and enter the bounded recovery path with a redacted stage-specific diagnostic.

#### Scenario: Streaming Thought remains in a projection batch
- **WHEN** the transport has acknowledged a Thought patch but the projection batcher has not applied it to the Store
- **THEN** `received_seq` may advance while `projected_seq` and `rendered_seq` do not, and the client does not report the transcript as healthy

### Requirement: Virtual viewport layout preserves visible transcript and transient notices
The client SHALL cache a virtual transcript entry height only after that entry has a stable, valid layout for the current content revision, width, and presentation mode. It MUST invalidate stale measurements when any of those inputs changes and MUST NOT let an invalid transient measurement create a spacer that moves the scroll target beyond the last actual entry. Local waiting, error, and report notices SHALL use a dedicated visible mount that is not separated from transcript content by a virtual spacer; they remain non-durable and outside transcript ordering.

#### Scenario: A streaming Thought receives an unstable first measurement
- **WHEN** a collapsed Thought is mounted while its rich-text child is still calculating its fit-content layout
- **THEN** the client does not cache the unstable height, remeasures after layout stabilizes, and scrolling to the bottom keeps the Thought and the following visible content reachable

#### Scenario: Error notice follows a virtualized entry
- **WHEN** an active turn receives an error after the viewport has mounted transcript entries and installed its bottom spacer
- **THEN** the error notice appears immediately after the last actual visible entry and a bottom-scroll request reveals it without traversing a spacer

### Requirement: ClassInfo cards contain only the class title
The client SHALL render a successful `read_class_docs` tool activity with the exact visible title `ClassInfo <class_name>`. It MUST NOT render member counts, byte counts, raw ClassDB/API text, JSON, or a detail expander for that activity.

#### Scenario: Rendering a bounded TileMap query
- **WHEN** a successful `read_class_docs` query targets `TileMap`
- **THEN** the rendered card title is exactly `ClassInfo TileMap` and contains no text such as `2 members`
