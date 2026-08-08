"""Resource tool registrations for the front-facing Godot tools."""

from __future__ import annotations

from app.tools.registry import ToolDef

from app.tools.front_tools._shared import _object_schema, register


def register_resource_tools() -> None:
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
            capture_read_path_args=["path"],
            schema={
                "name": "read_image_metadata",
                "description": (
                    "Read image size, format and sampled dominant colors, optionally asking the multimodal "
                    "asset-understanding model a focused visual question. Use this only for visual confirmation "
                    "(for example whether a result looks correct or reachable). It is not authoritative for exact "
                    "tile coordinates, source_id, atlas coordinates, or revision; use describe_map_context or "
                    "describe_map_region for those facts."
                ),
                "parameters": _object_schema(
                    {
                        "path": {
                            "type": "string",
                            "description": "Project-relative, res://, or user:// image path without '..'.",
                        },
                        "sample_step": {"type": "integer"},
                        "question": {
                            "type": "string",
                            "maxLength": 2000,
                            "description": (
                                "Optional focused visual question. Answers are approximate visual evidence, "
                                "not exact map-data facts."
                            ),
                        },
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
                    {
                        "path": {
                            "type": "string",
                            "description": "Relative or res:// resource path.",
                        }
                    },
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
                                'ShaderMaterial), pass {"_resource_path": "res://..."} instead of a raw value.'
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
                        "player_path": {
                            "type": "string",
                            "description": "NodePath to the AnimationPlayer, relative to the scene root.",
                        },
                        "animation": {
                            "type": "string",
                            "description": "Animation name within the library.",
                        },
                        "library": {
                            "type": "string",
                            "description": "AnimationLibrary name; defaults to the unnamed default library.",
                        },
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
                                    "value": {
                                        "description": "JSON value matching the animated property's type."
                                    },
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
                        "material_path": {
                            "type": "string",
                            "description": "Relative output path for the ShaderMaterial, e.g. materials/glow.tres.",
                        },
                        "shader_path": {
                            "type": "string",
                            "description": "Relative output path for the shader source, e.g. shaders/glow.gdshader.",
                        },
                        "shader_code": {
                            "type": "string",
                            "description": "Complete .gdshader source code.",
                        },
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
