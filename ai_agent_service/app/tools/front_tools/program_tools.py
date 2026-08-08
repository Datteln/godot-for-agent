"""Program tool registrations for the front-facing Godot tools."""

from __future__ import annotations

from app.tools.registry import ToolDef

from app.tools.front_tools._shared import _object_schema, register


def register_program_tools() -> None:
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
                        "path": {
                            "type": "string",
                            "description": "Optional relative path to scope the diff to.",
                        },
                        "staged": {
                            "type": "boolean",
                            "description": "Show staged changes instead of the working tree.",
                        },
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
                        "preset": {
                            "type": "string",
                            "description": "Export preset name, from list_export_presets.",
                        },
                        "output_path": {
                            "type": "string",
                            "description": "Project-relative output file path.",
                        },
                        "debug": {
                            "type": "boolean",
                            "description": "Export a debug build instead of release.",
                        },
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
