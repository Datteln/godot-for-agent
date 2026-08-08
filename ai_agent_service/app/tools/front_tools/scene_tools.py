"""Scene tool registrations for the front-facing Godot tools."""

from __future__ import annotations

from app.tools.registry import ToolDef

from app.tools.front_tools._shared import _object_schema, register


def register_scene_tools() -> None:
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
                        "path": {
                            "type": "string",
                            "description": "NodePath to the node, relative to the scene root.",
                        },
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
                        "path": {
                            "type": "string",
                            "description": "NodePath to the node, relative to the scene root.",
                        },
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
                        "path": {
                            "type": "string",
                            "description": "NodePath to the node, relative to the scene root.",
                        },
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
                    "For map objects, pass target_path plus map_cell to let Godot convert map cells through the "
                    "native TileMap transform; do not hand-calculate world pixels. map_cell and position are mutually exclusive. "
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
                        "scene_path": {
                            "type": "string",
                            "description": "Relative .tscn/.scn path to instantiate.",
                        },
                        "name": {
                            "type": "string",
                            "description": "Optional name override for the new instance root.",
                        },
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
                        "target_path": {
                            "type": "string",
                            "description": "TileMap/TileMapLayer path used to convert map_cell into the parent-local position.",
                        },
                        "map_cell": {
                            "type": "object",
                            "description": "2D map cell anchor {x,y}; use this instead of position for platform placement.",
                            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
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
                        "path": {
                            "type": "string",
                            "description": "NodePath to duplicate, relative to the scene root.",
                        },
                        "name": {
                            "type": "string",
                            "description": "Optional name override for the duplicate.",
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
                        "path": {
                            "type": "string",
                            "description": "NodePath of the signal source, relative to the scene root.",
                        },
                        "signal": {
                            "type": "string",
                            "description": "Signal name on the source node.",
                        },
                        "target_path": {
                            "type": "string",
                            "description": "NodePath of the target, relative to the scene root.",
                        },
                        "method": {
                            "type": "string",
                            "description": "Method name on the target node to call.",
                        },
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
                        "path": {
                            "type": "string",
                            "description": "NodePath of the signal source, relative to the scene root.",
                        },
                        "signal": {
                            "type": "string",
                            "description": "Signal name on the source node.",
                        },
                        "target_path": {
                            "type": "string",
                            "description": "NodePath of the target, relative to the scene root.",
                        },
                        "method": {
                            "type": "string",
                            "description": "Method name on the target node.",
                        },
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
                        "path": {
                            "type": "string",
                            "description": "NodePath, relative to the scene root.",
                        },
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
                        "path": {
                            "type": "string",
                            "description": "NodePath, relative to the scene root.",
                        },
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
                    {
                        "path": {
                            "type": "string",
                            "description": "NodePath, relative to the scene root.",
                        }
                    },
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
                    {
                        "path": {
                            "type": "string",
                            "description": "NodePath, relative to the scene root.",
                        }
                    },
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
                    {
                        "path": {
                            "type": "string",
                            "description": "NodePath, relative to the scene root.",
                        }
                    },
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
                                                "signal": {
                                                    "type": "string",
                                                    "description": "Signal name on the source node.",
                                                },
                                                "target_path": {
                                                    "type": "string",
                                                    "description": "Target NodePath relative to the scene root. Defaults to the source path.",
                                                },
                                                "method": {
                                                    "type": "string",
                                                    "description": "Target method name.",
                                                },
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
            capture_write_path_args=["output_path"],
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
                        "mode": {
                            "type": "string",
                            "enum": ["2d", "3d"],
                            "description": "Which editor viewport to capture.",
                        },
                        "viewport_index": {
                            "type": "integer",
                            "description": "3D viewport index, if multiple are open.",
                        },
                        "output_path": {
                            "type": "string",
                            "description": (
                                "Optional project-relative, res://, or user:// output path without '..'; "
                                "defaults to a temporary user:// location."
                            ),
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
                                'Map cell-coordinate rect to frame before capturing, e.g. {"x":0,"y":0,'
                                '"width":20,"height":10}. Requires target_path to identify the map node. '
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
                        "path": {
                            "type": "string",
                            "description": "Relative scene path, for example scenes/level_2.tscn.",
                        },
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
                    {
                        "path": {
                            "type": "string",
                            "description": "NodePath to the NavigationRegion2D/3D, relative to the scene root.",
                        }
                    },
                    ["path"],
                ),
            },
        )
    )
