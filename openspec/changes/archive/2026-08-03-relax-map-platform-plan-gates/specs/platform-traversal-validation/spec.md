## ADDED Requirements

### Requirement: Subjective design-quality checks are advisory
The validator SHALL distinguish objective traversal correctness from subjective design quality. Subjective design-quality conditions — a non-rest platform exceeding the configured maximum width, a challenge role repeated beyond the configured limit, and a route with fewer than the minimum segment count — SHALL be reported in `issues` and `repair_plan` but MUST NOT set a blocking failure, MUST NOT empty `edit_map_batches`, and MUST NOT prevent execution of an otherwise objectively valid plan.

#### Scenario: Over-wide non-rest platform
- **WHEN** a non-rest platform exceeds the configured max width and every objective traversal check passes
- **THEN** the plan executes and the over-width is reported as an advisory issue, not a blocking failure

#### Scenario: Repeated challenge roles
- **WHEN** the same challenge role repeats beyond the configured limit and every objective traversal check passes
- **THEN** the plan executes and the repetition is reported as advisory only

#### Scenario: Objective reachability still blocks
- **WHEN** a planned platform transition exceeds movement ability or a segment endpoint disagrees with referenced platform geometry
- **THEN** validation blocks execution with a typed reachability issue regardless of any subjective design-quality findings

#### Scenario: Advisory and blocking score issues coexist
- **WHEN** one or more advisory score issues precede a blocking score issue in `issue_details`
- **THEN** validation blocks execution and the top-level `error_code` identifies the first non-advisory issue rather than an advisory entry

### Requirement: Entry anchor accepts a flat coordinate dictionary
The validator SHALL accept an `entry_anchor` supplied as a flat coordinate dictionary (`x`, `y`, optional `role`) without a nested `cell` key and SHALL treat it as a valid anchor. Anchor parsing MUST NOT discard a flat coordinate dictionary as empty.

#### Scenario: Flat entry anchor is provided
- **WHEN** a plan provides `entry_anchor` as a flat `{x, y, role}` dictionary with no nested `cell` key
- **THEN** the validator consumes it as the entry anchor and does not return `entry_anchor_not_found`

#### Scenario: Wrapped entry anchor is provided
- **WHEN** a plan provides `entry_anchor` as a wrapper containing a nested `cell` coordinate dictionary
- **THEN** the validator unwraps `cell` and consumes the inner coordinates

### Requirement: Absent structured fields are rejected, not silently defaulted
The validator SHALL reject a plan with a typed missing-field issue when a required structured field is absent, and MUST NOT silently treat an absent field as a present empty collection. A presence guard that supplies an empty dictionary or array as the default value MUST distinguish "key absent" from "key present and empty".

#### Scenario: Required coordinate field is absent
- **WHEN** an entry omits a required coordinate field and the validator guards presence with a default collection
- **THEN** the guard reports the field as missing instead of treating the default empty collection as a present value

#### Scenario: Required field is present and empty
- **WHEN** a required structured field is present as an explicitly empty collection
- **THEN** the guard treats it as present-and-empty and reports the emptiness as a typed issue

### Requirement: Connectivity repair plans do not fabricate unreachable paths
A connectivity repair plan SHALL only suggest paths that have been validated as reachable under the configured movement model. The validator MUST NOT synthesize a repair path from an unvalidated geometric heuristic such as a raw manhattan trace.

#### Scenario: Repair hint is requested
- **WHEN** a connectivity failure produces a repair plan
- **THEN** the suggested path is either validated as reachable or omitted, never a fabricated manhattan trace
