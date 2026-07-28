## ADDED Requirements

### Requirement: Collision facts are authoritative
Platform validation MUST read collision facts from the canonical scene target or a digest-bound reader artifact for the same target and revision.

#### Scenario: Caller supplies raw collision cells
- **WHEN** a public validation request attempts to provide unverified collision cells
- **THEN** the validator ignores or rejects them and obtains authoritative facts

#### Scenario: Reader artifact revision is stale
- **WHEN** collision facts reference a different target revision
- **THEN** platform validation stops and requests a fresh read

### Requirement: Leap validation samples the complete actor trajectory
The validator SHALL sample the leap trajectory with the configured actor footprint and check intermediate collision, headroom, landing width, and landing clearance.

#### Scenario: Arc intersects an overhead tile
- **WHEN** start and landing cells are valid but the sampled actor footprint intersects overhead collision
- **THEN** the leap is not executable and the result identifies the obstructed samples

#### Scenario: Landing has insufficient clearance
- **WHEN** the trajectory reaches a platform whose landing footprint or headroom is insufficient
- **THEN** the leap is not executable

### Requirement: Segments match referenced platform geometry
Each route segment MUST reference existing endpoint ids and its from/to coordinates and direction MUST agree with the referenced platform geometry.

#### Scenario: Segment references an unknown endpoint
- **WHEN** a segment's from or to id does not exist in the platform set
- **THEN** validation fails with a structured endpoint issue

#### Scenario: Segment coordinates disagree with ids
- **WHEN** segment coordinates do not lie on the referenced endpoint platforms
- **THEN** validation fails even if the textual route order is otherwise valid

### Requirement: Default movement abilities cannot authorize execution
The system MUST require explicit actor movement and clearance facts before returning an executable platform plan.

#### Scenario: Ability values are defaults
- **WHEN** platform validation uses default ability or actor-size values
- **THEN** the plan remains non-executable and reports the missing explicit inputs
