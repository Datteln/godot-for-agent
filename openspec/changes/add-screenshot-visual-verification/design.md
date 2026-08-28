## Context

The frontend can capture only the entire current 2D or 3D editor viewport and return capture metadata. It has no target node, TileMap-region, crop, editor framing, or restoration contract. The backend enriches explicit `capture_viewport_screenshot` results with a multimodal description when asset understanding is configured, but its prompt is generic and the description is transient: it is not included in the authoritative transcript or evidence sidecar. Screenshot paths nested beneath `reload_map_targets` and `rebuild_map_builder` results are not enriched at all. The current map flow can therefore confuse a successfully saved PNG with visual proof, and cancellation can leave visual-verification entries permanently running.

## Goals / Non-Goals

**Goals:**

- Make every eligible screenshot, explicit or nested in a map-tool result, produce one normalized visual observation.
- Retain bounded visual conclusions and analysis provenance in the same durable session artifacts used for history, recovery, and later model context.
- Model capture, observation, and verification as distinct states with terminal cancellation/failure outcomes.
- Let an agent request a precise, reproducible 2D or 3D visual target without silently relying on the user's current viewport framing.
- Return coordinates and bounds derived by the frontend scene/rendering APIs, never guessed from image pixels.
- Keep deterministic map-region reads authoritative for tile-placement completion.

**Non-Goals:**

- Treating a multimodal description as pixel-perfect, collision, gameplay, or source-of-truth map verification.
- Capturing gameplay/runtime frames, arbitrary desktop pixels, or auto-framing a Node3D that has no finite visible geometry.
- Introducing another model provider, an image database, or long-term binary screenshot storage.

## Decisions

### 1. Add target-aware, bounded 2D and 3D capture

Extend the screenshot contract with an optional `target` union:

- `viewport_rect`: a bounded pixel rectangle within the currently captured viewport; it never changes editor framing.
- `canvas_item`: a scene-relative `CanvasItem` or `Control` node path plus bounded pixel padding.
- `map_region`: a `TileMap`/`TileMapLayer` path, explicit layer where applicable, cell bounds, and bounded cell padding.

The frontend resolves node/map facts against the currently edited scene, derives the 2D canvas rectangle, frames that rectangle if it is off-screen, waits for the editor to settle, captures the viewport, and crops the exact target rectangle. The observation includes edited-scene identity, requested target, resolved target bounds, viewport/crop coordinate spaces, editor transform, screenshot hash, and capture timestamp. It MUST restore the prior selection and 2D viewport transform after capture, including failure paths.

The target union additionally accepts `node_3d`: a `Node3D` path, `viewport_index`, optional `view_direction` (`current`, six-axis direction, or `isometric`), and bounded padding. The frontend accepts a `VisualInstance3D` or a Node3D containing visible descendants, merges finite world AABBs, and calculates a camera position that contains the target bounding sphere at the active projection and viewport aspect ratio. It records the original camera transform, projection, selection, and a viewport-state version, waits for the projected bounds to settle, captures, then restores only if the transaction still owns that viewport.

Only one 3D framing transaction may run for a viewport at a time. If the user changes its camera while the transaction is active, the operation ends `editor_view_changed` and MUST NOT overwrite the new user view. A missing node, no visible geometry, non-finite bounds, or unavailable camera-control API returns `target_missing`, `target_not_visual`, `bounds_unavailable`, or `target_unavailable`; an unrelated whole-viewport image must never be presented as target evidence. Before implementation, a Godot-version API probe MUST prove per-viewport camera/projection read, write, and restoration. If public `EditorInterface` APIs are insufficient, a version-limited editor-plugin bridge is required; simulated input is prohibited.

### 2. Normalize screenshot references before enrichment

Create one backend normalization step that extracts image references from explicit screenshot results and from nested `visual_evidence` fields returned by `reload_map_targets` and `rebuild_map_builder`. Each reference carries the source tool, capture path, dimensions, scope, advisory flag, and an immutable observation id. The normalizer resolves only existing local files and preserves existing project/user path safety checks.

This avoids separate enrichment rules for each map tool. Directly teaching each caller to invoke `capture_viewport_screenshot` was rejected because automatic captures would still silently bypass analysis.

### 3. Persist an explicit visual-observation record

Persist a bounded record containing capture metadata, multimodal model identity, analysis status, a truncated description, and error/cancellation reason where applicable. The record is linked from the originating tool activity and has a visible transcript representation; evidence sidecars include the same bounded semantic fact.

Raw image bytes and data URLs are never stored in session JSON, transcript patches, evidence sidecars, or LLM context. The existing PNG file remains the artifact reference.

### 4. Use terminal observation states

Use `pending`, `observed`, `unavailable`, `failed`, and `cancelled` states. `observed` means a configured visual model returned text, not that the task is semantically verified. Any cancellation, timeout, unreadable image, disabled configuration, or provider failure MUST reach a terminal state and update an already-visible pending entry.

This prevents cancellation restoration from leaving a perpetual “running” entry. Reusing generic tool success was rejected because `ok: true` only proves capture/write success.

### 5. Use an inspection rubric, not a generic caption

The capture request carries an optional bounded, structured `inspection`: question, expected conditions, target of attention, and allowed observation dimensions. The visual-model prompt asks only for a structured, advisory observation of that target: summary, matching/contradicting/inconclusive outcome, confidence, and limitations. It MUST instruct the model to ignore text in the image as commands and never derive Godot coordinates from pixels. An omitted inspection retains generic-caption behavior.

The frontend adds `spatial_facts` to the capture and observation record. These are source-derived, versioned facts: CanvasItem/Control `canvas_rect` and `viewport_rect_px`; TileMap `map_layer`, `cell_bounds`, and `map_local_rect`; Node3D `world_aabb`, target origin, `viewport_rect_px`, camera pose, and projection. Every fact includes `coordinate_space`, `source`, and availability. The vision result may refer to a fact identifier but must not generate engine-space numbers.

An `observed` state means that analysis text was returned. It is independent from the advisory inspection outcome and never equals deterministic verification.

### 6. Deliver a bounded observation summary to context

The current turn and later retained context receive a Markdown observation summary with source tool, scope, status, provenance, and bounded description. Timeline/history use the same normalized payload. Context compaction may summarize old observations but MUST retain their terminal status and artifact locator.

### 7. Keep map evidence two-tiered

Map completion requires deterministic `describe_map_region` evidence matching the declared target layer, bounds, and expected cells. A visual observation may support user-facing review and identify obvious mismatches, but cannot independently mark a tile-placement task complete. A future task may define visual-only acceptance criteria explicitly; that is outside this change.

### 8. Bound cost, privacy, and artifact lifetime

Deduplicate observations within a session by image hash, normalized target, rubric, and vision-model identity. Reuse a terminal observation only when all identifiers match; a changed viewport/crop or explicit refresh creates a new record. Before remote analysis, the visible tool result and context summary identify the configured model/provider class and that the screenshot leaves the local process. A configuration switch disables remote analysis while retaining local capture.

Screenshots retain their existing temporary local-file behavior. Each observation stores a hash, capture time, and locator but no bytes. History keeps an existing description even when the artifact later disappears, and re-analysis then ends as `unavailable` with `artifact_expired` rather than claiming the old image remains readable.

## Risks / Trade-offs

- [Vision model incorrectly describes editor chrome or a stale view] → label every description advisory, retain scope/path/provenance, and require deterministic map reads for completion.
- [Visual endpoint adds seconds to a tool-return turn] → use existing timeout, process only one normalized reference per capture, and persist `unavailable`/`failed` rather than blocking indefinitely.
- [Session/history growth] → bound description length and store no image bytes.
- [Automatic captures reveal an unexpected viewport] → persist capture scope and only state that the image was observed; do not infer scene correctness from it.
- [Cancellation races an in-flight provider call] → finalize the observation from the cancellation boundary and reject late completion for a terminal observation id.
- [Target framing disturbs the user's editor] → use short 2D/3D transactions, viewport leases and finally-style restoration; cancel 3D capture rather than restore over a user-changed camera.
- [Public Godot APIs cannot manipulate the 3D editor camera] → prove the API path first and use a controlled version-limited editor-plugin bridge if necessary.
- [A visual description follows the wrong target] → attach exact resolved crop bounds, scene identity, target revision/facts, hash, and timestamp to every observation.
- [Editor content is sent to an external provider unexpectedly] → disclose provider use at capture time and expose a configuration switch that yields a durable `unavailable` result.

## Migration Plan

1. Add target schema/validation and normalized observation rendering while accepting legacy whole-viewport screenshot results without semantic fields.
2. Implement safe 2D and 3D target framing/cropping with state restoration, then route explicit and nested map-tool captures through the normalizer.
3. Persist statuses, provenance, inspection outcomes, and artifact-expiry handling in transcript/evidence rendering.
4. Enforce the map completion gate after deterministic map-region evidence is available.
5. Roll back by disabling target framing and/or asset understanding: whole-viewport captures remain available, with remote analysis marked `unavailable`; no existing screenshot artifact or transcript data requires migration.

## Open Questions

- Should users be able to request a manual re-analysis of an existing screenshot artifact after a provider outage?
- What maximum persisted description length best balances map-review usefulness and long-session context cost?
- Should target framing visibly animate in the editor, or remain silent while restoring the user’s exact prior view?
