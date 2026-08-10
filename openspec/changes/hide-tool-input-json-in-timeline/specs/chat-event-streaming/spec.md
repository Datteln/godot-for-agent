## MODIFIED Requirements

### Requirement: Tool previews and all visible nodes use the renderer registry
Tool calls, results, diffs, reasoning disclosures, Markdown, system messages, errors, and finals MUST store serializable content blocks, render descriptors, or artifact references. TimelineStore MUST NOT contain a prebuilt Godot `Control`, and no visible node may bypass `ChatItemRendererRegistry`. Markdown, truncation, copy text, theme, indentation, lifecycle status, and status-color policy MUST be shared by live and historical rendering. Timeline rendering of `json` and `list` render-kind tool items MUST NOT display the raw tool-input JSON; it SHALL show the tool title and lifecycle status only. Approval and confirmation previews MAY continue to display full input detail. Structured `diff`, `map`, and `run` previews are unaffected.

#### Scenario: Tool diff appears live and in history
- **WHEN** the same tool result is rendered first from a WebSocket event and later from canonical history
- **THEN** both use the same descriptor, renderer, structure, copy text, theme policy, and visible diff semantics without reusing a prebuilt node

#### Scenario: Unknown mutation is received
- **WHEN** projection produces an unknown mutation kind, invalid lifecycle transition, ambiguous item identity, or epoch mismatch
- **THEN** validation fails closed before TimelineStore or any UI node changes, and unvalidated content is not rendered

#### Scenario: ChatPanel attempts direct insertion
- **WHEN** release architecture checks inspect ChatPanel and VirtualScroller integration
- **THEN** ChatPanel has no direct append or external-node insertion path and VirtualScroller subscribes only to TimelineStore mutations

#### Scenario: Parameterless front tool in timeline
- **WHEN** a `json` or `list` render-kind tool item is rendered in the Timeline
- **THEN** the node shows only the tool title and lifecycle status, with no raw input JSON block regardless of whether the input is empty

#### Scenario: Confirmation preview retains input detail
- **WHEN** a `needs_confirm` tool is presented in the approval dialog
- **THEN** the preview continues to display the full input detail needed for the approval decision

#### Scenario: Structured previews unchanged
- **WHEN** `diff`, `map`, or `run` render-kind tool items are rendered live or from history
- **THEN** their computed structured previews render exactly as before
