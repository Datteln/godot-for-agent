## Why

The map agent treated a request to extend an existing floor by ten tiles as a code-driven reconstruction of the whole legacy TileMap layer. That decision erased unrelated authored terrain because the generated layout did not faithfully represent the entire scene. The agent needs a better editing mental model: existing map content is the source of truth, and a local request normally calls for a local change.

## What Changes

- Teach the map agent to classify a request as a local incremental edit or a generation/rebuild task before choosing tools or authoring strategy.
- Make preservation of observed, user-authored map content the default assumption for local requests.
- Add map-agent guidance and worked examples that contrast a bounded floor extension with an inappropriate full-layer reconstruction.
- Require the agent to articulate the intended delta, nearby content that must remain unchanged, and why its chosen approach preserves it.
- Add a post-edit preservation review that compares the requested change with the observed surrounding map and reports unrelated changes honestly.
- Reframe builder bootstrap as an option for explicit generation or an existing dedicated generated target, rather than the default response to a missing authoring entry point.

## Capabilities

### New Capabilities

- `incremental-map-editing-guidance`: Teaches map agents how to choose and validate a minimal, preservation-oriented map editing approach.

### Modified Capabilities

- None.

## Impact

- Map-agent role instructions and the bundled `godot-map-authoring` skill.
- Map-agent prompt context, example-based guidance, and map workflow regression tests.
- The user experience of local TileMap edits; no new runtime or hard execution constraint is introduced by this change.
