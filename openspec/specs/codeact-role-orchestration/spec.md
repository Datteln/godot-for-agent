# codeact-role-orchestration Specification

## Purpose

Define role-scoped authority over the unified CodeAct protocol and coordinator serialization of project writers.

## Requirements

### Requirement: Roles share a protocol but receive scoped authority
Programming, map, and scene agents MUST use the unified CodeAct protocol with role-scoped default write ranges; advisor MUST receive only read/search/diff capabilities and no write-capable Shell; coordinator MUST default to delegation, read-only synthesis, and no workspace-writing Shell.

#### Scenario: Advisor investigates a change
- **WHEN** an advisor requests a project operation
- **THEN** it can read, search, and inspect Git diff but cannot invoke `project.edit`, headless mutation, or a write-capable shell command

### Requirement: Coordinator serializes project writers
For one project, the coordinator MUST allow at most one active write-capable agent at a time. It MAY run read-only subtasks concurrently and MUST begin the next writer only after the current writer completes or pauses.

#### Scenario: Code and map writes are both planned
- **WHEN** a plan contains a programming write outcome and a map write outcome for the same project
- **THEN** the coordinator runs their write-capable phases serially while allowing independent read-only analysis in parallel