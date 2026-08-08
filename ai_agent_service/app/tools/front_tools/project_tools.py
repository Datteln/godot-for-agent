"""Project tool registrations for the front-facing Godot tools."""

from __future__ import annotations

from app.tools.registry import ToolDef

from app.tools.front_tools._shared import _object_schema, register


def register_project_tools() -> None:
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
                        "value": {
                            "description": "JSON value to assign, or null to clear the override."
                        },
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
                    {
                        "key": {
                            "type": "string",
                            "description": "Setting key, for example application/run/main_scene.",
                        }
                    },
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
                        "name": {
                            "type": "string",
                            "description": "Autoload identifier, used as the global singleton name.",
                        },
                        "path": {
                            "type": "string",
                            "description": "Relative .gd/.tscn/.cs path to autoload.",
                        },
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
                                "enum": [
                                    "left",
                                    "right",
                                    "middle",
                                    "wheel_up",
                                    "wheel_down",
                                    "wheel_left",
                                    "wheel_right",
                                    "xbutton1",
                                    "xbutton2",
                                ],
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
