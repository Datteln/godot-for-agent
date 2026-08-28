## Context

The current map-agent and `godot-map-authoring` guidance strongly favors creating a readable layout and an `@tool` builder when a project has no existing authoring entry point. That advice is useful for intentional procedural generation, but it gave the model the wrong abstraction for a small request to extend an already authored floor. The model inferred that it should reconstruct the full legacy TileMap, then used an incomplete representation as the source of truth.

This change is a prompt-and-example intervention. It teaches a better reasoning process; it does not add a hard executor rejection, fixed cell-count limit, or runtime mutation firewall.

## Goals / Non-Goals

**Goals:**

- Give the map agent a durable preservation-first mental model for editing existing maps.
- Teach a contextual choice between local incremental editing and generation/rebuild work.
- Make the agent explain its intended delta and preserved context before it selects an authoring approach.
- Teach it to use nearby observed map facts to resolve collisions or ambiguity without deleting unrelated content.
- Exercise the guidance with representative prompt-level regression cases.

**Non-Goals:**

- Add hard-coded limits, execution-time blocking rules, or automatic rollback.
- Ban builders, layouts, or code-driven map authoring.
- Change Godot TileMap serialization formats or introduce a new map-editing API.
- Guarantee correctness when the editor, tool output, or user intent is ambiguous.

## Decisions

### Teach an editing hierarchy, not a universal procedure

The map agent will learn to classify intent before it chooses an implementation. Verbs such as extend, add, remove, repair, and move a small named structure are strong evidence of a local incremental edit. Explicit generation, regeneration, migration, procedural layout, or an already dedicated generated target are evidence for a builder. The guidance will present these as default interpretations with reasons and counterexamples, not absolute keyword rules.

This is preferred to a blanket ban on builders because the same agent must still support deliberate procedural authoring.

### Treat observed authored content as the canonical baseline

For a local request, the existing scene and bounded map observations are canonical. A newly created JSON layout is only a proposed representation, never evidence that unobserved map content is safe to replace. The agent will be taught the preservation question: if its plan requires recreating existing cells that the user did not ask to change, it is no longer a local edit and must reconsider the approach.

This is preferred to treating a builder's declared `generated_target_is_generated_only` property as sufficient evidence, because the property is a plan assertion rather than an observation of the existing scene.

### Add a delta-and-preservation narration checkpoint

Before a mutating map proposal, the agent will narrate: the target and layer, the requested delta, the local map facts it relied on, the surrounding structures it intends to preserve, and why the selected approach only changes the requested area. This is a reasoning aid visible to the user, not a schema validator.

The narration converts an implicit leap from "add ten tiles" to "rebuild a layer" into a reviewable choice and gives the model a concise self-critique step.

### Teach by contrast and recovery-oriented review

The role prompt and bundled skill will include worked contrasts:

- extending a floor beside a tower is a local edit that retains the tower and presents local alternatives if it conflicts;
- recreating a whole layer from partial observations is a migration/rebuild, not an extension;
- a builder is appropriate only when the task is explicitly generative or targets a known dedicated generated area.

After an edit, the agent will review the requested area plus its immediate surroundings and report both the intended delta and any unrelated visual difference it observes. This remains an honest reasoning and reporting practice, not a claim that screenshots prove every runtime property.

### Preserve the existing screenshot verification work as feedback

Focused screenshot and evidence work remains useful because it gives the model better observations for its preservation review. It is not positioned as the primary prevention mechanism; the primary change is choosing the correct editing abstraction before mutation.

## Risks / Trade-offs

- [Natural-language requests can be genuinely ambiguous] → The guidance teaches the agent to state the competing local interpretations and ask the user only when the difference materially changes the map.
- [A model can still ignore instructions] → Keep the reasoning compact, use contrastive examples near the map-agent role, and add regression fixtures that detect regressions in the selected strategy and explanation.
- [Local edits may not have a suitable authoring interface] → The agent explains that limitation and proposes an incremental authoring path; it does not silently convert the request into a full migration.
- [Verbose reasoning can slow simple tasks] → Require a short preservation summary rather than a long planning essay.

## Migration Plan

1. Update the map-agent role and bundled map-authoring skill with the preservation-first reasoning model and examples.
2. Add prompt/context tests covering local extension, explicit generation, and collision-with-existing-structure cases.
3. Review the resulting agent output with the screenshot-verification change enabled.
4. Roll back by reverting the prompt and test changes; this change introduces no persisted project-data migration.

## Open Questions

- Which existing map-edit test harness best captures agent strategy selection without overfitting to exact wording?
- Should the preservation review be surfaced as a distinct transcript entry or remain part of the agent's normal plan and completion text?
