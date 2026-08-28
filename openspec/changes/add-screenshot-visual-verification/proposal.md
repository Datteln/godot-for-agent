## Why

Viewport screenshots are sometimes analyzed by the configured multimodal model, but that analysis is only transient and always uses a generic image-description prompt. The current tool can only capture the entire currently framed 2D or 3D editor viewport: it cannot target a scene node, TileMap region, screen rectangle, or 3D node. Automatic screenshots nested in map rebuild/reload results bypass analysis entirely, while persisted timeline and evidence records retain only `ok`, path, and dimensions. A cancelled turn can therefore discard the only visual conclusion and leave a map task without auditable visual verification or reusable spatial facts.

## What Changes

- Normalize explicit and automatically captured screenshots into one visual-observation pipeline.
- Add bounded, target-aware capture for current-viewport rectangles, CanvasItem/Control bounds, TileMap cell regions, and visible Node3D targets; preserve and restore any editor framing changed to acquire the target.
- Automatically frame a 3D node from its frontend-derived world bounds and return source-derived scene/map/screen coordinates, boundary facts, and camera details with explicit coordinate systems.
- Pass a bounded, structured inspection objective to the visual model so its advisory observation changes with the requested question; the model must never invent engine coordinates from pixels.
- Send every eligible screenshot through configured multimodal understanding, persist a bounded description and its provenance, and make it available to subsequent agent turns.
- Distinguish capture success, visual observation, and semantic verification so a PNG with `ok: true` cannot be presented as proof that a map edit is correct.
- Preserve terminal visual-observation state when a turn is interrupted, timed out, or cannot be analyzed.
- Add map completion rules that require deterministic region evidence for tile placement; visual observations remain advisory unless a task explicitly accepts them.
- Require map-verification captures to declare and resolve the intended TileMap layer and finite cell bounds; reject a missing layer instead of defaulting to layer 0.
- Validate that focused map evidence is readable and covers the requested map region, and classify TileMap rebuilds that clear, set, or erase cells as mutating operations.
- Make external visual analysis transparent, cache equivalent observations within a session, and record screenshot-artifact expiry without storing image bytes long-term.

## Capabilities

### New Capabilities

- `screenshot-visual-observation`: Capture, multimodal analysis, durable storage, and contextual delivery of screenshot observations with explicit evidence status.
- `map-visual-verification-gate`: Evidence requirements and completion semantics for map edits that use screenshots alongside deterministic map-region reads.

### Modified Capabilities

- `authoritative-chat-transcript`: Persist and render the terminal status of visual observations so interrupted analysis is never displayed as running or successful.

## Impact

- Frontend screenshot capture, target validation, and automatic map reload/rebuild evidence handling.
- Backend front-tool result enrichment, Markdown context rendering, evidence sidecars, session persistence, and transcript entries.
- Agent prompts/tool contracts for screenshot and map completion decisions.
- The configured asset-understanding multimodal endpoint; no new provider is required.
