# atomic-tool-result-submission Specification

## Purpose

Define atomic validation, persistence, publication, idempotency, and artifact handling for mixed server/front tool-result submissions.

## Requirements

### Requirement: Entire tool-result batch is validated before mutation
The system MUST validate every result's tool id, turn id, frame ownership, status, pending metadata, and authorization before applying any result in the batch.

#### Scenario: A later result is invalid
- **WHEN** a batch contains valid earlier results and a later result that fails validation
- **THEN** the system rejects the entire batch and preserves the pre-request Session state

#### Scenario: Frame metadata does not match
- **WHEN** a result's frame id differs from the frame recorded in pending metadata
- **THEN** the system rejects the batch before updating messages, grants, checkpoints, caches, or persisted Session data

### Requirement: Valid batches commit as one Session transaction
The system SHALL apply a validated batch to an isolated Session working copy and make it active only after the complete batch can be persisted.

#### Scenario: All results are valid
- **WHEN** every result passes preflight and every reducer succeeds on the working copy
- **THEN** the system persists and activates all changes as one commit

#### Scenario: Applying a result fails
- **WHEN** a reducer or Session persistence fails after preflight
- **THEN** the active Session remains equal to its pre-request state

### Requirement: Retried submissions are idempotent
The system MUST identify committed tool-result batches by turn id and canonical content fingerprint.

#### Scenario: Client retries an already committed batch
- **WHEN** the same turn id and canonical result fingerprint are submitted again
- **THEN** the system returns the cached response without applying state changes or grants again

#### Scenario: A retried turn carries different artifact content
- **WHEN** an already known turn id is submitted with a different canonical artifact fingerprint
- **THEN** the system rejects the conflicting submission without replacing the committed turn block

### Requirement: Map tool artifacts use one Session document
The system MUST persist all large map-tool results for one Session in a single `map_artifacts.json` document, addressed by `turn_id` and `tool_use_id`, and MUST NOT create one persistent `describe_map_region-*.json` file per invocation.

#### Scenario: One turn returns multiple map regions
- **WHEN** a tool-result batch contains multiple successful `describe_map_region` calls
- **THEN** every result is stored under `turns[turn_id].entries[tool_use_id]` in the same Session `map_artifacts.json`

#### Scenario: A historical map result is referenced
- **WHEN** an Agent receives an artifact locator for a committed map result
- **THEN** the locator contains the Session artifact path, artifact turn id, and artifact entry id and resolves that exact JSON block without defaulting to the latest turn

### Requirement: Staged map artifacts follow the Session transaction
The system SHALL aggregate map results for the active submission as one staged turn block, allow the active transaction to read that block, and publish it to the Session artifact document only after Session persistence succeeds.

#### Scenario: Agent reads an artifact during the same submission
- **WHEN** a server-side map artifact reader requests an entry produced by front-tool results in the active uncommitted submission
- **THEN** the reader resolves the entry from the transaction-local staged turn block without requiring the persistent file to contain it

#### Scenario: Session commit succeeds
- **WHEN** the Session working copy is persisted successfully
- **THEN** the runtime atomically merges the complete staged turn block into the single `map_artifacts.json` document

#### Scenario: Submission is interrupted or rolled back
- **WHEN** the request is interrupted, cancelled, rejected, or fails reducer or Session persistence
- **THEN** the staged turn block is discarded and no committed locator or residual turn block remains

### Requirement: Mixed-side tool batches preserve execution semantics
The orchestrator SHALL execute server tools on the service and return only pending front tools to the Godot client while preserving all calls as one logical model-request batch.

#### Scenario: Model requests one server tool and two front tools
- **WHEN** one model response requests `search_tools` and two `describe_map_region` calls
- **THEN** the server tool may complete before the response is returned and the client receives only the two front calls without the server tool being classified as missing

### Requirement: Atomic publication remains externally live
The system MUST expose non-mutating request-liveness progress while Session events and artifacts remain buffered for atomic commit.

#### Scenario: A valid tool-result submission runs longer than the client idle timeout
- **WHEN** the backend is still processing the submission but no committed event can yet be published
- **THEN** it emits an out-of-band `turn_progress` heartbeat containing request/turn identity and phase but no assistant content, tool result, grant, artifact locator, or recoverable Session mutation

#### Scenario: A buffered submission rolls back
- **WHEN** the Session working copy is rejected, interrupted, or fails persistence after heartbeats were emitted
- **THEN** no buffered business event or artifact becomes visible and the earlier heartbeats cannot be replayed as committed state

#### Scenario: The client receives progress
- **WHEN** either a progress heartbeat or a committed event arrives for the active request
- **THEN** the client refreshes its idle watchdog without resubmitting the chat request or selecting a model

### Requirement: Legacy per-call artifacts are read-only migration inputs
The system MAY read an existing referenced `describe_map_region-*.json` during migration but MUST write all new map-tool artifacts to the Session `map_artifacts.json`.

#### Scenario: Persisted history references an existing legacy artifact
- **WHEN** a legacy per-call artifact still exists and passes path and schema validation
- **THEN** the compatibility reader may return it without creating another per-call artifact

#### Scenario: Persisted history references a deleted legacy artifact
- **WHEN** a referenced legacy artifact no longer exists
- **THEN** the system returns a structured missing-artifact result instead of guessing content or recreating it from an unrelated turn
