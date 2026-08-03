## 1. Authoritative Snapshot Contracts

- [x] 1.1 Define `authoritative_map_snapshot_v1`, planner projection, completeness, evidence-source, traversal-profile, and execution-eligibility schemas in the service contracts.
- [x] 1.2 Implement snapshot artifact persistence and digest verification keyed by target, layer, revision, and coverage without copying large cell arrays into worker summaries.
- [x] 1.3 Extend canonical Godot map reads to produce exact cell/resource, collision/support, object-occupancy freshness, entry, boundary, and coverage evidence required by the snapshot schema.
- [x] 1.4 Implement snapshot builder validation that marks missing or truncated facts explicitly and rejects defaults as execution-authorizing movement facts.
- [x] 1.5 Add deterministic reachable-frontier materialization/recomputation against the snapshot revision and traversal profile.

## 2. Workflow State and Evidence

- [x] 2.1 Add reducer-owned events and lifecycle metadata for snapshot identity, snapshot scope, planner attempts, candidate fingerprints, repair artifacts, planning status, and execution status.
- [x] 2.2 Persist and hydrate the new planning state across restart while invalidating stale approvals when authoritative target revision or snapshot digest changes.
- [x] 2.3 Extend the per-turn map-progress digest to re-inject bounded snapshot, attempt, repair, and publication references after conversation compaction.
- [x] 2.4 Add compatibility handling that returns a typed migration block for active legacy plans that cannot supply a valid snapshot or new approval contract.

## 3. Planner and Skill Boundary

- [x] 3.1 Define the planner input contract that binds a compatible snapshot projection and the planner output contract for route geometry, semantic resources, reference cells, and rationale.
- [x] 3.2 Update map planner scheduling so missing, stale, or incomplete snapshot fields trigger typed reader refresh/recompute work rather than ad hoc untracked fact reads.
- [x] 3.3 Update `map-area-expansion` and related planner instructions to consume injected traversal, entry, boundary, and frontier facts and to forbid planner-authored naked atlas batches.
- [x] 3.4 Update planner agent tools/capability binding so exact fact collection stays reader-owned, exact resource compilation stays validator/compiler-owned, and planner tool search cannot expand write authority.
- [x] 3.5 Add skill/worker contract tests proving the documented snapshot requirements and effective runtime inputs remain aligned.

## 4. Deterministic Validation and Compilation

- [x] 4.1 Update platform validation to require the same snapshot target, layer, revision, digest, complete trajectory coverage, and explicit traversal profile used by the planner.
- [x] 4.2 Implement deterministic semantic-resource and reference-cell resolution to exact TileSet/GridMap write operations using verified snapshot and registry data.
- [x] 4.3 Separate structured route-validation failures from resource-compilation failures and preserve valid route candidates across typed resource refreshes.
- [x] 4.4 Generate immutable approved batch artifacts containing approval id, snapshot id/digest, target, layer, expected revision, compiled operations, and batch fingerprint.

## 5. Three-Attempt Planning and Publication

- [x] 5.1 Add a reducer/scheduler attempt key scoped to task lineage, target, layer, snapshot id, and planning operation with a hard maximum of three deterministic validation attempts.
- [x] 5.2 Bind each failed attempt's structured issues and repair artifact into the next planner Frame and return typed `unchanged_plan_attempt` for an unchanged or non-repaired candidate.
- [x] 5.3 Add a plan-publication graph step independent from writer execution and allow it to consume either an approved plan or the exhausted third candidate.
- [x] 5.4 Publish the latest failed candidate with `planning_status=delivered`, `execution_status=blocked_by_validation`, unresolved issues, validation history, unchanged revision, and no approved batch.
- [x] 5.5 Preserve task-level convergence accounting when a real revision or fact change creates a new snapshot and exact three-attempt budget.

## 6. Writer Enforcement and UI Events

- [x] 6.1 Reject planner-produced raw atlas operations and require writer inputs to match compiler approval id, snapshot digest, target, layer, revision, and batch fingerprint before transaction creation.
- [x] 6.2 Recheck authoritative revision and snapshot evidence after recovery and immediately before mutation, preserving existing approval consumption and replay guarantees.
- [x] 6.3 Emit distinct planning-delivered, execution-approved, execution-blocked, write-committed, and map-edit-incomplete events so the UI cannot present a blocked plan as an edited map.

## 7. Verification and Migration

- [x] 7.1 Add schema and unit tests for snapshot completeness, traversal occupancy sources, projection redaction, digest mismatch, and semantic atlas resolution.
- [x] 7.2 Add scheduler tests for pass-on-attempt-one/two/three, three failures with final publication, fourth-attempt refusal, unchanged candidate handling, and writer non-scheduling.
- [x] 7.3 Add compaction and restart tests proving snapshot, frontier, attempts, repair plans, and final planning results rehydrate without repeating completed work.
- [x] 7.4 Add Godot integration tests for stale revision, incomplete coverage, frontier recomputation failure, resource-registry drift, approval replay, and pre-mutation rejection.
- [x] 7.5 Run shadow-mode comparisons on representative 2D platform, multilayer TileMap, and 3D GridMap fixtures, then remove the legacy planner-produced raw batch path after parity checks pass.
