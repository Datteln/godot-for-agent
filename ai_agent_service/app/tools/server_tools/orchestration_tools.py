"""Core orchestration tools: delegation and planning.

These tools execute on the server side through frame-level routing
(`app.orchestrator.map_turn.response_routing`, `delegation*.py`) and only
need their schemas registered — they never carry a ``handler``.  They were
originally registered under ``front_tools`` and got swept away by the
CodeAct front-tool disable switch; this module keeps them on the server
side so that roles declaring them always resolve them.
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import ToolDef, register


def _object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


def _worker_spec_schema() -> dict[str, Any]:
    """返回动态地图 worker 的参数 schema。"""
    return _object_schema(
        {
            "name": {"type": "string"},
            "objective": {"type": "string"},
            "mode": {
                "type": "string",
                "enum": [
                    "read_only",
                    "propose_only",
                    "write_one_batch",
                    "review_only",
                    "repair_propose",
                    "repair_write_one_batch",
                ],
            },
            "skills": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional additional map skills. For planner modes the service derives and "
                    "adds required pipeline skills from canonical operation names."
                ),
            },
            "operations": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Canonical registered tool names for this worker stage; planner operations "
                    "deterministically select required planning skills."
                ),
            },
            "constraints": {
                "type": "array",
                "items": _object_schema(
                    {
                        "validator": {"type": "string"},
                        "required_args": {"type": "object"},
                    },
                    ["validator"],
                ),
            },
            "output_schema": {"type": "string", "enum": ["map_worker_result_v1"]},
            "authoritative_snapshot": {
                "type": "object",
                "description": (
                    "Legacy single-context planner input; runtime migrates it into a one-entry "
                    "planning_context_bundle."
                ),
                "properties": {
                    "artifact_ref": {"type": "string"},
                    "snapshot_id": {"type": "string"},
                    "digest": {"type": "string"},
                    "target_path": {"type": "string"},
                    "map_layer": {"type": "integer"},
                    "map_revision": {"type": "integer"},
                    "execution_eligible": {"type": "boolean"},
                },
                "required": [
                    "artifact_ref",
                    "snapshot_id",
                    "digest",
                    "target_path",
                    "map_layer",
                    "map_revision",
                ],
            },
            "planning_context_bundle": {
                "type": "object",
                "description": (
                    "Runtime-owned planner reference contexts; entries may use different "
                    "targets, layers, regions, and source revisions."
                ),
                "properties": {
                    "bundle_id": {"type": "string"},
                    "required_roles": {"type": "array", "items": {"type": "string"}},
                    "contexts": {
                        "type": "array",
                        "items": _object_schema(
                            {
                                "context_id": {"type": "string"},
                                "semantic_role": {"type": "string"},
                                "artifact_ref": {"type": "string"},
                                "digest": {"type": "string"},
                                "provenance": {"type": "object"},
                                "target_path": {"type": "string"},
                                "map_layer": {"type": "integer"},
                                "region": {"type": "object"},
                                "source_revision": {"type": "integer"},
                                "fact_fields": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "fresh": {"type": "boolean"},
                            },
                            [
                                "context_id",
                                "semantic_role",
                                "artifact_ref",
                                "digest",
                            ],
                        ),
                    },
                },
                "required": ["contexts"],
            },
            "required_context_roles": {
                "type": "array",
                "items": {"type": "string"},
            },
            "approved_batch": {
                "type": "object",
                "description": (
                    "Required for write modes; immutable planner/validator artifact identity "
                    "for the exact target and revision."
                ),
                "properties": {
                    "artifact_ref": {"type": "string"},
                    "batch_id": {"type": "string"},
                    "target_path": {"type": "string"},
                    "map_layer": {"type": "integer"},
                    "map_revision": {"type": "integer"},
                    "snapshot_id": {"type": "string"},
                    "snapshot_digest": {"type": "string"},
                    "batch_fingerprint": {"type": "string"},
                    "execution_operations": {
                        "type": "array",
                        "items": _object_schema(
                            {
                                "operation_id": {"type": "string"},
                                "target_path": {"type": "string"},
                                "map_layer": {"type": "integer"},
                                "expected_revision": {"type": "integer"},
                                "write_payload": {"type": "object"},
                                "artifact_ref": {"type": "string"},
                                "batch_id": {"type": "string"},
                            },
                            [
                                "operation_id",
                                "target_path",
                                "map_layer",
                                "expected_revision",
                                "write_payload",
                            ],
                        ),
                    },
                },
                "required": [
                    "artifact_ref",
                    "batch_id",
                    "target_path",
                    "map_layer",
                    "map_revision",
                    "snapshot_id",
                    "snapshot_digest",
                    "batch_fingerprint",
                ],
            },
            "stage_id": {"type": "string"},
            "max_turns": {"type": "integer", "minimum": 1, "maximum": 12},
        },
        ["name", "objective", "mode", "operations", "output_schema"],
    )


def register_orchestration_tools() -> None:
    """注册服务端执行的委托与计划工具 schema。"""
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