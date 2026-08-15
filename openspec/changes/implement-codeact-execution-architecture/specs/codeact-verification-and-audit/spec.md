## ADDED Requirements

### Requirement: Each write has diff evidence and target-matched validation
After every `project.edit`, shell call, or temporary script that can alter the project, the system MUST collect the current task diff and run or explicitly report the unavailability of the minimal validation applicable to the modified object. Scripts and tests MUST use an applicable test, static check, or headless entry; scenes MUST load the target `PackedScene`; resources MUST use `ResourceLoader.load` with a type assertion; maps MUST run their project range, semantic, and target-region validators.

#### Scenario: Scene is changed
- **WHEN** a task changes a `.tscn` file
- **THEN** the result includes task diff evidence and a target-scene load validation outcome rather than an unrelated editor launch result

### Requirement: Map validation repairs or preserves failed work transparently
Every map write, including writes caused by shell or temporary scripts, MUST run map range and semantic validation. The system MUST return a failed report to the same map agent for bounded repair retries. If retries are exhausted, the task is cancelled, or repair cannot continue, it MUST end as `failed_validation`, retain the visible current diff, and MUST NOT report success or automatically roll the diff back.

#### Scenario: Map scope validator fails
- **WHEN** a map agent's write violates target-region validation and retry budget remains
- **THEN** the agent receives the typed failure report and may modify the map before a new validation attempt

#### Scenario: Map repair budget is exhausted
- **WHEN** the final map validation attempt fails
- **THEN** the task ends as `failed_validation` with the validator evidence and retained task diff visible to the user

### Requirement: CodeAct actions produce bounded auditable evidence
For each action, the system MUST associate audit data with `task_execution_id`: edit target and before/after digests; temporary-script hash, path, command, exit code and artifacts; shell/headless parameter summary, cwd, timeout, exit code and output artifacts; Editor method, approval and artifact; validator version, errors and retry count; and final completion summary. Persisted complete artifacts MUST be subject to configured size limits and sensitive-content filtering.

#### Scenario: Shell output contains a credential-like string
- **WHEN** a command produces output selected for long-term audit
- **THEN** the artifact is processed by the configured size and sensitive-content policy before persistence

