## 1. Target-aware screenshot contract

- [x] 1.1 Probe the target Godot version for per-viewport 3D editor camera/projection read, write, and restore support; define a controlled editor-plugin bridge if public APIs are insufficient.
- [x] 1.2 Define and validate bounded screenshot targets for current viewport rectangles, 2D CanvasItem/Control nodes, TileMap cell regions, and visible Node3D paths; include constrained `inspection`, viewport, direction, and padding inputs.
- [x] 1.2.1 Require map-verification targets to provide a valid TileMap layer and finite cell bounds; reject missing layers instead of defaulting to layer 0.
- [x] 1.3 Resolve 2D/3D target bounds against current scene/map facts and record scene identity, target bounds, coordinate spaces, hash, timestamp, and scope.
- [x] 1.4 Implement safe 2D editor framing, frame-settlement waiting, exact crop capture, and restoration of prior selection/viewport transform on every terminal path.
- [x] 1.4.1 Validate that a focused map crop is readable and intersects the requested map-local bounds; retain non-matching captures only as diagnostics.
- [x] 1.5 Implement per-viewport mutually exclusive 3D framing: merge visible world AABBs, calculate a containing camera, verify projected coverage, detect user camera changes, and restore only while its lease remains valid.

## 2. Screenshot observation contract

- [x] 2.1 Define a normalized visual-observation payload, identifier, bounded description limit, provenance, scope, capture metadata, inspection outcome, and terminal-state vocabulary.
- [x] 2.2 Implement safe extraction and normalization for explicit screenshot results and nested `visual_evidence` returned by map reload/rebuild tools.
- [x] 2.3 Route normalized image references through the existing asset-understanding client, passing only backend-validated structured inspection data from the current orchestration LLM and preserving path validation and bounded provider timeout behavior.
- [x] 2.4 Add session-local deduplication by image hash, normalized target, rubric, and model identity, with explicit refresh support.
- [x] 2.5 Add frontend-derived `spatial_facts` with coordinate system, source, version, and availability; never request engine coordinates from the visual model.

## 3. Durable context and transcript delivery

- [x] 3.1 Render normalized observation summaries and bounded inspection rubrics into front-tool Markdown so current and later model calls receive the visual conclusion, not only capture metadata.
- [x] 3.2 Persist the bounded observation payload, artifact locator, hash, coordinate provenance, and artifact-expiry state in authoritative transcript revisions and evidence sidecars without storing image bytes or data URLs.
- [x] 3.3 Add cancellation, timeout, unavailable, artifact-expiry, and provider-failure finalization so no visual-observation entry remains pending after the request reaches a terminal boundary.
- [x] 3.4 Ensure cancellation restoration rejects late vision completions and preserves the terminal transcript revision.
- [x] 3.5 Disclose configured remote visual analysis in tool results and support a disabled-analysis configuration with a durable unavailable outcome.

## 4. Map completion gate

- [x] 4.1 Update map result rendering and agent guidance to label capture, visual observation, advisory inspection outcome, and deterministic verification separately.
- [x] 4.2 Require matching `describe_map_region` evidence for map tasks that report tile placement changes; classify targeted screenshots as advisory supplementary evidence.
- [x] 4.3 Return an explicit unverified outcome when map-region evidence is absent, mismatched, or unavailable even if screenshot capture/observation succeeded.
- [x] 4.4 Reclassify map rebuild operations that clear, set, or erase TileMap cells as mutating and route them through the matching permission behavior.
- [x] 4.5 Return changed-cell bounds, or an explicit unavailable status, from rebuild results and require a passed focused capture before visual verification can succeed.

## 5. Verification and rollout

- [x] 5.1 Verify viewport-rectangle, CanvasItem/Control, TileMap-region, and Node3D captures, including off-screen framing, world-AABB/camera correctness, crop-coordinate correctness, user camera movement, and restoration after failure/cancellation.
- [x] 5.2 Verify explicit screenshots, nested rebuild/reload screenshots, disabled vision configuration, unreadable/expired paths, provider failure, deduplication, and interrupted analysis against the transcript/history contract.
- [x] 5.3 Verify that a completed map edit requires deterministic target/layer/bounds/cell evidence and that screenshot-only runs remain unverified.
- [x] 5.3.1 Verify missing-layer rejection, layer/bounds metadata, non-intersecting-crop rejection, and that a generic editor viewport cannot pass map verification.
- [x] 5.4 Run the relevant existing frontend and service validation suites, inspect history/recovery output, and document asset-understanding configuration, privacy disclosure, coordinate provenance, and 3D-capture limitations.
