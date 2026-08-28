## 1. Preservation-first map-agent guidance

- [x] 1.1 Revise the map-agent role to classify local incremental requests before selecting a builder or layout strategy.
- [x] 1.2 Replace the missing-authoring-entry bootstrap default with preservation-first guidance for existing authored TileMaps.
- [x] 1.3 Add a concise delta-and-preservation summary to the map agent's pre-mutation planning guidance.

## 2. Bundled map-authoring curriculum

- [x] 2.1 Update `godot-map-authoring` to teach the distinction between local edits, explicit generation, and migration.
- [x] 2.2 Add contrastive floor-extension, tower-conflict, and explicit-regeneration examples to the skill.
- [x] 2.3 Teach post-edit review of the changed area and immediate surrounding authored context, with truthful reporting of unrelated differences.

## 3. Prompt-level regression coverage

- [x] 3.1 Add a fixture or test case where extending an existing floor chooses an incremental strategy and preserves neighboring terrain.
- [x] 3.2 Add a fixture or test case where a local extension meets a tower and the agent proposes a local resolution without deleting the tower.
- [x] 3.3 Add a fixture or test case where explicit procedural regeneration still selects the builder strategy with a stated rationale.
- [x] 3.4 Add assertions for the delta-and-preservation explanation and post-edit discrepancy reporting.

## 4. Verification

- [x] 4.1 Run affected map-agent prompt/context and frontend/service regression tests.
- [x] 4.2 Validate the OpenSpec change and review the final agent guidance for non-goal compliance: no hard execution constraint or builder ban.
