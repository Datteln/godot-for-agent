## Context

Map workers are multi-turn agents. They first call tools to gather canonical map facts, then finish with a text response that the orchestration service parses as `map_worker_result_v1`. Today the Frame contract carries only the schema name and a few scope values. The worker prompt does not contain the complete result shape, the provider request has no structured-response contract, and the inherited effort normally produces the final response at temperature `0.7`.

The local parser accepts a plain JSON object or a fenced object and then checks a separately maintained set of required fields plus Frame-specific stage, target, revision, worker, and transition rules. On failure, the runtime immediately replaces the result with a fail-closed partial object and pops the child Frame. The retry counter is semantic task state, but no corrective model call occurs inside that same Frame; a parent can consequently create multiple replacement workers that each fail on attempt one.

The change spans the schema definition, Frame construction, QueryEngine final-turn control, LLM provider abstraction, orchestration finish path, reducer-owned retry state, delegate artifacts, and tests. It must preserve providers that do not support native JSON Schema and must not constrain intermediate tool-call turns as though they were final results.

## Goals / Non-Goals

**Goals:**

- Make one canonical JSON Schema the source for model constraints, local validation, required fields, and safe prompt summaries.
- Specialize that schema with the frozen Frame contract before the final worker turn.
- Send the specialized contract to the LLM through the strongest configured provider mode while retaining a compatible prompt-only fallback.
- Keep structured correction in the same Frame so already gathered facts and tool results remain available.
- Bound both local formatting correction and task-level semantic repetition.
- Preserve conservative, completion-blocking repair after correction is exhausted.
- Produce safe diagnostics that identify response mode, schema version, attempt, and validation category without logging untrusted raw output by default.

**Non-Goals:**

- Changing public HTTP responses, Godot map tool schemas, map-generation algorithms, or Completion Gate evidence rules.
- Forcing structured-response mode during worker tool-call turns.
- Trusting provider-side validation without local validation.
- Persisting full raw model output in ordinary logs or delegate artifacts.
- Replacing provider timeout/fallback behavior or allowing unlimited correction attempts.

## Decisions

### 1. Define one canonical base schema and derive every representation

`map_contracts.py` will own a versioned `map_worker_result_v1` JSON Schema. Required-field checks, field types, stage enums, validation subfields, and list types are derived from it. Code will not maintain a separate hand-written required-field set.

Before a worker's final turn, the runtime creates a per-Frame schema by copying the base schema and adding contract-derived constraints such as stage, worker instance, canonical target, concrete revision, and allowed next stages. Unknown scope values are represented by the base type contract rather than a false constant.

This is preferred over maintaining a prompt template, parser field set, and provider schema separately because schema drift caused the current failure to be difficult for the model to correct.

### 2. Deliver the specialized schema only on the final text-only turn

Tool-gathering turns continue to use the worker's normal tools, effort, and sampling policy. Once the runtime knows the required facts are available or the turn budget requires a structured partial result, it marks the Frame `force_text_only` and attaches a structured-response contract for the next provider call.

The provider request uses one configured mode:

1. `json_schema`: pass the full specialized schema as a strict native response format.
2. `json_object`: request one JSON object and include the specialized schema in the runtime system contract.
3. `prompt_only`: include the specialized schema and a minimal stage-correct example in the runtime system contract.

In native `json_schema` mode the ordinary prompt receives a compact human-readable summary rather than a second verbose schema copy. The server-side constant name is not exposed as source code; its specialized wire representation is sent. All modes still run identical local validation.

This is preferred over sending the schema on every turn because some providers combine tools and response formatting differently, and forcing the final result schema during a tool turn can suppress required tool calls.

### 3. Negotiate provider support explicitly and fail over narrowly

The LLM provider abstraction will accept an optional response contract and deterministic overrides. Model/provider capability configuration selects `json_schema`, `json_object`, or `prompt_only`; it is not inferred from a model-name substring.

If a provider rejects only the response-format feature before producing content, the backend retries the same final turn once using the next supported mode. It does not replay tool turns, switch task identity, or treat malformed model content as proof that the capability itself is unsupported. Existing primary/fallback model behavior remains separate.

This is preferred over assuming OpenAI-compatible endpoints implement identical structured-output extensions.

### 4. Make the final structured turn deterministic

Final structured generation is tool-free, uses temperature `0`, and uses a bounded thinking policy suitable for serialization. Earlier reasoning and tool turns continue to use the worker's configured effort.

The override belongs to the call request, not a mutation of the Agent definition, so parallel Frames and later tasks cannot inherit it accidentally.

### 5. Correct invalid output inside the same Frame

When final output fails JSON Schema or frozen Frame validation, the finish path does not pop the child Frame. It records a frame-local attempt, appends a safe correction message containing the stable category, missing or invalid fields, schema version, and immutable Frame constraints, then invokes another text-only deterministic final turn using the same messages and tool results.

Raw invalid output is not echoed into the correction message. A successful correction replaces the provisional invalid output and publishes one normal delegate artifact. Local correction is bounded independently from semantic task retries.

After the local bound is exhausted, the existing conservative repair produces a typed partial result with `validation.passed=false` and `completion_allowed=false`. The task-level recovery path records the semantic failure and may pause or schedule a semantically justified replacement, but repeated replacement workers share the task-level convergence budget.

This is preferred over immediate repair because immediate repair loses usable facts, and over immediate replacement because a new worker repeats expensive reads without receiving the validation error.

### 6. Keep retry identities separate but correlated

Frame-local structured attempts are keyed by Frame contract identity and reset only when that Frame terminates. Task-level semantic attempts remain reducer-owned and are keyed by task lineage, stage, target, revision, operation, and structured error category. Logs and typed results carry both identities.

A successful same-Frame correction closes the local formatting incident without pretending the initial output was valid. Exhaustion increments the semantic counter once for the failed Frame, not once per local corrective call.

### 7. Preserve fail-closed validation and safe observability

Provider success never bypasses local Schema and Frame validation. Invalid results cannot update facts, checkpoints, stages, blockers, validation, or Completion Gate state until corrected. Exhausted conservative repair remains explicitly non-completing.

Diagnostics record schema version, response mode, model, final-turn overrides, finish reason, raw length and digest, validation category, parse offset where available, local attempt, and semantic attempt. Full raw content is available only through an explicitly enabled, access-controlled, short-lived diagnostic facility; it is absent from normal logs and artifacts.

## Risks / Trade-offs

- **[Provider feature variance]** A nominally OpenAI-compatible model may reject or partially implement `json_schema`. → Use explicit capability configuration, narrow response-mode fallback, and provider contract tests.
- **[Schema token cost]** Prompt-only fallback can add a large schema to every corrective turn. → Use native response formatting where available and a compact, stable schema representation with a minimal example in fallback mode.
- **[Retry latency]** Same-Frame correction adds another model call. → Restrict it to the final text-only phase, cap attempts, and avoid repeating map reads.
- **[Over-constrained unknown scope]** A Frame created before target or revision discovery cannot truthfully use a `const`. → Specialize only known immutable values and require discovered values through normal schema and local contract validation.
- **[Provider says valid, local validator disagrees]** Provider implementations may accept a weaker schema dialect. → Treat local validation as authoritative and run the same correction path.
- **[Duplicate accounting]** Counting every local correction as semantic no-progress would pause productive tasks too early. → Maintain separate counters and increment semantic failure only when a Frame exhausts correction.
- **[Persisted Frame compatibility]** Older Frames lack structured-attempt metadata. → Default missing metadata to zero and derive the response contract when the Frame next enters text-only completion.

## Migration Plan

1. Add the canonical schema, local validator, specialization builder, and tests without changing provider requests.
2. Route existing required-field and Frame validation through the canonical schema.
3. Add frame-local corrective retry behind a service feature flag while retaining the current repair fallback.
4. Extend the provider abstraction and test doubles with optional response contracts and deterministic per-call overrides.
5. Enable `prompt_only` by default, then enable `json_object` or `json_schema` per verified provider/model capability.
6. Observe correction success, exhaustion, latency, and provider fallback metrics before removing the feature flag.

Rollback disables native response formatting and same-Frame correction, returning to prompt-only generation plus the existing conservative repair. The canonical local validator remains safe to keep because it does not alter public persistence formats.

## Open Questions

- Which deployed model endpoints pass strict `json_schema` and `json_object` compatibility tests, and which must initially remain `prompt_only`?
- Should the default local corrective bound be one retry or two? The implementation should make it configurable while tests cover exhaustion deterministically.
- Does the existing Session persistence need to retain a Frame-local attempt across process restart, or may restart convert an in-progress correction into one typed resumable task attempt?
