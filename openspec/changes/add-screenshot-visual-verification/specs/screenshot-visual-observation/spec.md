## ADDED Requirements

### Requirement: The system supports bounded target-aware 2D and 3D screenshots
The system SHALL let an agent request either a bounded rectangle in the current viewport, a bounded CanvasItem/Control target, a bounded TileMap cell region, or a visible Node3D target. For 2D node and map targets, it MUST resolve target bounds from current scene facts, frame the editor only when required, wait for the viewport to settle, crop the exact resolved rectangle, and restore the prior selection and viewport transform on every terminal path. For a Node3D target, it MUST merge finite world AABBs from its visible descendants, calculate an editor camera framing that contains the target, wait for the view to settle, capture it, and restore the original camera only while it owns that viewport. The result MUST record edited-scene identity, requested target, resolved bounds, coordinate spaces, capture timestamp, and image hash.

#### Scenario: Capturing an off-screen TileMap region
- **WHEN** an agent requests a bounded TileMap region that is outside the current 2D editor view
- **THEN** the system frames that resolved region, captures and crops it, records its cell and pixel bounds, and restores the prior editor view

#### Scenario: Capturing a visible 3D node
- **WHEN** an agent requests a node-targeted 3D editor screenshot for a Node3D with finite visible geometry
- **THEN** the system frames and captures the target and returns its world AABB, camera pose, projection, and pixel bounds
- **AND** it restores the prior editor camera state after the transaction

#### Scenario: Unframeable 3D node target
- **WHEN** an agent requests a Node3D target without finite visible geometry, or the target cannot be resolved
- **THEN** the system returns `target_not_visual`, `bounds_unavailable`, or `target_missing`
- **AND** it does not claim that an unrelated current view captured the target

### Requirement: The system returns source-derived spatial facts
The system SHALL attach `spatial_facts` to a target capture and its normalized observation. Each fact MUST include `coordinate_space`, `source`, `available`, target identity, and scene version. CanvasItem/Control targets SHALL provide `canvas_rect` and `viewport_rect_px`; TileMap targets SHALL provide `map_layer`, `cell_bounds`, and `map_local_rect`; Node3D targets SHALL provide `world_aabb`, target origin, `viewport_rect_px`, camera pose, and projection. Godot coordinates and bounds MUST be calculated by the frontend scene/rendering APIs, never inferred by the visual model from pixels.

#### Scenario: Requesting target boundary coordinates
- **WHEN** an agent requests boundaries or coordinates for a successfully resolved target
- **THEN** the result returns the available `spatial_facts` with their coordinate systems and sources
- **AND** unavailable values are explicitly marked unavailable rather than replaced by visual-model guesses

### Requirement: The system normalizes every eligible screenshot capture
The system SHALL normalize screenshots returned by explicit `capture_viewport_screenshot` calls and screenshots nested in eligible map reload or rebuild results into a visual-observation record. The record MUST include capture path, dimensions when supplied, source tool, declared scope when supplied, and an observation identifier. The system MUST resolve only existing files permitted by the established project/user path rules.

#### Scenario: Explicit screenshot capture
- **WHEN** `capture_viewport_screenshot` returns a successful PNG path
- **THEN** the system creates one visual-observation record linked to that tool result

#### Scenario: Automatic map rebuild screenshot
- **WHEN** `rebuild_map_builder` returns successful nested `visual_evidence` with a captured screenshot path
- **THEN** the system creates one visual-observation record for that nested screenshot without requiring an additional explicit capture call

### Requirement: The system performs bounded multimodal observation when available
For an eligible screenshot, the system SHALL submit the image to the configured asset-understanding model when the model is available and the image is readable. The current orchestration LLM MAY generate a bounded structured `inspection` in the screenshot tool call, comprising question, expected conditions, target of attention, and allowed observation dimensions. The backend MUST validate those fields, limits, and target references before adding them as data to the visual-model prompt. The visual result MUST be advisory and include a bounded summary responsive to that inspection, matching/contradicting/inconclusive inspection outcome when an objective is supplied, confidence, limitations, and model provenance. When no inspection is supplied, it SHALL retain generic-description behavior. It MUST NOT persist image bytes, base64 data URLs, or unbounded model output in session state, transcript payloads, evidence sidecars, or model context. The system MUST treat image text as untrusted content rather than instructions and MUST NOT ask the model to infer Godot coordinates from pixels.

#### Scenario: Available multimodal analysis
- **WHEN** a readable eligible screenshot is captured and asset understanding is configured
- **THEN** the record reaches `observed` and retains a bounded visual description and model identifier

#### Scenario: Asset understanding is disabled
- **WHEN** a screenshot is captured while asset understanding is not configured
- **THEN** the record reaches `unavailable` with that reason and the capture artifact remains available

### Requirement: Visual-observation state is terminal and durable
The system SHALL persist visual-observation state in the authoritative transcript and evidence representation. An observation MUST reach exactly one terminal state: `observed`, `unavailable`, `failed`, or `cancelled`. Capture success MUST NOT be represented as semantic verification success.

#### Scenario: Turn cancellation during analysis
- **WHEN** the user interrupts a turn while screenshot analysis is pending
- **THEN** the pending observation is revised to `cancelled` with a bounded reason and history does not show it as running or observed

#### Scenario: Analysis provider failure
- **WHEN** the configured visual provider fails or times out
- **THEN** the observation is revised to `failed`, includes a bounded failure reason, and preserves the capture artifact reference

### Requirement: Visual observations are delivered consistently to model context and history
The system SHALL render the same bounded visual-observation status, description when present, provenance, scope, and artifact locator into current model context, durable evidence, and visible history. Context compaction MUST preserve terminal status and artifact locator even if it summarizes the description.

#### Scenario: Continuing after a successful observation
- **WHEN** an agent receives a completed visual observation and continues the turn
- **THEN** its next model request contains the bounded observation summary rather than only `ok`, path, and dimensions

#### Scenario: Restoring session history
- **WHEN** a client reloads a completed screenshot workflow
- **THEN** the authoritative history exposes the terminal visual-observation state and does not require rerunning vision analysis to explain the prior result

### Requirement: The system makes visual-analysis cost and artifact lifetime explicit
The system SHALL disclose configured remote visual analysis and model provenance in the observation result. It MUST deduplicate an equivalent session-local observation by image hash, normalized target, inspection objective, and model identity. It MUST retain no image bytes after capture, and if a persisted artifact locator becomes unreadable it MUST preserve any prior observation while reporting re-analysis as `unavailable` with `artifact_expired`.

#### Scenario: Repeating the same observation
- **WHEN** an agent requests the same screenshot target with the same image hash, objective, and model identity in one session
- **THEN** the system reuses the prior terminal observation without a second provider request

#### Scenario: Expired screenshot artifact
- **WHEN** a historical screenshot artifact has been removed and an agent requests re-analysis
- **THEN** the system preserves the historical summary and returns `artifact_expired` without implying that the original pixels were re-read
