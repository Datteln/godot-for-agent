## ADDED Requirements

### Requirement: Empty search results explain the boundary and stop guidance
When a `tool.search` query matches no tool in the visible set and no hidden tool in the full registry, the result MUST include the sorted list of visible tool names, an advisory stating that further searching will not produce new tools, and MUST NOT appear to leave the search space open. When the failure streak for the same session and agent reaches the configured threshold, the advisory MUST escalate to a hard stop instruction (`search_stop`).

#### Scenario: Query matches nothing anywhere
- **WHEN** a coordinator searches for `create_plan` while that tool is absent from both the visible set and the registry
- **THEN** the result includes `visible_tools` with the current tool names, an `advisory` stating no tool matches anywhere and further searching is futile, and `search_stop` remains false for the first two misses

#### Scenario: Repeated empty searches escalate to a hard stop
- **WHEN** the same session and agent records a third consecutive empty match
- **THEN** the result sets `search_stop` true with an explicit instruction to stop searching and either use the visible tools or report the missing capability to the user

#### Scenario: A successful match resets the streak
- **WHEN** a later search in the same session and agent returns at least one match
- **THEN** the empty-match streak counter resets, and subsequent empty results start from one again

### Requirement: Agent prompts forbid endless tool re-searching
Agent definitions that rely on `tool.search` MUST instruct the model to stop re-searching after a bounded number of empty matches, state the missing tools to the user, and continue with the tools actually available instead of repeating the same objective with different keywords.

#### Scenario: Coordinator prompt includes the anti-spin clause
- **WHEN** a coordinator agent definition is loaded
- **THEN** its system prompt contains an explicit rule: stop after two consecutive empty `tool.search` results, explain the missing tool, and proceed with available tools or ask the user

#### Scenario: Tool-based agents carry the same clause
- **WHEN** any agent definition that lists `tool.search` in its tools is loaded (advisor, map-agent, programming-agent, scene-agent, resource-agent, map-planner-agent, map-reader-agent, map-reviewer-agent, map-validator-agent)
- **THEN** its system prompt contains the same bounded-retry rule