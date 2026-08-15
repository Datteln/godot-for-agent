## MODIFIED Requirements

### Requirement: Collision facts are authoritative
Platform validation MUST read collision facts from the canonical scene target or an immutable, digest-bound authoritative map snapshot for the same target, layer, and revision used by the planner. Public planner and validation requests MUST NOT substitute caller-supplied raw collision cells, incomplete region summaries, or stale snapshot data for those facts. Region dictionaries consumed by occupancy reads and traversal checks MUST be produced by the canonical region constructor exposing both origin/size and min/max bounds. Validators MUST NOT hard-index region keys; a region missing bound keys SHALL fail closed with a typed invalid-region error instead of raising a runtime key error or degrading null bounds to a zero-sized region.

#### Scenario: Caller supplies raw collision cells
- **WHEN** a public validation request attempts to provide unverified collision cells
- **THEN** the validator ignores or rejects them and obtains authoritative facts

#### Scenario: Reader artifact revision is stale
- **WHEN** collision facts reference a different target revision
- **THEN** platform validation stops and requests a fresh read

#### Scenario: Snapshot coverage is incomplete
- **WHEN** the planner snapshot omits actor, trajectory, landing, headroom, or support cells required by validation
- **THEN** validation returns a typed incomplete-coverage issue and creates no approved batch

#### Scenario: describe_map_region computes live object occupancy
- **WHEN** the describe_map_region handler builds the occupancy region for `_live_object_occupancy`
- **THEN** it passes the canonical normalized region and the result lists every collision object whose mapped cell lies inside the requested region

#### Scenario: Region missing bound keys fails closed
- **WHEN** a validator or occupancy read receives a region dictionary without `min_x`/`max_x` (or other bound keys)
- **THEN** the call returns a typed invalid-region error and neither raises a runtime key error nor treats the region as zero-sized
