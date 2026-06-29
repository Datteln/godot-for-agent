"""Front tool definitions.

These tools are executed by the Godot editor plugin, not by the Python service.
The service still owns their schema, risk metadata, path arguments, and permission
decisions, so frontmatter/skills cannot grant new abilities.
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


def register_front_tools() -> None:
    """Register all built-in Godot-front tools."""
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
                            "description": "Specialist agent name, e.g. programming-agent.",
                        },
                        "task": {
                            "type": "string",
                            "description": "Focused task for the child agent.",
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
                "parameters": _object_schema(
                    {
                        "tasks": {
                            "type": "array",
                            "description": "List of subtasks, each with agent and task.",
                            "items": _object_schema(
                                {
                                    "agent": {"type": "string"},
                                    "task": {"type": "string"},
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
                                    "depends_on": {
                                        "type": "array",
                                        "items": {"type": "integer"},
                                        "description": "Optional 1-based indices of steps this step depends on.",
                                    },
                                    "estimated_complexity": {
                                        "type": "string",
                                        "enum": ["low", "medium", "high"],
                                        "description": "Optional estimated complexity for this step.",
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
    register(
        ToolDef(
            name="read_class_docs",
            domain="program",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "read_class_docs",
                "description": (
                    "Read real Godot ClassDB or script-class signatures from the editor. "
                    "Use before generating code that calls Godot APIs."
                ),
                "parameters": _object_schema(
                    {
                        "class_name": {
                            "type": "string",
                            "description": "Godot class name, for example CharacterBody2D.",
                        }
                    },
                    ["class_name"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="read_scene_tree",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "read_scene_tree",
                "description": "Read the currently edited scene tree from the Godot editor.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="propose_script_edit",
            domain="program",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="diff",
            path_args=["path"],
            schema={
                "name": "propose_script_edit",
                "description": (
                    "Replace a text script/resource file after user preview confirmation. "
                    "The path must be relative to project root, for example scripts/player.gd."
                ),
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "Relative file path."},
                        "content": {
                            "type": "string",
                            "description": "Complete replacement file content.",
                        },
                    },
                    ["path", "content"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="propose_tests",
            domain="program",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="diff",
            path_args=["path"],
            schema={
                "name": "propose_tests",
                "description": (
                    "Create or replace a Godot test file after user preview confirmation. "
                    "Use for GUT/WAT or project-local test scripts."
                ),
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "Relative test file path."},
                        "content": {
                            "type": "string",
                            "description": "Complete replacement test file content.",
                        },
                        "framework": {
                            "type": "string",
                            "description": "Test framework hint, for example gut or wat.",
                        },
                    },
                    ["path", "content"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="apply_text_edit",
            domain="program",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="diff",
            path_args=["path"],
            schema={
                "name": "apply_text_edit",
                "description": (
                    "Apply a precise find-and-replace edit to an existing text file, instead of rewriting "
                    "the whole file with propose_script_edit. `old_string` must be copied verbatim from a "
                    "previous read_file/read_script result for this exact path (calling this before ever "
                    "reading the file is rejected). old_string must match exactly once unless replace_all "
                    "is set; if it matches zero or multiple times, include more surrounding context instead."
                ),
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "Relative file path."},
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to find, copied verbatim from a prior read.",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text.",
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every occurrence instead of requiring a unique match. Defaults to false.",
                        },
                    },
                    ["path", "old_string", "new_string"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="read_debugger_errors",
            domain="program",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="log",
            schema={
                "name": "read_debugger_errors",
                "description": "Read recent editor debugger/runtime errors captured by the Godot frontend.",
                "parameters": _object_schema(
                    {
                        "max_items": {
                            "type": "integer",
                            "description": "Maximum diagnostic/error items to return.",
                        }
                    },
                ),
            },
        )
    )
    register(
        ToolDef(
            name="read_runtime_state",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "read_runtime_state",
                "description": "Read a bounded, read-only snapshot of editor/runtime state for diagnosis.",
                "parameters": _object_schema(
                    {
                        "max_depth": {
                            "type": "integer",
                            "description": "Maximum scene tree depth to return.",
                        }
                    },
                ),
            },
        )
    )
    register(
        ToolDef(
            name="read_profiler_snapshot",
            domain="program",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "read_profiler_snapshot",
                "description": "Read Godot Performance monitor values for bottleneck diagnosis.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="run_tests",
            domain="program",
            side="front",
            reads_project=True,
            executes_process=True,
            needs_preview=True,
            timeout_ms=120000,
            render_kind="run",
            schema={
                "name": "run_tests",
                "description": (
                    "Run a user-configured, controlled Godot test or headless self-check command. "
                    "The model may choose only the configured kind, never an arbitrary executable."
                ),
                "parameters": _object_schema(
                    {
                        "kind": {
                            "type": "string",
                            "enum": ["project", "headless_scene"],
                            "description": "Configured runner kind.",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Requested timeout; frontend clamps to its local limit.",
                        },
                    },
                    ["kind"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="run_headless_self_test",
            domain="program",
            side="front",
            reads_project=True,
            executes_process=True,
            needs_preview=True,
            timeout_ms=180000,
            render_kind="run",
            schema={
                "name": "run_headless_self_test",
                "description": (
                    "Run the user-configured headless self-test/playtest command and return logs. "
                    "The executable and arguments come only from EditorSettings."
                ),
                "parameters": _object_schema(
                    {
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Requested timeout; frontend clamps to its local limit.",
                        }
                    },
                ),
            },
        )
    )
    register(
        ToolDef(
            name="run_system_command",
            domain="program",
            side="front",
            reads_project=True,
            writes_project=True,
            executes_process=True,
            needs_preview=True,
            timeout_ms=120000,
            render_kind="run",
            schema={
                "name": "run_system_command",
                "description": (
                    "Run a system command after explicit user confirmation. Supports automatic native "
                    "shell selection plus PowerShell, CMD, sh, bash, and zsh when installed. Use this "
                    "for build, test, version-control, and other terminal tasks."
                ),
                "parameters": _object_schema(
                    {
                        "command": {
                            "type": "string",
                            "description": "The exact command text to execute.",
                        },
                        "shell": {
                            "type": "string",
                            "enum": ["auto", "powershell", "pwsh", "cmd", "sh", "bash", "zsh"],
                            "description": "Shell to use. auto selects PowerShell on Windows and sh on Linux/macOS.",
                        },
                        "working_directory": {
                            "type": "string",
                            "description": "Working directory. Defaults to the Godot project root; res:// paths are supported.",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Requested timeout; frontend clamps it to the configured local limit.",
                        },
                    },
                    ["command"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="execute_gd_script",
            domain="program",
            side="front",
            reads_project=True,
            executes_process=True,
            needs_preview=True,
            timeout_ms=60000,
            render_kind="run",
            read_path_args=["path"],
            schema={
                "name": "execute_gd_script",
                "description": (
                    "Run a project-relative .gd file directly with the editor's own Godot executable "
                    "(headless --script) and return its stdout/stderr and exit code. Use this to execute "
                    "one-off GDScript utility/generator scripts, not to launch the game itself. The entry "
                    "script must directly extend SceneTree or MainLoop; EditorScript and Node scripts are "
                    "rejected before launch. Godot ERROR/SCRIPT ERROR output is treated as failure even if "
                    "the process exits with code 0."
                ),
                "parameters": _object_schema(
                    {
                        "path": {
                            "type": "string",
                            "description": (
                                "Project-relative .gd entry script, for example tools/generate_map.gd. "
                                "It must directly extend SceneTree or MainLoop, never EditorScript."
                            ),
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Extra string arguments passed through to the script.",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Requested timeout; frontend clamps it to the configured local limit.",
                        },
                    },
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="git_status",
            domain="program",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="run",
            schema={
                "name": "git_status",
                "description": "Run `git status --porcelain=v1 -b` in the project root and return its output. Fixed, read-only command.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="git_diff",
            domain="program",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="run",
            read_path_args=["path"],
            schema={
                "name": "git_diff",
                "description": "Run `git diff` (optionally --staged, optionally scoped to one path) and return its output. Fixed, read-only command.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "Optional relative path to scope the diff to."},
                        "staged": {"type": "boolean", "description": "Show staged changes instead of the working tree."},
                    },
                ),
            },
        )
    )
    register(
        ToolDef(
            name="export_project",
            domain="program",
            side="front",
            reads_project=True,
            executes_process=True,
            needs_preview=True,
            timeout_ms=600000,
            render_kind="run",
            write_path_args=["output_path"],
            schema={
                "name": "export_project",
                "description": (
                    "Trigger a project export using a configured export preset, via the editor's own Godot "
                    "executable (--export-release/--export-debug). Requires export templates to be installed "
                    "and can take a long time; must be confirmed every time."
                ),
                "parameters": _object_schema(
                    {
                        "preset": {"type": "string", "description": "Export preset name, from list_export_presets."},
                        "output_path": {"type": "string", "description": "Project-relative output file path."},
                        "debug": {"type": "boolean", "description": "Export a debug build instead of release."},
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Requested timeout; frontend clamps it to the configured local limit.",
                        },
                    },
                    ["preset", "output_path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="add_node",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "add_node",
                "description": (
                    "Add a node under a parent in the currently edited scene, with an optional local 2D/3D "
                    "position. Visual leaf nodes (Sprite2D/Sprite3D/AnimatedSprite2D/AnimatedSprite3D/"
                    "MeshInstance3D) render nothing without their content resource, so add_node REQUIRES a "
                    "`texture` (res:// resource path) for those types and rejects them otherwise with "
                    "error_code 'visual_node_missing_resource'. For a finished prop with art, prefer "
                    "instance_scene on a prefab .tscn instead of hand-building an empty Sprite node. When the "
                    "scene has a TileMap, the result includes `placement` with `placed_at_tile` (the tile cell "
                    "the node actually landed on) and `map_tile_bounds` — check placed_at_tile is inside the "
                    "region you intended to populate; a coordinate far outside the map is rejected with "
                    "error_code 'position_off_map'."
                ),
                "parameters": _object_schema(
                    {
                        "parent_path": {
                            "type": "string",
                            "description": "NodePath relative to the edited scene root, or '.' for root.",
                        },
                        "type": {"type": "string", "description": "Node class to instantiate."},
                        "name": {"type": "string", "description": "New node name."},
                        "texture": {
                            "type": "string",
                            "description": (
                                "res:// path to the content resource for a visual leaf node — assigned to "
                                "texture (Sprite2D/Sprite3D), sprite_frames (AnimatedSprite2D/3D) or mesh "
                                "(MeshInstance3D). Required for those types; without it the node is invisible "
                                "and the call is rejected."
                            ),
                        },
                        "position": {
                            "type": "object",
                            "description": "Optional local position relative to the parent: x/y for Node2D, x/y/z for Node3D (z defaults to 0).",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"},
                            },
                            "required": ["x", "y"],
                            "additionalProperties": False,
                        },
                    },
                    ["type", "name"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="set_node_property",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "set_node_property",
                "description": (
                    "Set a property on a node in the currently edited scene. The frontend coerces common "
                    "Godot Variant types from JSON: Vector2/Vector2i use {x,y} or [x,y], Vector3/Vector3i "
                    "use {x,y,z} or [x,y,z], Color uses {r,g,b,a?}, NodePath/StringName use strings, and "
                    "Resource references use {'_resource_path': 'res://...'}."
                ),
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath to the node."},
                        "property": {"type": "string", "description": "Property name."},
                        "value": {
                            "description": (
                                "JSON value to assign. For position/global_position/scale-like properties, "
                                "pass {x,y} for 2D nodes or {x,y,z} for 3D nodes instead of a raw string."
                            )
                        },
                    },
                    ["path", "property", "value"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="delete_node",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "delete_node",
                "description": "Delete a node from the currently edited scene. The scene root cannot be deleted.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath to the node, relative to the scene root."},
                    },
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="reparent_node",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "reparent_node",
                "description": "Move a node to a new parent within the currently edited scene, preserving the node and its children.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath to the node, relative to the scene root."},
                        "new_parent_path": {
                            "type": "string",
                            "description": "NodePath of the new parent, relative to the scene root, or '.' for root.",
                        },
                    },
                    ["path", "new_parent_path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="rename_node",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "rename_node",
                "description": "Rename a node within the currently edited scene. The scene root cannot be renamed.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath to the node, relative to the scene root."},
                        "name": {"type": "string", "description": "New node name."},
                    },
                    ["path", "name"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="instance_scene",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            read_path_args=["scene_path"],
            schema={
                "name": "instance_scene",
                "description": (
                    "Instantiate a .tscn/.scn file as a new child node, with an optional local 2D/3D position. "
                    "When the scene has a TileMap, the result includes `placement` with `placed_at_tile` (the "
                    "tile cell the instance landed on) and `map_tile_bounds` — verify placed_at_tile is inside "
                    "the region you intended; a coordinate far outside the map is rejected with error_code "
                    "'position_off_map'."
                ),
                "parameters": _object_schema(
                    {
                        "parent_path": {
                            "type": "string",
                            "description": "NodePath of the parent, relative to the scene root, or '.' for root.",
                        },
                        "scene_path": {"type": "string", "description": "Relative .tscn/.scn path to instantiate."},
                        "name": {"type": "string", "description": "Optional name override for the new instance root."},
                        "position": {
                            "type": "object",
                            "description": "Optional local position relative to the parent: x/y for a Node2D root, x/y/z for a Node3D root (z defaults to 0).",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"},
                            },
                            "required": ["x", "y"],
                            "additionalProperties": False,
                        },
                    },
                    ["scene_path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="duplicate_node",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "duplicate_node",
                "description": "Duplicate a node and its children, optionally overriding the duplicate's local 2D/3D position.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath to duplicate, relative to the scene root."},
                        "name": {"type": "string", "description": "Optional name override for the duplicate."},
                        "position": {
                            "type": "object",
                            "description": "Optional local position relative to the parent: x/y for Node2D, x/y/z for Node3D (z defaults to 0).",
                            "properties": {
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "z": {"type": "number"},
                            },
                            "required": ["x", "y"],
                            "additionalProperties": False,
                        },
                    },
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="connect_signal",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "connect_signal",
                "description": "Connect a node's signal to a method on another node (or the same node), persisted with the scene.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath of the signal source, relative to the scene root."},
                        "signal": {"type": "string", "description": "Signal name on the source node."},
                        "target_path": {"type": "string", "description": "NodePath of the target, relative to the scene root."},
                        "method": {"type": "string", "description": "Method name on the target node to call."},
                    },
                    ["path", "signal", "target_path", "method"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="disconnect_signal",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "disconnect_signal",
                "description": "Disconnect a previously connected signal between two nodes in the currently edited scene.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath of the signal source, relative to the scene root."},
                        "signal": {"type": "string", "description": "Signal name on the source node."},
                        "target_path": {"type": "string", "description": "NodePath of the target, relative to the scene root."},
                        "method": {"type": "string", "description": "Method name on the target node."},
                    },
                    ["path", "signal", "target_path", "method"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="add_to_group",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "add_to_group",
                "description": "Add a node to a scene group (for batch lookup, collision categorization, etc.).",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath, relative to the scene root."},
                        "group": {"type": "string", "description": "Group name."},
                    },
                    ["path", "group"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="remove_from_group",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "remove_from_group",
                "description": "Remove a node from a scene group.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "NodePath, relative to the scene root."},
                        "group": {"type": "string", "description": "Group name."},
                    },
                    ["path", "group"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="list_node_groups",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_node_groups",
                "description": "List the groups a node currently belongs to.",
                "parameters": _object_schema(
                    {"path": {"type": "string", "description": "NodePath, relative to the scene root."}},
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="list_node_signals",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_node_signals",
                "description": "List the signals a node can emit, for wiring up with connect_signal.",
                "parameters": _object_schema(
                    {"path": {"type": "string", "description": "NodePath, relative to the scene root."}},
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="list_node_methods",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_node_methods",
                "description": "List the public methods a node exposes, for wiring up with connect_signal.",
                "parameters": _object_schema(
                    {"path": {"type": "string", "description": "NodePath, relative to the scene root."}},
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="validate_scene_state",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "validate_scene_state",
                "description": (
                    "Validate the currently edited scene against explicit expectations without modifying it. "
                    "Use after scene-editing tools to verify nodes exist or are absent, node types match, "
                    "properties have expected values, groups are present/absent, and signal connections are "
                    "present/absent. Property values use the same JSON coercion as set_node_property: "
                    "{x,y} for Vector2, {x,y,z} for Vector3, {r,g,b,a?} for Color, and "
                    "{'_resource_path': 'res://...'} for Resource references."
                ),
                "parameters": _object_schema(
                    {
                        "tolerance": {
                            "type": "number",
                            "description": "Numeric tolerance for float, Vector2, Vector3, and Color comparisons. Defaults to 0.001.",
                        },
                        "checks": {
                            "type": "array",
                            "description": "Scene assertions to evaluate against the current edited scene root.",
                            "items": _object_schema(
                                {
                                    "path": {
                                        "type": "string",
                                        "description": "NodePath relative to the edited scene root, or '.' for the root.",
                                    },
                                    "exists": {
                                        "type": "boolean",
                                        "description": "Whether the node should exist. Defaults to true.",
                                    },
                                    "type": {
                                        "type": "string",
                                        "description": "Optional Godot class/type expectation, e.g. Node2D, Area2D, Node3D.",
                                    },
                                    "properties": {
                                        "type": "object",
                                        "description": "Optional property expectations keyed by property name, e.g. {'position': {'x': 10, 'y': 20}}.",
                                    },
                                    "groups": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Groups the node must belong to.",
                                    },
                                    "not_groups": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Groups the node must not belong to.",
                                    },
                                    "signals": {
                                        "type": "array",
                                        "description": "Signal connection expectations for this source node.",
                                        "items": _object_schema(
                                            {
                                                "signal": {"type": "string", "description": "Signal name on the source node."},
                                                "target_path": {
                                                    "type": "string",
                                                    "description": "Target NodePath relative to the scene root. Defaults to the source path.",
                                                },
                                                "method": {"type": "string", "description": "Target method name."},
                                                "connected": {
                                                    "type": "boolean",
                                                    "description": "Whether the connection should exist. Defaults to true.",
                                                },
                                            },
                                            ["signal", "method"],
                                        ),
                                    },
                                },
                                ["path"],
                            ),
                        },
                    },
                    ["checks"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="list_groups",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_groups",
                "description": (
                    "Scan the whole currently edited scene tree and list every group in use, with which "
                    "nodes belong to each. Use list_node_groups instead to query a single node's groups."
                ),
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="get_current_scene_path",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "get_current_scene_path",
                "description": "Get the file path of the scene currently being edited (empty if unsaved/none).",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="save_scene",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "save_scene",
                "description": "Save the currently edited scene to disk, persisting pending in-editor changes.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="list_open_scenes",
            domain="scene",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_open_scenes",
                "description": "List the scene tabs currently open in the editor and which one is active.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="capture_viewport_screenshot",
            domain="scene",
            side="front",
            reads_project=True,
            uses_network=True,
            is_read_only=True,
            write_path_args=["output_path"],
            render_kind="json",
            schema={
                "name": "capture_viewport_screenshot",
                "description": (
                    "Capture the editor's current 2D or 3D viewport as a PNG so the model can see the actual "
                    "result of a map/UI/animation change instead of only reading scene data. When asset "
                    "understanding is configured, the service also sends the screenshot through the multimodal "
                    "asset-understanding model after applying the shared image compression/format conversion. "
                    "By default the viewport camera stays wherever the user last left it in the editor, so a "
                    "screenshot can easily miss the region you just edited. Pass EITHER focus_node_path (any "
                    "Node2D/Node3D path in the edited scene) OR focus_region+target_path (a map cell-coordinate "
                    "rect, same x/y/z/width/height/depth shape used by edit_map/validate_map_region, target_path "
                    "pointing at the TileMapLayer/TileMap/GridMap) to re-center the camera (3D) or pan/zoom the "
                    "2D canvas onto the target before capturing, instead of guessing where the viewport happens "
                    "to be pointed. The result also includes `rendered_nodes` (visual nodes that actually have "
                    "their texture/mesh/sprite_frames set and will draw pixels) and `nodes_missing_visual_resource` "
                    "(Sprite/Mesh nodes that exist but have NO resource and therefore render nothing). Cross-check "
                    "these against what you claim to have added: a tree node appearing in nodes_missing_visual_resource "
                    "means it is invisible despite being in the tree — do not report it as done."
                ),
                "parameters": _object_schema(
                    {
                        "mode": {"type": "string", "enum": ["2d", "3d"], "description": "Which editor viewport to capture."},
                        "viewport_index": {"type": "integer", "description": "3D viewport index, if multiple are open."},
                        "output_path": {
                            "type": "string",
                            "description": "Optional project-relative output path; defaults to a temp user:// location.",
                        },
                        "focus_node_path": {
                            "type": "string",
                            "description": (
                                "Path (relative to the edited scene root) of a Node2D/Node3D to center the "
                                "camera/canvas on before capturing. Mutually exclusive with focus_region; use "
                                "this for a single node (a prop, a sign, a character) rather than a tile region."
                            ),
                        },
                        "focus_region": {
                            "type": "object",
                            "description": (
                                "Map cell-coordinate rect to frame before capturing, e.g. {\"x\":0,\"y\":0,"
                                "\"width\":20,\"height\":10}. Requires target_path to identify the map node. "
                                "Use the same region you just passed to edit_map/validate_map_region so the "
                                "screenshot actually shows what you changed."
                            ),
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer", "description": "3D only."},
                                "width": {"type": "integer"},
                                "height": {"type": "integer"},
                                "depth": {"type": "integer", "description": "3D only."},
                            },
                        },
                        "target_path": {
                            "type": "string",
                            "description": "TileMapLayer/TileMap/GridMap path; required when focus_region is set.",
                        },
                        "focus_margin": {
                            "type": "number",
                            "description": "Padding multiplier around the focus bounds (default 1.3); raise it to zoom out further.",
                        },
                    },
                ),
            },
        )
    )
    register(
        ToolDef(
            name="open_scene",
            domain="scene",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            read_path_args=["path"],
            schema={
                "name": "open_scene",
                "description": (
                    "Switch the editor's currently edited scene to another .tscn/.scn file. "
                    "This discards any unsaved in-editor edits to the scene being left, so it must be "
                    "confirmed every time."
                ),
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "Relative scene path, for example scenes/level_2.tscn."},
                    },
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="bake_navigation_mesh",
            domain="scene",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "bake_navigation_mesh",
                "description": "Bake the navigation mesh/polygon for a NavigationRegion2D or NavigationRegion3D node.",
                "parameters": _object_schema(
                    {"path": {"type": "string", "description": "NodePath to the NavigationRegion2D/3D, relative to the scene root."}},
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="set_project_setting",
            domain="project",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "set_project_setting",
                "description": (
                    "Set or clear a project setting (project.godot), for example an input map action, "
                    "autoload, or rendering option. Pass value=null to clear an override back to default."
                ),
                "parameters": _object_schema(
                    {
                        "key": {
                            "type": "string",
                            "description": "Setting key, for example rendering/textures/canvas_textures/default_texture_filter.",
                        },
                        "value": {"description": "JSON value to assign, or null to clear the override."},
                    },
                    ["key", "value"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="read_project_setting",
            domain="project",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "read_project_setting",
                "description": "Read a single project setting's current value (project.godot).",
                "parameters": _object_schema(
                    {"key": {"type": "string", "description": "Setting key, for example application/run/main_scene."}},
                    ["key"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="list_autoloads",
            domain="project",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_autoloads",
                "description": "List configured autoload singletons (name, path, enabled).",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="add_autoload",
            domain="project",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            read_path_args=["path"],
            schema={
                "name": "add_autoload",
                "description": "Register a script or scene as an autoload singleton.",
                "parameters": _object_schema(
                    {
                        "name": {"type": "string", "description": "Autoload identifier, used as the global singleton name."},
                        "path": {"type": "string", "description": "Relative .gd/.tscn/.cs path to autoload."},
                        "enabled": {"type": "boolean", "description": "Defaults to true."},
                    },
                    ["name", "path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="remove_autoload",
            domain="project",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "remove_autoload",
                "description": "Remove a previously registered autoload singleton.",
                "parameters": _object_schema(
                    {"name": {"type": "string", "description": "Autoload identifier to remove."}},
                    ["name"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="list_input_actions",
            domain="project",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_input_actions",
                "description": "List configured InputMap actions with their deadzone and bound keys/buttons.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="add_input_action",
            domain="project",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "add_input_action",
                "description": (
                    "Create or fully replace an InputMap action's bindings. To add to existing bindings "
                    "instead of replacing them, first read them with list_input_actions and include them "
                    "in keys/mouse_buttons."
                ),
                "parameters": _object_schema(
                    {
                        "action": {"type": "string", "description": "Action name."},
                        "deadzone": {"type": "number", "description": "Defaults to 0.5."},
                        "keys": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Key names parsed by Godot, for example A, Space, Enter, Escape.",
                        },
                        "mouse_buttons": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "enum": ["left", "right", "middle", "wheel_up", "wheel_down", "wheel_left", "wheel_right", "xbutton1", "xbutton2"],
                            },
                        },
                    },
                    ["action"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="remove_input_action",
            domain="project",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "remove_input_action",
                "description": "Remove a previously configured InputMap action.",
                "parameters": _object_schema(
                    {"action": {"type": "string", "description": "Action name to remove."}},
                    ["action"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="list_export_presets",
            domain="project",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "list_export_presets",
                "description": "List configured export presets (name, platform, export_path) from export_presets.cfg.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="describe_tilemap_selection",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "describe_tilemap_selection",
                "description": "Describe the selected TileMapLayer, if any.",
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="describe_map_context",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "describe_map_context",
                "description": (
                    "Read the current scene's editable map context before planning a 2D/3D map task: "
                    "TileMapLayer/legacy TileMap/GridMap nodes, TileSet/MeshLibrary references, "
                    "resource_registry.json status, performance summary, and spatial_index.json summary. Use this as the "
                    "project-recognition step before resolving natural-language resources or choosing "
                    "a target map node."
                ),
                "parameters": _object_schema({}),
            },
        )
    )
    register(
        ToolDef(
            name="plan_map_layout",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "plan_map_layout",
                "description": (
                    "Parse a natural-language 2D/3D map request into a structured MapIntent and generate a "
                    "read-only layout plan: standard layer needs, zones, anchors, required semantic resources, "
                    "missing registry keys, draft edit_map operations, and validation steps. Use before large "
                    "generate/edit/decorate tasks; it never edits the scene."
                ),
                "parameters": _object_schema(
                    {
                        "prompt": {
                            "type": "string",
                            "description": "User's map-editing request in natural language.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "2d", "3d"],
                            "description": "Optional mode override; defaults to auto.",
                        },
                        "task": {
                            "type": "string",
                            "description": "Optional task override such as generate, erase, replace, decorate.",
                        },
                        "theme": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "density": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "seed": {"type": "integer"},
                        "noise": {"type": "boolean"},
                    },
                    ["prompt"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="describe_map_region",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "describe_map_region",
                "description": (
                    "Read the actual tiles/cells already placed in a small region of a 2D TileMapLayer/legacy "
                    "TileMap or 3D GridMap, plus the map node's own local position and tile_size/cell_size. "
                    "Use this before extending or blending with existing terrain/background so new content "
                    "reuses the real source_id/atlas_coords already in use, and before computing world "
                    "coordinates for nodes placed relative to the map, instead of guessing constants. "
                    "For a legacy TileMap, the response also includes a `layers` array (index/name/enabled/"
                    "used_bounds) listing every layer the node actually has — check it and pick the right "
                    "map_layer explicitly; do not assume map_layer 0 is the visible/collidable foreground layer, "
                    "many templates put a non-collidable background/decoration layer at index 0. Each layer's "
                    "`used_bounds` (min_x/max_x/min_y/max_y, empty {} if the layer has no tiles) tells you how "
                    "far that layer's content actually reaches — compare a background/sky/water layer's bounds "
                    "against the foreground layer's to see whether the backdrop has fallen behind before you "
                    "extend the level further. A larger-than-usual region is served whole automatically (the "
                    "response carries `auto_served: true`), so you do NOT need to pre-split typical level-width "
                    "reads yourself. Only a region above the auto-serve ceiling fails with error_code "
                    "'region_too_large', and then it returns `suggested_regions`: smaller pre-split rectangles "
                    "covering the same area — just issue describe_map_region for each."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {
                            "type": "string",
                            "description": (
                                "NodePath relative to the edited scene root. Omit to use the selected map node "
                                "or the only compatible map node in the scene."
                            ),
                        },
                        "map_layer": {
                            "type": "integer",
                            "description": (
                                "Layer index for a legacy TileMap; ignored by TileMapLayer and GridMap. Defaults "
                                "to 0 if omitted, which is not necessarily the foreground/collidable layer — "
                                "check the `layers` field in a prior response before assuming."
                            ),
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="plan_map_algorithms",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "plan_map_algorithms",
                "description": (
                    "Build a reusable read-only algorithm plan for map generation/editing using the preferred "
                    "general stack: zone planning, Poisson disk sampling, A*/NavMesh validation, "
                    "grammar/blueprint composition, and constraint validation/repair. Use this before large "
                    "or style-sensitive map edits when plan_map_layout is not enough."
                ),
                "parameters": _object_schema(
                    {
                        "mode": {"type": "string", "enum": ["2d", "3d"]},
                        "dimension": {"type": "string", "enum": ["2d", "3d"]},
                        "theme": {"type": "string"},
                        "pattern": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "density": {"type": "string", "enum": ["low", "medium", "high"]},
                        "seed": {"type": "integer"},
                        "min_object_distance": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Minimum grid distance between sampled object/decor points.",
                        },
                        "max_object_points": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Maximum sampled object/decor points.",
                        },
                        "blueprints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional saved blueprint names to compose into the generated structure.",
                        },
                        "start": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        "goal": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        "waypoints": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "entrances": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "exits": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="plan_platform_level",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "plan_platform_level",
                "description": (
                    "Plan a 2D side-scrolling platformer level from player movement ability first. It builds "
                    "a critical route of gameplay motifs, a jump reachability graph, preview-safe edit_map "
                    "batches for thin support platforms, reward coin arcs, enemy slots, a score, and a "
                    "validate_map_region plan using movement_model='leap'. It also applies platformer design "
                    "grammar limits so routes avoid oversized solid blocks, repeated pillar patterns, and unsafe "
                    "finish buffers. Use for Mario/Celeste/platform maps instead of generic zone/Poisson planning."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {
                            "type": "string",
                            "description": "2D TileMapLayer/TileMap used to sample the existing entry boundary.",
                        },
                        "map_layer": {
                            "type": "integer",
                            "description": "Layer index for legacy TileMap boundary sampling.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 8},
                        "height": {"type": "integer", "minimum": 8},
                        "ground_y": {
                            "type": "integer",
                            "description": "Support-row y coordinate for the first platform; defaults to vertical center.",
                        },
                        "seed": {"type": "integer"},
                        "connect_from_existing": {
                            "type": "boolean",
                            "description": "When true, scan the left boundary and make the generated route reachable from the existing map. Defaults to true.",
                        },
                        "entry_sample_x": {
                            "type": "integer",
                            "description": "Optional x for the existing-boundary sample rectangle.",
                        },
                        "entry_sample_y": {
                            "type": "integer",
                            "description": "Optional y for the existing-boundary sample rectangle.",
                        },
                        "entry_sample_width": {
                            "type": "integer",
                            "minimum": 3,
                            "description": "Width of the left-boundary sample used to find an existing foothold.",
                        },
                        "entry_sample_height": {
                            "type": "integer",
                            "minimum": 4,
                            "description": "Height of the left-boundary sample used to find an existing foothold.",
                        },
                        "max_horizontal_gap": {
                            "type": "integer",
                            "minimum": 2,
                            "description": "Maximum horizontal jump distance in cells, derived from the real controller.",
                        },
                        "max_rise": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "Maximum upward jump height in cells, derived from the real controller.",
                        },
                        "max_fall": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum acceptable downward landing difference in cells.",
                        },
                        "min_landing_width": {
                            "type": "integer",
                            "minimum": 2,
                            "description": "Minimum safe landing platform width in cells.",
                        },
                        "platform_thickness": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Tile thickness for emitted platform support fill operations.",
                        },
                        "max_platform_thickness": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Hard cap for emitted platform thickness; default 2 to avoid wall-like masses.",
                        },
                        "max_platform_width": {
                            "type": "integer",
                            "minimum": 5,
                            "description": "Maximum non-rest platform surface width before the plan is rejected as too blocky.",
                        },
                        "min_finish_buffer_width": {
                            "type": "integer",
                            "minimum": 4,
                            "description": "Minimum flat safe landing width before the finish area.",
                        },
                        "max_repeated_challenge_roles": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum tolerated repetition of the same challenge role before the plan is rejected.",
                        },
                        "max_solid_column_height": {
                            "type": "integer",
                            "minimum": 3,
                            "description": "Platform design validation limit for tall solid columns in the emitted route.",
                        },
                        "max_solid_mass_width": {
                            "type": "integer",
                            "minimum": 4,
                            "description": "Platform design validation limit for dense connected solid mass width.",
                        },
                        "max_solid_mass_height": {
                            "type": "integer",
                            "minimum": 3,
                            "description": "Platform design validation limit for dense connected solid mass height.",
                        },
                        "ground_resource": {
                            "type": "string",
                            "description": "Semantic resource key used by emitted edit_map platform fill drafts.",
                        },
                        "fallback_ground_resource": {
                            "type": "string",
                            "description": "Fallback resource key for emitted platform fill drafts.",
                        },
                    },
                    ["width", "height"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="plan_reachable_map_growth",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "plan_reachable_map_growth",
                "description": (
                    "Plan map expansion from a reachable frontier instead of generating isolated content. "
                    "Supports profile='platformer' (delegates to platform reachability), 'topdown' "
                    "(connected roads/ground), 'dungeon' (rooms and corridors), and '3d_grid' "
                    "(connected floor strips). Returns candidates, accepted_motifs, preview-safe "
                    "edit_map_batches, validation, and repair strategies; it never edits the scene. For "
                    "profile='platformer', the delegated plan uses platformer design grammar checks to prefer "
                    "thin platforms, varied motifs, safe finish buffers, and reachable growth from the real frontier."
                ),
                "parameters": _object_schema(
                    {
                        "profile": {
                            "type": "string",
                            "enum": ["platformer", "topdown", "dungeon", "3d_grid"],
                            "description": "Map/gameplay profile that selects movement model, motifs, validation, and repairs.",
                        },
                        "target_path": {"type": "string"},
                        "map_layer": {"type": "integer"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "frontier": {
                            "type": "object",
                            "description": "Optional known reachable frontier cell {x,y[,z]}; platformer can auto-sample it from the left boundary.",
                            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}},
                        },
                        "start": {
                            "type": "object",
                            "description": "Optional real player/unit start. When provided, plan_reachable_map_growth first computes rightmost_frontier from real map reachability.",
                            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}},
                        },
                        "frontier_type": {"type": "string"},
                        "walkable_is_filled": {"type": "boolean"},
                        "movement_model": {
                            "type": "string",
                            "enum": ["grid", "leap", "free"],
                            "description": "Movement model used when start is provided to compute the real reachable frontier.",
                        },
                        "connect_from_existing": {
                            "type": "boolean",
                            "description": "For platformer, scan existing left boundary and use the found foothold as frontier. Defaults to true.",
                        },
                        "entry_sample_x": {"type": "integer"},
                        "entry_sample_y": {"type": "integer"},
                        "entry_sample_width": {"type": "integer", "minimum": 3},
                        "entry_sample_height": {"type": "integer", "minimum": 4},
                        "max_steps": {"type": "integer", "minimum": 1},
                        "step_length": {"type": "integer", "minimum": 1},
                        "max_gap": {"type": "integer", "minimum": 1},
                        "room_width": {"type": "integer", "minimum": 1},
                        "room_height": {"type": "integer", "minimum": 1},
                        "corridor_length": {"type": "integer", "minimum": 1},
                        "path_depth": {"type": "integer", "minimum": 1},
                        "max_horizontal_gap": {"type": "integer", "minimum": 2},
                        "max_rise": {"type": "integer", "minimum": 0},
                        "max_fall": {"type": "integer", "minimum": 1},
                        "max_step": {"type": "integer", "minimum": 1},
                        "gravity_axis": {"type": "string", "enum": ["x", "y", "z"]},
                        "gravity_sign": {"type": "integer", "enum": [-1, 1]},
                        "max_returned_cells": {"type": "integer", "minimum": 1},
                        "min_landing_width": {"type": "integer", "minimum": 2},
                        "max_platform_thickness": {"type": "integer", "minimum": 1},
                        "max_platform_width": {"type": "integer", "minimum": 5},
                        "min_finish_buffer_width": {"type": "integer", "minimum": 4},
                        "max_repeated_challenge_roles": {"type": "integer", "minimum": 1},
                        "max_solid_column_height": {"type": "integer", "minimum": 3},
                        "max_solid_mass_width": {"type": "integer", "minimum": 4},
                        "max_solid_mass_height": {"type": "integer", "minimum": 3},
                        "road_resource": {"type": "string"},
                        "floor_resource": {"type": "string"},
                        "fallback_road_resource": {"type": "string"},
                        "fallback_floor_resource": {"type": "string"},
                    },
                    ["profile", "width", "height"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="compute_reachable_frontier",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "compute_reachable_frontier",
                "description": (
                    "Read the real TileMap/TileMapLayer/GridMap cells and compute all cells reachable from a "
                    "real player/unit start under movement_model='grid', 'leap', or 'free'. Returns "
                    "reachable_cells, reachable_footholds, rightmost_frontier, frontier_candidates, and "
                    "first_blocked_gap. Use before plan_reachable_map_growth when extending an existing playable map."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {"type": "string"},
                        "map_layer": {"type": "integer"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "start": {
                            "type": "object",
                            "description": "Real player/unit start cell {x,y[,z]} in map coordinates.",
                            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}},
                        },
                        "walkable_is_filled": {
                            "type": "boolean",
                            "description": "When true, filled cells are walkable; otherwise empty cells are walkable.",
                        },
                        "movement_model": {
                            "type": "string",
                            "enum": ["grid", "leap", "free"],
                            "description": "grid=adjacent walking, leap=platform footholds with support/jump limits, free=gravity-free movement.",
                        },
                        "max_horizontal_gap": {"type": "integer", "minimum": 1},
                        "max_rise": {"type": "integer", "minimum": 0},
                        "max_fall": {"type": "integer", "minimum": 0},
                        "max_step": {"type": "integer", "minimum": 1},
                        "gravity_axis": {"type": "string", "enum": ["x", "y", "z"]},
                        "gravity_sign": {"type": "integer", "enum": [-1, 1]},
                        "max_returned_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Caps returned reachable_cells/footholds payload size; search still visits the whole region.",
                        },
                    },
                    ["start", "width", "height"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="sample_poisson_points",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "sample_poisson_points",
                "description": (
                    "Deterministically sample naturally spaced map cells for props, resources, enemies, "
                    "collectibles, or decoration. Use instead of hand-rolling random coordinates."
                ),
                "parameters": _object_schema(
                    {
                        "mode": {"type": "string", "enum": ["2d", "3d"]},
                        "dimension": {"type": "string", "enum": ["2d", "3d"]},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "min_distance": {"type": "integer", "minimum": 1},
                        "max_points": {"type": "integer", "minimum": 0},
                        "seed": {"type": "integer"},
                        "zone": {
                            "type": "string",
                            "description": "Optional zone/semantic_layer name when zones are provided.",
                        },
                        "zones": {
                            "type": "array",
                            "description": "Optional zones from plan_map_algorithms/plan_map_layout.algorithm_plan.",
                            "items": {"type": "object"},
                        },
                        "exclude": {
                            "type": "array",
                            "description": "Exact cells to exclude from sampling.",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                    },
                    ["width", "height"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="compose_map_blueprint_grammar",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "compose_map_blueprint_grammar",
                "description": (
                    "Compose saved map blueprints/prefabs into a read-only stamping plan. Returns "
                    "apply_map_blueprint drafts when blueprint names are supplied, or edit/terrain fallback "
                    "drafts when no blueprints are available. It never edits the scene."
                ),
                "parameters": _object_schema(
                    {
                        "mode": {"type": "string", "enum": ["2d", "3d"]},
                        "dimension": {"type": "string", "enum": ["2d", "3d"]},
                        "pattern": {"type": "string"},
                        "region": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "width": {"type": "integer", "minimum": 1},
                                "height": {"type": "integer", "minimum": 1},
                                "depth": {"type": "integer", "minimum": 1},
                            },
                        },
                        "zones": {"type": "array", "items": {"type": "object"}},
                        "anchors": {"type": "object"},
                        "blueprints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Saved blueprint names to stamp in grammar slots.",
                        },
                        "seed": {"type": "integer"},
                    },
                    ["region"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="edit_map",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="map",
            schema={
                "name": "edit_map",
                "description": (
                    "Edit a 2D TileMapLayer/legacy TileMap or a 3D GridMap through Godot's native APIs. "
                    "Use this tool instead of refusing a map edit or directly rewriting serialized tile/map data. "
                    "Supports fill, erase, and overlap-safe region copy; all changes are previewed and undoable. "
                    "For a legacy TileMap with multiple layers, call describe_map_region first to see the real "
                    "`layers` list and confirm which index is the visible/collidable foreground layer before "
                    "picking map_layer — do not assume index 0 is the right one. "
                    "The result includes `layer_coverage_gaps`: any sibling layer (other legacy-TileMap layer "
                    "index, or other TileMapLayer under the same parent) that already covered ~90%+ of the map's "
                    "extent before this edit (a background/sky/water backdrop, not local decoration) but now falls "
                    "short of the map's new overall extent after this edit. Each entry may include "
                    "`shortfall_cells` (boundary lag — left/right/top/bottom cell counts) and/or "
                    "`interior_holes_x` (column ranges where that layer has NO tiles even though they're inside "
                    "its own already-covered boundary — e.g. a background that nominally spans the right width "
                    "but still has a gray gap in the middle). When non-empty, extend those layers to match before "
                    "treating a level-extension task as finished — do not rely on remembering to check this "
                    "yourself; read the field every time. Also: if the `resource`/`resource_key` you passed is "
                    "registered with a scene_path (an object/PackedScene, e.g. a tree), this call fails with "
                    "error_code 'resource_requires_object_placement' — use place_map_objects for it instead of "
                    "approximating it out of tiles. Pass `expected_cells` (the number of cells this batch should "
                    "write, e.g. an inclusive x=A..B span is B-A+1 columns × the fill height) so the tool can "
                    "reject an off-by-one batch (error_code 'cell_count_mismatch') before any tiles are written, "
                    "instead of discovering the gap later in validate_map_region."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {
                            "type": "string",
                            "description": (
                                "NodePath relative to the edited scene root. Omit to use the selected map node "
                                "or the only compatible map node in the scene."
                            ),
                        },
                        "map_layer": {
                            "type": "integer",
                            "description": (
                                "Layer index for a legacy TileMap; ignored by TileMapLayer and GridMap. Defaults "
                                "to 0 if omitted — confirm this is the intended layer via describe_map_region's "
                                "`layers` field first, since index 0 is not always the foreground/collidable layer."
                            ),
                        },
                        "operations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 128,
                            "description": (
                                "Ordered map operations. Coordinates use x/y for 2D and x/y/z for 3D. "
                                "copy reads the complete source region before writing, so overlapping copies are safe."
                            ),
                            "items": _object_schema(
                                {
                                    "action": {
                                        "type": "string",
                                        "enum": ["fill", "erase", "copy"],
                                    },
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "width": {"type": "integer", "minimum": 1},
                                    "height": {"type": "integer", "minimum": 1},
                                    "depth": {"type": "integer", "minimum": 1},
                                    "source_id": {
                                        "type": "integer",
                                        "description": "2D TileSet source id for fill.",
                                    },
                                    "atlas_x": {"type": "integer"},
                                    "atlas_y": {"type": "integer"},
                                    "alternative_tile": {"type": "integer"},
                                    "item": {
                                        "type": "integer",
                                        "description": "3D MeshLibrary item id for fill.",
                                    },
                                    "orientation": {
                                        "type": "integer",
                                        "description": "3D GridMap orthogonal orientation index.",
                                    },
                                    "resource": {
                                        "type": "string",
                                        "description": "Optional semantic resource key from resource_registry.json.",
                                    },
                                    "resource_key": {
                                        "type": "string",
                                        "description": "Optional alias for resource; stored in the spatial index.",
                                    },
                                    "fallback_resource": {
                                        "type": "string",
                                        "description": "Optional fallback registry key used when resource/resource_key is absent.",
                                    },
                                    "semantic_layer": {
                                        "type": "string",
                                        "description": "Optional logical layer such as ground, water, road, obstacle, decor.",
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Optional semantic tags copied into the spatial index.",
                                    },
                                    "cost": {
                                        "type": "number",
                                        "description": "Optional traversal cost copied into the spatial index.",
                                    },
                                    "from_x": {"type": "integer"},
                                    "from_y": {"type": "integer"},
                                    "from_z": {"type": "integer"},
                                    "to_x": {"type": "integer"},
                                    "to_y": {"type": "integer"},
                                    "to_z": {"type": "integer"},
                                },
                                ["action"],
                            ),
                        },
                        "update_spatial_index": {
                            "type": "boolean",
                            "description": (
                                "When true, update res://.ai_agent_service/map_agent/spatial_index.json with the "
                                "changed cells in the same preview/undo batch. Use for durable local edits, "
                                "delete/replace tasks, and blueprint-like reuse; omit for quick exploratory edits."
                            ),
                        },
                        "allowed_bounds": {
                            "type": "object",
                            "description": (
                                "Optional hard map bounds {x,y[,z],width,height[,depth]}. When provided, edit_map "
                                "rejects any operation that would write outside this playable/design region."
                            ),
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "width": {"type": "integer", "minimum": 1},
                                "height": {"type": "integer", "minimum": 1},
                                "depth": {"type": "integer", "minimum": 1},
                            },
                        },
                        "expected_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Optional self-check: the number of cells this batch is supposed to write. If it "
                                "does not match what the operations actually produce, the call is rejected with "
                                "error_code 'cell_count_mismatch' and nothing is written. Always set it to your "
                                "declared coverage (sum of width*height[*depth] per op) so a miscounted inclusive "
                                "range — e.g. meaning x=85..87 but only emitting 85 and 86 — is caught immediately."
                            ),
                        },
                    },
                    ["operations"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="paint_terrain_connect",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="map",
            schema={
                "name": "paint_terrain_connect",
                "description": (
                    "Paint 2D TileMapLayer/legacy TileMap cells with Godot TileSet terrain connection rules. "
                    "Use when a resource_registry entry provides terrain_set/terrain or when water/roads need "
                    "smooth auto-connected edges. Previewed and undoable. If the resolved resource is registered "
                    "with a scene_path instead (an object/PackedScene) and you didn't explicitly pass "
                    "terrain_set/terrain, this fails with error_code 'resource_requires_object_placement' — use "
                    "place_map_objects for it instead."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {"type": "string"},
                        "map_layer": {"type": "integer"},
                        "terrain_set": {"type": "integer"},
                        "terrain": {"type": "integer"},
                        "resource": {
                            "type": "string",
                            "description": "Optional registry key carrying terrain_set/terrain.",
                        },
                        "fallback_resource": {
                            "type": "string",
                            "description": "Optional fallback registry key carrying terrain_set/terrain.",
                        },
                        "ignore_empty_terrains": {"type": "boolean"},
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                },
                            },
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "allowed_bounds": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "width": {"type": "integer", "minimum": 1},
                                "height": {"type": "integer", "minimum": 1},
                            },
                        },
                    },
                    ["terrain_set", "terrain"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="place_map_objects",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "place_map_objects",
                "description": (
                    "Instantiate PackedScene map objects under ObjectLayer/PropsRoot (or parent_path) at map cell "
                    "coordinates. It resolves scene_path directly or from resource_registry entries, converts map "
                    "cell coordinates to local Node2D/Node3D positions, rejects overlaps by default using the "
                    "spatial index, and can record placements back into the index. Previewed and undoable."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {
                            "type": "string",
                            "description": "Map node used for coordinate conversion.",
                        },
                        "parent_path": {
                            "type": "string",
                            "description": "Optional ObjectLayer/PropsRoot node path; inferred when omitted.",
                        },
                        "objects": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 128,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "resource": {"type": "string"},
                                    "resource_key": {"type": "string"},
                                    "fallback_resource": {"type": "string"},
                                    "scene_path": {"type": "string"},
                                    "name": {"type": "string"},
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "semantic_layer": {"type": "string"},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                },
                            },
                        },
                        "allow_overlap": {
                            "type": "boolean",
                            "description": "Defaults to false; when false, same-cell object placements are rejected.",
                        },
                        "allow_on_blocked": {
                            "type": "boolean",
                            "description": (
                                "Defaults to false; when false, object placement is rejected on spatial-index "
                                "water/blocked/obstacle cells."
                            ),
                        },
                        "update_spatial_index": {
                            "type": "boolean",
                            "description": "Defaults to true; records placed objects for future semantic lookup.",
                        },
                        "allowed_bounds": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "width": {"type": "integer", "minimum": 1},
                                "height": {"type": "integer", "minimum": 1},
                                "depth": {"type": "integer", "minimum": 1},
                            },
                        },
                    },
                    ["objects"],
                ),
            },
        )
    )
    placement_profile_properties = {
        "placement_kind": {
            "type": "string",
            "description": "Generic object placement preset: tree, decor, building, npc, enemy, chest, coin, etc.",
        },
        "kind": {"type": "string"},
        "anchor": {
            "type": "string",
            "enum": ["bottom_center", "bottom_left", "bottom_right", "top_center", "top_left", "top_right", "center"],
            "description": "How the object footprint is aligned to the input cell; defaults to bottom_center.",
        },
        "surface_type": {
            "type": "string",
            "enum": ["ground", "wall", "water_surface", "water", "air", "room_center", "branch_end", "path_edge"],
        },
        "footprint_width": {"type": "integer", "minimum": 1},
        "footprint_height": {"type": "integer", "minimum": 1},
        "footprint_depth": {"type": "integer", "minimum": 1},
        "requires_support": {"type": "boolean"},
        "support_mode": {"type": "string", "enum": ["bottom", "wall"]},
        "support_layers": {"type": "array", "items": {"type": "string"}},
        "forbidden_layers": {"type": "array", "items": {"type": "string"}},
        "clearance": {"type": "integer", "minimum": 0},
        "clearance_left": {"type": "integer", "minimum": 0},
        "clearance_right": {"type": "integer", "minimum": 0},
        "clearance_up": {"type": "integer", "minimum": 0},
        "clearance_down": {"type": "integer", "minimum": 0},
        "clearance_front": {"type": "integer", "minimum": 0},
        "clearance_back": {"type": "integer", "minimum": 0},
        "min_distance_to_protected": {"type": "integer", "minimum": 0},
        "preferred_distance_to_protected": {"type": "integer", "minimum": 0},
        "min_distance_from_same_kind": {"type": "integer", "minimum": 0},
        "requires_reachable": {
            "type": "boolean",
            "description": "When true, the anchor/interaction/entrance point must be reachable from start under movement_model.",
        },
        "reachability_point": {
            "type": "string",
            "enum": ["anchor", "interaction", "entrance"],
            "description": "Which point to test for reachability; interaction/entrance use their offset from the anchor.",
        },
        "interaction_offset": {"type": "object"},
        "entrance_offset": {"type": "object"},
        "map_layer": {"type": "integer"},
        "ground_map_layer": {"type": "integer"},
        "start": {"type": "object"},
        "movement_model": {"type": "string", "enum": ["grid", "leap", "free"]},
        "path_algorithm": {"type": "string", "enum": ["bfs", "astar", "a*"]},
        "walkable_is_filled": {"type": "boolean"},
        "max_horizontal_gap": {"type": "integer", "minimum": 1},
        "max_rise": {"type": "integer", "minimum": 0},
        "max_fall": {"type": "integer", "minimum": 0},
        "max_step": {"type": "integer", "minimum": 1},
        "protected_cells": {"type": "array", "items": {"type": "object"}},
        "path_cells": {"type": "array", "items": {"type": "object"}},
        "route_cells": {"type": "array", "items": {"type": "object"}},
        "frontier_cells": {"type": "array", "items": {"type": "object"}},
        "branch_ends": {"type": "array", "items": {"type": "object"}},
        "room_centers": {"type": "array", "items": {"type": "object"}},
        "reward_cells": {"type": "array", "items": {"type": "object"}},
    }
    register(
        ToolDef(
            name="find_placement_anchors",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "find_placement_anchors",
                "description": (
                    "Search real map cells for legal object anchors before placing trees, buildings, NPCs, enemies, "
                    "chests, and other PackedScene objects. It checks empty footprint cells, solid support, clearance, "
                    "spatial-index object/blocked cells, and optional protected route/path cells, then returns scored "
                    "candidate anchors. Use before place_map_objects instead of guessing decoration coordinates."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "max_results": {"type": "integer", "minimum": 1},
                        **placement_profile_properties,
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="validate_object_placements",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "validate_object_placements",
                "description": (
                    "Validate proposed PackedScene object coordinates with the same generic placement rules used by "
                    "place_map_objects: footprint emptiness, support, clearance, object overlap, blocked/water/obstacle "
                    "cells, and optional protected route/path cells. Returns issues and relocation repair hints."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {"type": "string"},
                        "objects": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "resource": {"type": "string"},
                                    "resource_key": {"type": "string"},
                                    **placement_profile_properties,
                                },
                            },
                        },
                        **placement_profile_properties,
                    },
                    ["objects"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="repair_placements",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="json",
            schema={
                "name": "repair_placements",
                "description": (
                    "Preview/undoable semantic relocation repair for indexed PackedScene map objects. It validates "
                    "objects in a region with the same placement profile rules as validate_object_placements, finds "
                    "the highest-scoring legal anchor, moves resolvable scene nodes there, and updates the spatial "
                    "index. If an indexed object has no resolvable node, it returns a suggested relocation plan."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {"type": "string"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "resource": {"type": "string"},
                        "resource_key": {"type": "string"},
                        "tags": {"type": "array", "items": {"type": "string"}},
                        **placement_profile_properties,
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="validate_layer_coverage",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "validate_layer_coverage",
                "description": (
                    "Read-only check for blanket layers such as background/sky/water that originally covered about "
                    "90% of the map extent but now fall short of the foreground map extent or have interior column "
                    "holes. Use after map growth and before final screenshots."
                ),
                "parameters": _object_schema(
                    {"target_path": {"type": "string"}, "map_layer": {"type": "integer"}},
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="repair_layer_coverage",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="json",
            schema={
                "name": "repair_layer_coverage",
                "description": (
                    "Preview/undoable repair for blanket layer coverage gaps. It copies real existing cells from the "
                    "nearest available column of the lagging blanket layer into missing boundary columns or interior "
                    "holes, so backgrounds extend with the playable map instead of exposing the editor gray area."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {"type": "string"},
                        "map_layer": {"type": "integer"},
                        "max_cells": {"type": "integer", "minimum": 1},
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="query_spatial_index",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "query_spatial_index",
                "description": (
                    "Search the spatial index (res://.ai_agent_service/map_agent/spatial_index.json) for "
                    "semantic objects previously recorded via edit_map(update_spatial_index=true). Filter by "
                    "tags, semantic_layer, resource key, target node, and/or a coordinate region. Use this to "
                    "locate existing objects for local edits ('delete the tree in the top-left', 'replace the "
                    "village road') instead of re-reading and re-drawing the whole map."
                ),
                "parameters": _object_schema(
                    {
                        "dimension": {
                            "type": "string",
                            "enum": ["2d", "3d"],
                            "description": "Which index branch to search; defaults to 2d.",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Match entries carrying any of these tags.",
                        },
                        "resource": {
                            "type": "string",
                            "description": "Match entries with this resource/resource_key.",
                        },
                        "semantic_layer": {
                            "type": "string",
                            "description": "Match entries on this logical layer (ground, water, road, ...).",
                        },
                        "target_path": {
                            "type": "string",
                            "description": "Restrict the search to one map node's recorded cells.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Maximum number of matches to return; defaults to 200.",
                        },
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="compact_spatial_index",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="json",
            schema={
                "name": "compact_spatial_index",
                "description": (
                    "Compact or clear the durable spatial index at "
                    "res://.ai_agent_service/map_agent/spatial_index.json. Use it when describe_map_context "
                    "shows the index is near max_entries, after large deleted/replaced regions, or when a target "
                    "map has been regenerated. It can clear everything, clear one dimension/target/coordinate "
                    "region, or simply prune the index down to max_entries. Previewed and undoable."
                ),
                "parameters": _object_schema(
                    {
                        "dimension": {
                            "type": "string",
                            "enum": ["2d", "3d"],
                            "description": "Optional index branch to compact.",
                        },
                        "target_path": {
                            "type": "string",
                            "description": "Optional map node path whose indexed cells should be compacted/cleared.",
                        },
                        "clear_all": {
                            "type": "boolean",
                            "description": "When true, remove all entries matching dimension/target_path filters.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "max_entries": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Prune the whole remaining index down to this entry count.",
                        },
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="validate_map_region",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "validate_map_region",
                "description": (
                    "Read-only reachability/structure check over a small map region. Counts filled vs empty cells and, "
                    "when start/goal, entrances/exits, or waypoints are given, runs BFS or A* connectivity under a "
                    "pluggable MOVEMENT MODEL (movement_model): 'grid' (abstract adjacency, no gravity — tactics/top-down/"
                    "mazes), 'leap' (gravity: a foothold must be empty with solid support directly below, and you can only "
                    "reach other footholds within max_horizontal_gap / max_rise / max_fall — use for 2D platformers AND "
                    "3D jump/climb), or 'free' (no gravity, single step up to max_step — flying/swimming). CRITICAL: "
                    "'grid' only proves cells are adjacent, NOT that the character can actually traverse — for any "
                    "jump/gravity gameplay you MUST pass movement_model='leap' with jump limits measured from the real "
                    "character controller, otherwise a level of floating platforms over open air will wrongly pass. "
                    "By default empty cells are walkable and filled cells are obstacles; set walkable_is_filled=true to "
                    "invert. Optionally override gravity direction via gravity_axis/gravity_sign. It can also enforce "
                    "allowed_bounds, check spatial-index overlaps, and detect objects on water/blocked cells. Returns "
                    "issues, passed, path/multi_connectivity, and repair_plan, but never edits. For leap/platformer "
                    "validation it can also run platform_design checks for oversized solid rows, tall columns, filled "
                    "masses, and insufficient finish buffer. A 'passed' result only "
                    "means reachable under the given movement assumptions — still verify the design visually. It also "
                    "always returns `layer_coverage_gaps`: any sibling layer (other legacy-TileMap layer index, or "
                    "other TileMapLayer under the same parent) that already covers ~90%+ of the map's extent (a "
                    "background/sky/water backdrop, not local decoration) but currently falls short — either at the "
                    "boundary (`shortfall_cells`) or with a gap in the middle of its own already-covered range "
                    "(`interior_holes_x`, e.g. a background that nominally spans the right width but still has a "
                    "gray hole partway through). Either kind forces `passed=false` just like a failed connectivity "
                    "check, regardless of whether the gap was introduced by a recent edit or was already there."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {
                            "type": "string",
                            "description": (
                                "NodePath relative to the edited scene root. Omit to use the selected/only map node."
                            ),
                        },
                        "map_layer": {
                            "type": "integer",
                            "description": "Layer index for a legacy TileMap; ignored by TileMapLayer and GridMap.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "start": {
                            "type": "object",
                            "description": "Optional BFS start cell {x, y[, z]} in map coordinates.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                            },
                        },
                        "goal": {
                            "type": "object",
                            "description": "Optional BFS goal cell {x, y[, z]} in map coordinates.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                            },
                        },
                        "waypoints": {
                            "type": "array",
                            "description": "Optional ordered cells that the path must pass through.",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "entrances": {
                            "type": "array",
                            "description": "Optional entrance cells; each entrance must reach at least one exit.",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "exits": {
                            "type": "array",
                            "description": "Optional exit cells used with entrances.",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "walkable_is_filled": {
                            "type": "boolean",
                            "description": "When true, filled cells are walkable and empty cells are obstacles.",
                        },
                        "movement_model": {
                            "type": "string",
                            "enum": ["grid", "leap", "free"],
                            "description": (
                                "How the character moves, which decides what 'reachable' means. 'grid' = abstract "
                                "adjacency, no gravity. 'leap' = gravity + jump/climb limits (footholds need support "
                                "below; only reachable within max_horizontal_gap/max_rise/max_fall). 'free' = no "
                                "gravity, step up to max_step. Use 'leap' for ANY jump/platform/gravity gameplay; "
                                "defaults to 'grid'."
                            ),
                        },
                        "max_horizontal_gap": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "leap: max horizontal distance (in cells) the character can clear in one jump. Derive from real run speed + jump airtime; do not guess.",
                        },
                        "max_rise": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "leap: max cells the character can gain in height per jump. Derive from real jump velocity/gravity; do not guess.",
                        },
                        "max_fall": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "leap: max cells the character may drop and still be considered a valid reach (falling is usually generous).",
                        },
                        "max_step": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "free: max single-move distance in cells (Manhattan) for gravity-free movement (flying/swimming).",
                        },
                        "gravity_axis": {
                            "type": "string",
                            "enum": ["x", "y", "z"],
                            "description": "Optional override for which axis points 'down'. Defaults: 2D y-down, 3D y-up. Only set if your project uses a non-standard gravity direction.",
                        },
                        "gravity_sign": {
                            "type": "integer",
                            "enum": [-1, 1],
                            "description": "Sign of the gravity axis ('down' direction). Used with gravity_axis.",
                        },
                        "path_algorithm": {
                            "type": "string",
                            "enum": ["bfs", "astar", "a*"],
                            "description": "Connectivity algorithm; use astar when repairs should prefer obstacle-aware paths.",
                        },
                        "check_overlaps": {
                            "type": "boolean",
                            "description": "When true, fail validation if the spatial index has multiple entries at one coordinate.",
                        },
                        "check_blocked_objects": {
                            "type": "boolean",
                            "description": "When true, fail validation if indexed objects sit on water/blocked/obstacle cells.",
                        },
                        "check_platform_design": {
                            "type": "boolean",
                            "description": "When true, fail 2D leap validation on wall-like platformer shapes: oversized solid rows, tall columns, large filled masses, or unsafe finish buffer. Defaults on for leap.",
                        },
                        "max_solid_run_width": {
                            "type": "integer",
                            "minimum": 4,
                            "description": "Platform design check: longest allowed continuous solid row before it is considered too blocky.",
                        },
                        "max_solid_column_height": {
                            "type": "integer",
                            "minimum": 3,
                            "description": "Platform design check: tallest allowed continuous solid column before it is considered a wall/pillar problem.",
                        },
                        "max_solid_mass_width": {
                            "type": "integer",
                            "minimum": 4,
                            "description": "Platform design check: maximum bounding-box width for dense connected solid masses.",
                        },
                        "max_solid_mass_height": {
                            "type": "integer",
                            "minimum": 3,
                            "description": "Platform design check: maximum bounding-box height for dense connected solid masses.",
                        },
                        "min_finish_buffer_width": {
                            "type": "integer",
                            "minimum": 2,
                            "description": "Platform design check: minimum contiguous standable width at goal.",
                        },
                        "allowed_bounds": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "width": {"type": "integer", "minimum": 1},
                                "height": {"type": "integer", "minimum": 1},
                                "depth": {"type": "integer", "minimum": 1},
                            },
                        },
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="repair_map_region",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="map",
            schema={
                "name": "repair_map_region",
                "description": (
                    "Apply automatic repairs for validate_map_region failures. Pass the SAME movement_model (and jump "
                    "limits) you validated with so the repair matches how the character actually moves. For 'grid'/'free' "
                    "connectivity it erases a corridor by default (empty space becomes walkable); with "
                    "walkable_is_filled=true it fills the corridor. For 'leap' failures it instead proposes filling the "
                    "SUPPORT row beneath the path (a ground/platform bridge across the gap), so it requires "
                    "source_id/atlas_x/atlas_y for 2D or item for 3D. With repair_overlaps/repair_blocked_objects it "
                    "moves indexed object nodes to nearby free cells and updates the spatial index. Re-run "
                    "validate_map_region after repair. Note: it only stitches a minimal path/bridge — complex or "
                    "art-quality fixes still need edit_map."
                ),
                "parameters": _object_schema(
                    {
                        "target_path": {"type": "string"},
                        "map_layer": {"type": "integer"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "start": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                            },
                        },
                        "goal": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                            },
                        },
                        "waypoints": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "entrances": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "exits": {
                            "type": "array",
                            "items": {"type": "object", "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}, "z": {"type": "integer"}}},
                        },
                        "walkable_is_filled": {"type": "boolean"},
                        "movement_model": {
                            "type": "string",
                            "enum": ["grid", "leap", "free"],
                            "description": "Must match the movement_model used in validate_map_region. 'leap' bridges gaps by filling the support row beneath the path.",
                        },
                        "max_horizontal_gap": {"type": "integer", "minimum": 1, "description": "leap: max horizontal jump distance in cells."},
                        "max_rise": {"type": "integer", "minimum": 0, "description": "leap: max jump height gain in cells."},
                        "max_fall": {"type": "integer", "minimum": 0, "description": "leap: max drop in cells treated as reachable."},
                        "max_step": {"type": "integer", "minimum": 1, "description": "free: max single-move distance in cells."},
                        "gravity_axis": {"type": "string", "enum": ["x", "y", "z"], "description": "Optional 'down' axis override; must match validate_map_region."},
                        "gravity_sign": {"type": "integer", "enum": [-1, 1], "description": "Sign of the gravity axis; used with gravity_axis."},
                        "repair_overlaps": {
                            "type": "boolean",
                            "description": "Move duplicate indexed objects to nearby free cells; start/goal not required.",
                        },
                        "repair_blocked_objects": {
                            "type": "boolean",
                            "description": "Move indexed objects off water/blocked/obstacle cells; start/goal not required.",
                        },
                        "path_algorithm": {
                            "type": "string",
                            "enum": ["bfs", "astar", "a*"],
                        },
                        "source_id": {"type": "integer"},
                        "fill_source_id": {"type": "integer"},
                        "atlas_x": {"type": "integer"},
                        "atlas_y": {"type": "integer"},
                        "fill_atlas_x": {"type": "integer"},
                        "fill_atlas_y": {"type": "integer"},
                        "alternative_tile": {"type": "integer"},
                        "item": {"type": "integer"},
                        "fill_item": {"type": "integer"},
                        "orientation": {"type": "integer"},
                        "update_spatial_index": {"type": "boolean"},
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="sample_noise_grid",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "sample_noise_grid",
                "description": (
                    "Sample a FastNoiseLite grid of normalized 0..1 values over a region, for natural "
                    "distribution decisions (tree/rock density, terrain variation). Pure computation; reads "
                    "and writes nothing in the scene. Use a fixed seed for reproducible layouts, then apply a "
                    "threshold to turn values into placement density."
                ),
                "parameters": _object_schema(
                    {
                        "dimension": {
                            "type": "string",
                            "enum": ["2d", "3d"],
                            "description": "Sample a 2D plane or a 3D volume; defaults to 2d.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "seed": {"type": "integer", "description": "Noise seed for reproducibility."},
                        "frequency": {"type": "number", "description": "Noise frequency; defaults to 0.05."},
                        "noise_type": {
                            "type": "string",
                            "enum": [
                                "simplex",
                                "simplex_smooth",
                                "perlin",
                                "cellular",
                                "value",
                                "value_cubic",
                            ],
                            "description": "FastNoiseLite type; defaults to simplex.",
                        },
                        "octaves": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Optional fractal octaves; omit for the engine default.",
                        },
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="write_resource_registry",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="json",
            schema={
                "name": "write_resource_registry",
                "description": (
                    "Create or maintain the resource semantic registry at "
                    "res://.ai_agent_service/map_agent/resource_registry.json, mapping natural-language keys "
                    "(grass, wall, river, elf_house, ...) to real TileSet/MeshLibrary/PackedScene references "
                    "so future tasks resolve resources instead of guessing ids. Entries merge by key by "
                    "default; set replace=true to overwrite the whole table. Each write is previewed and "
                    "undoable. Only record resources you have verified exist via describe_map_context or "
                    "describe_map_region."
                ),
                "parameters": _object_schema(
                    {
                        "entries": {
                            "type": "object",
                            "description": (
                                "Object mapping each resource key to its definition object (display_name, "
                                "mode, target, source_id/atlas_coords, terrain_set/terrain, mesh_library_item "
                                "or scene_path, tags, cost, ...). Values are stored verbatim."
                            ),
                            "additionalProperties": {"type": "object"},
                        },
                        "replace": {
                            "type": "boolean",
                            "description": "When true, replace the entire registry instead of merging by key.",
                        },
                    },
                    ["entries"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="save_map_blueprint",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="json",
            schema={
                "name": "save_map_blueprint",
                "description": (
                    "Capture the non-empty tiles/cells in a region as a reusable template at "
                    "res://.ai_agent_service/map_agent/blueprints/<name>.json, storing relative coordinates "
                    "and real resource references. Reuse later with apply_map_blueprint. Previewed and "
                    "undoable."
                ),
                "parameters": _object_schema(
                    {
                        "name": {
                            "type": "string",
                            "description": "Blueprint name (letters, digits, _ and - only).",
                        },
                        "target_path": {"type": "string"},
                        "map_layer": {"type": "integer"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional tags stored with the blueprint.",
                        },
                    },
                    ["name"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="apply_map_blueprint",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="map",
            schema={
                "name": "apply_map_blueprint",
                "description": (
                    "Stamp a previously saved blueprint at a destination origin, reusing its real resource "
                    "references. The blueprint's dimension must match the target map. Optionally update the "
                    "spatial index. Previewed and undoable."
                ),
                "parameters": _object_schema(
                    {
                        "name": {
                            "type": "string",
                            "description": "Name of the saved blueprint to apply.",
                        },
                        "target_path": {"type": "string"},
                        "map_layer": {"type": "integer"},
                        "x": {"type": "integer", "description": "Destination origin x for the blueprint."},
                        "y": {"type": "integer", "description": "Destination origin y for the blueprint."},
                        "z": {"type": "integer", "description": "Destination origin z (3D only)."},
                        "update_spatial_index": {
                            "type": "boolean",
                            "description": "When true, record stamped cells into the spatial index.",
                        },
                    },
                    ["name"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="ensure_standard_map_layers",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "ensure_standard_map_layers",
                "description": (
                    "Create any missing standard map structure under a parent node. For 2D it ensures "
                    "GroundLayer, WaterLayer, RoadLayer, ObstacleLayer, DecorLayer, and ObjectLayer; "
                    "for 3D it ensures GridMap, PropsRoot, LightsRoot, and InteractRoot. Existing nodes "
                    "are reused. TileSet/MeshLibrary is copied from reference_path or the first compatible "
                    "map node under the parent when available. Previewed and undoable."
                ),
                "parameters": _object_schema(
                    {
                        "mode": {
                            "type": "string",
                            "enum": ["2d", "3d"],
                            "description": "Which standard structure to create; defaults to 2d.",
                        },
                        "parent_path": {
                            "type": "string",
                            "description": "Parent NodePath relative to the edited scene root; defaults to scene root.",
                        },
                        "reference_path": {
                            "type": "string",
                            "description": "Optional existing TileMapLayer/GridMap to copy TileSet/MeshLibrary from.",
                        },
                    },
                    [],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="fill_rect",
            domain="map",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="map",
            schema={
                "name": "fill_rect",
                "description": "Fill a rectangle in the selected TileMapLayer after user confirmation.",
                "parameters": _object_schema(
                    {
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "width": {"type": "integer"},
                        "height": {"type": "integer"},
                        "source_id": {"type": "integer"},
                        "atlas_x": {"type": "integer"},
                        "atlas_y": {"type": "integer"},
                    },
                    ["x", "y", "width", "height", "source_id", "atlas_x", "atlas_y"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="paint_from_image_grid",
            domain="map",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="map",
            read_path_args=["image_path"],
            schema={
                "name": "paint_from_image_grid",
                "description": (
                    "Convert an image or sketch into a bounded TileMap cell grid using a color palette. "
                    "Requires a selected TileMapLayer and user confirmation."
                ),
                "parameters": _object_schema(
                    {
                        "image_path": {"type": "string", "description": "Relative or res:// image path."},
                        "origin_x": {"type": "integer"},
                        "origin_y": {"type": "integer"},
                        "max_width": {"type": "integer"},
                        "max_height": {"type": "integer"},
                        "palette": {
                            "type": "array",
                            "description": "Color-to-tile mappings with hex/source_id/atlas_x/atlas_y.",
                            "items": _object_schema(
                                {
                                    "hex": {"type": "string"},
                                    "source_id": {"type": "integer"},
                                    "atlas_x": {"type": "integer"},
                                    "atlas_y": {"type": "integer"},
                                    "alternative_tile": {"type": "integer"},
                                },
                                ["hex", "source_id", "atlas_x", "atlas_y"],
                            ),
                        },
                    },
                    ["image_path", "palette"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="create_resource",
            domain="resource",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            path_args=["path"],
            schema={
                "name": "create_resource",
                "description": "Create a Godot Resource file after user confirmation.",
                "parameters": _object_schema(
                    {
                        "path": {
                            "type": "string",
                            "description": "Relative resource path, for example resources/item.tres.",
                        },
                        "type": {
                            "type": "string",
                            "description": "Resource class to instantiate, default Resource.",
                        },
                    },
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="read_image_metadata",
            domain="resource",
            side="front",
            reads_project=True,
            uses_network=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            path_args=["path"],
            schema={
                "name": "read_image_metadata",
                "description": (
                    "Read image size, format and sampled dominant colors from a project asset. When asset "
                    "understanding is configured, the service also sends the image through the multimodal "
                    "asset-understanding model after applying the shared image compression/format conversion."
                ),
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "Relative or res:// image path."},
                        "sample_step": {"type": "integer"},
                    },
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="create_sprite_frames_from_sheet",
            domain="resource",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            path_args=["output_path"],
            read_path_args=["sheet_path"],
            schema={
                "name": "create_sprite_frames_from_sheet",
                "description": "Create a SpriteFrames resource from a sprite sheet after confirmation.",
                "parameters": _object_schema(
                    {
                        "sheet_path": {"type": "string"},
                        "output_path": {"type": "string"},
                        "frame_width": {"type": "integer"},
                        "frame_height": {"type": "integer"},
                        "animations": {
                            "type": "array",
                            "items": _object_schema(
                                {
                                    "name": {"type": "string"},
                                    "from": {"type": "integer"},
                                    "to": {"type": "integer"},
                                    "fps": {"type": "number"},
                                    "loop": {"type": "boolean"},
                                },
                                ["name", "from", "to"],
                            ),
                        },
                    },
                    ["sheet_path", "output_path", "frame_width", "frame_height", "animations"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="read_resource",
            domain="resource",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            path_args=["path"],
            schema={
                "name": "read_resource",
                "description": "Read the exported/storable properties of any .tres/.res resource file.",
                "parameters": _object_schema(
                    {"path": {"type": "string", "description": "Relative or res:// resource path."}},
                    ["path"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="set_resource_property",
            domain="resource",
            side="front",
            reads_project=True,
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            path_args=["path"],
            schema={
                "name": "set_resource_property",
                "description": "Set a single exported property on an existing .tres/.res resource and save it.",
                "parameters": _object_schema(
                    {
                        "path": {"type": "string", "description": "Relative resource path."},
                        "property": {"type": "string", "description": "Exported property name."},
                        "value": {
                            "description": (
                                "JSON value to assign. To attach another resource (e.g. a Shader on a "
                                "ShaderMaterial), pass {\"_resource_path\": \"res://...\"} instead of a raw value."
                            ),
                        },
                    },
                    ["path", "property", "value"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="create_animation_track",
            domain="resource",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="list",
            schema={
                "name": "create_animation_track",
                "description": (
                    "Add or replace a single VALUE track (by track_path) on an animation inside an "
                    "AnimationPlayer's AnimationLibrary. Other existing tracks on the same animation are untouched."
                ),
                "parameters": _object_schema(
                    {
                        "player_path": {"type": "string", "description": "NodePath to the AnimationPlayer, relative to the scene root."},
                        "animation": {"type": "string", "description": "Animation name within the library."},
                        "library": {"type": "string", "description": "AnimationLibrary name; defaults to the unnamed default library."},
                        "track_path": {
                            "type": "string",
                            "description": "NodePath:property being animated, relative to the AnimationPlayer's root node, e.g. Sprite2D:position.",
                        },
                        "interpolation": {
                            "type": "integer",
                            "description": "Animation.InterpolationType value; defaults to linear (1).",
                        },
                        "keyframes": {
                            "type": "array",
                            "minItems": 1,
                            "items": _object_schema(
                                {
                                    "time": {"type": "number"},
                                    "value": {"description": "JSON value matching the animated property's type."},
                                    "transition": {"type": "number"},
                                },
                                ["time", "value"],
                            ),
                        },
                    },
                    ["player_path", "animation", "track_path", "keyframes"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="create_shader_material",
            domain="resource",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="diff",
            path_args=["material_path", "shader_path"],
            schema={
                "name": "create_shader_material",
                "description": (
                    "Write a .gdshader file and a ShaderMaterial (.tres) that references it, in one step. "
                    "Equivalent to propose_content_file + create_resource + set_resource_property chained together."
                ),
                "parameters": _object_schema(
                    {
                        "material_path": {"type": "string", "description": "Relative output path for the ShaderMaterial, e.g. materials/glow.tres."},
                        "shader_path": {"type": "string", "description": "Relative output path for the shader source, e.g. shaders/glow.gdshader."},
                        "shader_code": {"type": "string", "description": "Complete .gdshader source code."},
                    },
                    ["material_path", "shader_path", "shader_code"],
                ),
            },
        )
    )
    register(
        ToolDef(
            name="propose_content_file",
            domain="resource",
            side="front",
            writes_project=True,
            needs_preview=True,
            render_kind="diff",
            path_args=["path"],
            schema={
                "name": "propose_content_file",
                "description": (
                    "Create or replace a project text/data file such as dialogue, quest, localization, JSON or CSV."
                ),
                "parameters": _object_schema(
                    {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                        "content_type": {"type": "string"},
                    },
                    ["path", "content"],
                ),
            },
        )
    )
