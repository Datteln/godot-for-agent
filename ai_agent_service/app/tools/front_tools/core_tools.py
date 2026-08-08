"""Core tool registrations for the front-facing Godot tools."""

from __future__ import annotations

from app.tools.registry import ToolDef

from app.tools.front_tools._shared import _object_schema, _worker_spec_schema, register


def register_core_tools() -> None:
    register(
        ToolDef(
            name="delegate",
            domain="core",
            side="server",
            is_read_only=True,
            is_concurrency_safe=False,
            render_kind="json",
            schema={
                "name": "delegate",
                "description": (
                    "Delegate a focused subtask to a specialist agent. "
                    "Must be the only tool call in the assistant turn."
                ),
                "parameters": _object_schema(
                    {
                        "agent": {
                            "type": "string",
                            "description": "Specialist agent name, e.g. programming-agent. For dynamic map workers use map-worker.",
                        },
                        "task": {
                            "type": "string",
                            "description": "Focused task for the child agent.",
                        },
                        "worker_spec": {
                            "description": (
                                "Optional dynamic map worker spec. Only map-agent may use this. "
                                "Use agent=map-worker (or legacy agent=map-agent) with this field; "
                                "do not combine it with a permanent specialist agent name. "
                                "Fields include name, objective, mode, operations, constraints, "
                                "skills, output_schema, stage_id, max_turns."
                            ),
                            **_worker_spec_schema(),
                        },
                    },
                    ["agent", "task"],
                ),
            },
        )
    )

    register(
        ToolDef(
            name="delegate_many",
            domain="core",
            side="server",
            is_read_only=True,
            is_concurrency_safe=False,
            render_kind="json",
            schema={
                "name": "delegate_many",
                "description": (
                    "Delegate multiple independent subtasks to specialist agents. "
                    "The service executes them as isolated child frames and returns one combined result. "
                    "Must be the only tool call in the assistant turn."
                ),
                # 子任务按数组顺序逐条执行（非并行），每条在独立 child frame 中运行；
                # 前一条失败时后续任务仍会继续，最终合并返回。
                "parameters": _object_schema(
                    {
                        "tasks": {
                            "type": "array",
                            "description": "List of subtasks, each with agent and task.",
                            "items": _object_schema(
                                {
                                    "agent": {"type": "string"},
                                    "task": {"type": "string"},
                                    "plan_step_id": {
                                        "type": "string",
                                        "description": "Stable id returned by create_plan.",
                                    },
                                    "scheduler_inputs": {
                                        "type": "object",
                                        "description": (
                                            "Typed predecessor outputs bound by the scheduler. "
                                            "Callers must not synthesize this field."
                                        ),
                                    },
                                    "depends_on": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Predecessor task ids interpreted only by the scheduler.",
                                    },
                                    "input_bindings": {
                                        "type": "array",
                                        "items": {"type": "object"},
                                    },
                                    "expected_result_schema": {
                                        "type": "object",
                                    },
                                    "worker_spec": {
                                        "description": "Optional dynamic map worker spec, allowed only for map-agent tasks.",
                                        **_worker_spec_schema(),
                                    },
                                },
                                ["agent", "task"],
                            ),
                        }
                    },
                    ["tasks"],
                ),
            },
        )
    )

    register(
        ToolDef(
            name="create_plan",
            domain="core",
            side="server",
            is_read_only=True,
            is_concurrency_safe=False,
            render_kind="json",
            schema={
                "name": "create_plan",
                "description": (
                    "Produce a structured execution plan for a complex multi-step task and notify the "
                    "user via the event stream. Must be the only tool call in the assistant turn. "
                    "After this returns successfully, immediately call delegate_many with the returned "
                    "tasks to start executing the plan."
                ),
                "parameters": _object_schema(
                    {
                        "summary": {
                            "type": "string",
                            "description": "One-sentence overview of the plan.",
                        },
                        "steps": {
                            "type": "array",
                            "description": "Ordered list of plan steps.",
                            "items": _object_schema(
                                {
                                    "id": {
                                        "type": "string",
                                        "description": (
                                            "Optional stable step id; omitted ids are assigned "
                                            "deterministically as step-N."
                                        ),
                                    },
                                    "title": {"type": "string", "description": "Short step title."},
                                    "agent": {
                                        "type": "string",
                                        "description": "Specialist agent name for this step, e.g. programming-agent.",
                                    },
                                    "task": {
                                        "type": "string",
                                        "description": (
                                            "Specific task description delegated to the agent; should "
                                            "include concrete file paths and key operations since it is "
                                            "shown directly to the user."
                                        ),
                                    },
                                    "estimated_complexity": {
                                        "type": "string",
                                        "enum": ["low", "medium", "high"],
                                        "description": "Optional estimated complexity for this step.",
                                    },
                                    "depends_on": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Stable predecessor step ids.",
                                    },
                                    "input_bindings": {
                                        "type": "array",
                                        "description": (
                                            "Typed bindings from predecessor results into this step."
                                        ),
                                        "items": _object_schema(
                                            {
                                                "name": {"type": "string"},
                                                "source_step_id": {"type": "string"},
                                                "source_path": {"type": "string"},
                                                "required": {"type": "boolean"},
                                            },
                                            ["name", "source_step_id"],
                                        ),
                                    },
                                    "expected_result_schema": {
                                        "type": "object",
                                        "description": (
                                            "JSON-schema fragment used to validate the terminal result."
                                        ),
                                    },
                                    "owner_agent": {
                                        "type": "string",
                                        "description": (
                                            "Domain owner agent for this macro outcome (macro_v2). "
                                            "Aliases `agent`; preferred for domain-owned steps."
                                        ),
                                    },
                                    "domain": {
                                        "type": "string",
                                        "enum": ["map", "code", "resource", "scene"],
                                        "description": (
                                            "Domain of this macro outcome. Required for macro_v2 steps."
                                        ),
                                    },
                                    "objective": {
                                        "type": "string",
                                        "description": (
                                            "Domain-owned outcome objective (macro_v2). Aliases `task`. "
                                            "Must not encode specialist-internal stages, tools, or "
                                            "worker_spec; such fields are rejected."
                                        ),
                                    },
                                    "acceptance_criteria": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Acceptance criteria the owner must satisfy.",
                                    },
                                    "predecessor_bindings": {
                                        "type": "array",
                                        "description": (
                                            "Declared predecessor owner-publication fields or artifact "
                                            "locators bound into this owner's input contract."
                                        ),
                                        "items": _object_schema(
                                            {
                                                "name": {"type": "string"},
                                                "source_step_id": {"type": "string"},
                                                "source_path": {"type": "string"},
                                                "required": {"type": "boolean"},
                                            },
                                            ["name", "source_step_id"],
                                        ),
                                    },
                                    "display_milestones": {
                                        "type": "array",
                                        "description": (
                                            "Display-only milestones for UI; never executable scheduler "
                                            "nodes and never assigned Frame or tool authority."
                                        ),
                                        "items": _object_schema(
                                            {
                                                "id": {"type": "string"},
                                                "title": {"type": "string"},
                                                "kind": {"type": "string"},
                                            },
                                            ["id", "title"],
                                        ),
                                    },
                                },
                                ["title", "agent", "task"],
                            ),
                        },
                    },
                    ["summary", "steps"],
                ),
            },
        )
    )
