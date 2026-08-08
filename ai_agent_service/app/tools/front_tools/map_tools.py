"""Map tool registrations for the front-facing Godot tools."""

from __future__ import annotations

from app.tools.registry import ToolDef

from app.tools.front_tools._shared import _authoritative_snapshot_binding_properties, _object_schema, placement_profile_properties, register


def register_map_tools() -> None:
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
            writes_internal_cache=True,
            internal_cache_paths=(
                "res://.ai_agent_service/map_agent/resource_registry.json",
                "res://.ai_agent_service/map_agent/spatial_index.json",
            ),
            is_read_only=True,
            is_concurrency_safe=False,
            render_kind="json",
            schema={
                "name": "describe_map_context",
                "description": (
                    "Read the current scene's editable map context before planning a 2D/3D map task: "
                    "TileMapLayer/legacy TileMap/GridMap nodes, TileSet/MeshLibrary references, "
                    "resource_registry.json status, performance summary, and spatial_index.json summary. "
                    "When either fixed internal support file is missing and a compatible map exists, this same "
                    "read deterministically rebuilds only the missing file from canonical editor facts; it never "
                    "creates planner/writer work or grants general project writes. Use this as the "
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
                    "extend the level further. By default this returns summary counts/atlas distribution only "
                    "(`cells_format=summary_only`) so large reads do not flood context; request "
                    "`cells_format=non_empty_only` with `max_returned_cells` for precise occupied cells, or "
                    "`cells_format=full` only for small regions where every cell is needed. A larger-than-usual region is served whole automatically (the "
                    "response carries `auto_served: true`), so you do NOT need to pre-split normal or thin-wide reads. "
                    "A region beyond the total/absolute-axis limit fails with error_code "
                    "'region_too_large', and then it returns `suggested_regions`: smaller pre-split rectangles "
                    "covering the same area — just issue describe_map_region for each. "
                    "Hard size rule: width*height*depth must be <= 1600; additionally 2D axes must each be <=160 "
                    "and 3D axes <=40. A 100x5 2D strip is valid. If the result has error_code "
                    "'region_too_large', use its suggested_regions exactly and do not retry the original "
                    "oversized rectangle."
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
                        "width": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Region width. Keep total cells <=1600; absolute maximum 160 for 2D and 40 for 3D.",
                        },
                        "height": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Region height. Keep total cells <=1600; absolute maximum 160 for 2D and 40 for 3D.",
                        },
                        "depth": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Region depth; use 1 for 2D, keep total cells <=1600, absolute maximum 40 for 3D.",
                        },
                        "cells_format": {
                            "type": "string",
                            "enum": ["summary_only", "non_empty_only", "full"],
                            "description": (
                                "Return shape for tile details. Defaults to summary_only. Use non_empty_only "
                                "for exact occupied cells before editing; use full only for small regions."
                            ),
                        },
                        "max_returned_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Maximum cells returned when cells_format is non_empty_only or full. Defaults to 120."
                            ),
                        },
                    },
                    [],
                ),
            },
        )
    )

    register(
        ToolDef(
            name="convert_map_coords",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "convert_map_coords",
                "description": (
                    "Convert between map cell coordinates and world coordinates for a 2D TileMapLayer/legacy "
                    "TileMap or 3D GridMap, using Godot's native map_to_local/local_to_map plus the node's "
                    "global transform. ALWAYS use this instead of doing the math yourself from node_position + "
                    "tile_size: the manual formula ignores tile offsets, half-offset/isometric tile shapes and "
                    "the node's transform, and hand-computing coordinates is exactly what makes coordinate "
                    "reasoning spiral. Pass `cells` (a list of {x, y[, z]}) to get the matching `world` list back; "
                    "pass `world` (a list of {x, y[, z]}) to get the matching `cells` list back; you may pass "
                    "both. Output order matches input order. 3D uses x/y/z, 2D uses x/y."
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
                        "cells": {
                            "type": "array",
                            "description": "Cell coordinates to convert to world coordinates.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                },
                            },
                        },
                        "world": {
                            "type": "array",
                            "description": "World coordinates to convert to cell coordinates.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "number"},
                                    "y": {"type": "number"},
                                    "z": {"type": "number"},
                                },
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
                        "start": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "goal": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "waypoints": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "entrances": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "exits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
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
            name="validate_platform_level_plan",
            domain="map",
            side="front",
            reads_project=True,
            is_read_only=True,
            is_concurrency_safe=True,
            render_kind="json",
            schema={
                "name": "validate_platform_level_plan",
                "description": (
                    "Validate and compile an explicit 2D platformer level plan already authored by the LLM. "
                    "This is not a level planner or generator: the caller MUST submit ordered platforms and route "
                    "segments after reading the real map and player movement ability. The tool never invents, "
                    "randomizes, or repairs geometry. It checks entry connectivity, jump reachability, and design "
                    "constraints; invalid submissions return field-addressed issue_details/repair_plan, while "
                    "valid submissions are compiled into preview-safe edit_map batches."
                ),
                "parameters": _object_schema(
                    {
                        **_authoritative_snapshot_binding_properties(),
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
                        "entry_anchor": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "frontier": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "platforms": {
                            "type": "array",
                            "minItems": 1,
                            "description": (
                                "LLM-authored platforms in traversal order. Coordinates identify the support "
                                "row; every platform must stay inside x/y/width/height. The last platform is "
                                "the finish buffer and must satisfy min_finish_buffer_width."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "width": {"type": "integer", "minimum": 1},
                                    "role": {
                                        "type": "string",
                                        "description": (
                                            "Semantic role such as safe_intro, takeoff, landing, stair, "
                                            "hazard_entry, hazard_exit, rest, or finish."
                                        ),
                                    },
                                    "existing": {
                                        "type": "boolean",
                                        "description": "True only for already-existing support that must not be emitted.",
                                    },
                                    "connection": {"type": "boolean"},
                                },
                                "required": ["x", "y", "width", "role"],
                            },
                        },
                        "segments": {
                            "type": "array",
                            "minItems": 1,
                            "description": (
                                "LLM-authored critical-route segments in traversal order. They explain the "
                                "intended gameplay between the submitted platforms; the tool preserves rather "
                                "than generates them."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "type": {"type": "string"},
                                    "from_platform": {"type": "string"},
                                    "to_platform": {"type": "string"},
                                    "direction": {
                                        "type": "integer",
                                        "enum": [-1, 0, 1],
                                    },
                                    "start": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "integer"},
                                            "y": {"type": "integer"},
                                        },
                                    },
                                    "end": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "integer"},
                                            "y": {"type": "integer"},
                                        },
                                    },
                                    "difficulty": {"type": "integer", "minimum": 0},
                                    "note": {"type": "string"},
                                },
                                "required": [
                                    "index",
                                    "type",
                                    "from_platform",
                                    "to_platform",
                                    "direction",
                                    "start",
                                    "end",
                                ],
                            },
                        },
                        "coin_arcs": {
                            "type": "array",
                            "description": "Optional LLM-authored reward arcs; no arcs are generated automatically.",
                            "items": {"type": "object"},
                        },
                        "enemy_slots": {
                            "type": "array",
                            "description": "Optional LLM-authored enemy placements; no slots are generated automatically.",
                            "items": {"type": "object"},
                        },
                        "connect_from_existing": {
                            "type": "boolean",
                            "description": "When true, scan the left boundary and validate the first submitted platform from that real foothold. Defaults to true.",
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
                        "movement_model": {"type": "string", "enum": ["leap"]},
                        "cell_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "requires_support": {"type": "boolean"},
                        "support_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "planning_contract": {"type": "object"},
                        "min_landing_width": {
                            "type": "integer",
                            "minimum": 2,
                            "description": "Minimum safe landing platform width in cells.",
                        },
                        "actor_clearance_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Actor collision height in map cells. Required so jump arcs and "
                                "landing headroom are checked against real occupied cells."
                            ),
                        },
                        "actor_width_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "Actor collision width in map cells, measured from the "
                                "real CollisionShape2D."
                            ),
                        },
                        "collision_facts_artifact": {
                            "type": "object",
                            "description": (
                                "Optional reader artifact bound to target, layer, revision, "
                                "region and digest. Raw collision-cell dictionaries are rejected."
                            ),
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
                    # 必填字段：包含平台约束（max_horizontal_gap/max_rise/max_fall/min_landing_width）
                    # 和角色碰撞高度（actor_clearance_cells），确保生成的关卡在物理上可达
                    [
                        "x",
                        "y",
                        "width",
                        "height",
                        "platforms",
                        "segments",
                        "max_horizontal_gap",
                        "max_rise",
                        "max_fall",
                        "min_landing_width",
                        "actor_clearance_cells",
                        "actor_width_cells",
                        "movement_model",
                        "cell_occupancy",
                        "requires_support",
                        "support_occupancy",
                    ],
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
                    "Supports profile='platformer' (validates and compiles LLM-authored platforms/segments), 'topdown' "
                    "(connected roads/ground), 'dungeon' (rooms and corridors), and '3d_grid' "
                    "(connected floor strips). Returns candidates, accepted_motifs, preview-safe "
                    "edit_map_batches, validation, and repair strategies; it never edits the scene. For "
                    "profile='platformer', platforms and segments are required and no geometry is generated "
                    "automatically."
                ),
                "parameters": _object_schema(
                    {
                        **_authoritative_snapshot_binding_properties(),
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
                        "platforms": {
                            "type": "array",
                            "minItems": 1,
                            "description": "Required for profile='platformer': LLM-authored platforms in traversal order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string"},
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "width": {"type": "integer", "minimum": 1},
                                    "role": {"type": "string"},
                                    "existing": {"type": "boolean"},
                                    "connection": {"type": "boolean"},
                                },
                                "required": ["x", "y", "width", "role"],
                            },
                        },
                        "segments": {
                            "type": "array",
                            "minItems": 1,
                            "description": "Required for profile='platformer': LLM-authored route segments.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "type": {"type": "string"},
                                    "from_platform": {"type": "string"},
                                    "to_platform": {"type": "string"},
                                    "direction": {
                                        "type": "integer",
                                        "enum": [-1, 0, 1],
                                    },
                                    "start": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "integer"},
                                            "y": {"type": "integer"},
                                        },
                                    },
                                    "end": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "integer"},
                                            "y": {"type": "integer"},
                                        },
                                    },
                                    "difficulty": {"type": "integer", "minimum": 0},
                                    "note": {"type": "string"},
                                },
                                "required": [
                                    "index",
                                    "type",
                                    "from_platform",
                                    "to_platform",
                                    "direction",
                                    "start",
                                    "end",
                                ],
                            },
                        },
                        "coin_arcs": {
                            "type": "array",
                            "description": "Optional LLM-authored reward arcs for profile='platformer'.",
                            "items": {"type": "object"},
                        },
                        "enemy_slots": {
                            "type": "array",
                            "description": "Optional LLM-authored enemy slots for profile='platformer'.",
                            "items": {"type": "object"},
                        },
                        "frontier": {
                            "type": "object",
                            "description": "Optional known reachable frontier cell {x,y[,z]}; platformer can auto-sample it from the left boundary.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "entry_anchor": {
                            "type": "object",
                            "description": "Optional explicit growth anchor, using actor_cell or support_cell coordinates.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "start": {
                            "type": "object",
                            "description": "Optional real player/unit start. When provided, plan_reachable_map_growth first computes reachable_frontier from real map reachability.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "frontier_type": {"type": "string"},
                        "cell_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "requires_support": {"type": "boolean"},
                        "support_occupancy": {"type": "string", "enum": ["empty", "filled"]},
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
                        "frontier_axis": {"type": "string", "enum": ["x", "y", "z"]},
                        "frontier_sign": {"type": "integer", "enum": [-1, 1]},
                        "planning_contract": {"type": "object"},
                        "max_returned_cells": {"type": "integer", "minimum": 1},
                        "min_landing_width": {"type": "integer", "minimum": 2},
                        "actor_clearance_cells": {"type": "integer", "minimum": 1},
                        "actor_width_cells": {"type": "integer", "minimum": 1},
                        "collision_facts_artifact": {"type": "object"},
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
                    [
                        "profile",
                        "x",
                        "y",
                        "width",
                        "height",
                        "movement_model",
                        "cell_occupancy",
                        "requires_support",
                        "support_occupancy",
                    ],
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
                    "reachable_cells, reachable_footholds, reachable_frontier, frontier_candidates, and "
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
                            "description": "Real start anchor. role='actor_cell' means the occupied character cell; role='support_cell' means the ground/support cell.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "cell_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "requires_support": {"type": "boolean"},
                        "support_occupancy": {"type": "string", "enum": ["empty", "filled"]},
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
                        "frontier_axis": {"type": "string", "enum": ["x", "y", "z"]},
                        "frontier_sign": {"type": "integer", "enum": [-1, 1]},
                        "planning_contract": {"type": "object"},
                        "max_returned_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "Caps returned reachable_cells/footholds payload size; search still visits the whole region.",
                        },
                    },
                    [
                        "start",
                        "x",
                        "y",
                        "width",
                        "height",
                        "movement_model",
                        "cell_occupancy",
                        "requires_support",
                        "support_occupancy",
                    ],
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
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                },
                            },
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
                    "approximating it out of tiles. `expected_cells` is required (the number of cells this batch should "
                    "write, e.g. an inclusive x=A..B span is B-A+1 columns × the fill height) so the tool can "
                    "reject an off-by-one batch (error_code 'cell_count_mismatch') before any tiles are written, "
                    "instead of discovering the gap later in validate_map_region. The tool also rejects oversized "
                    "batches and thin, non-blanket fills that look like broad map repair; split those into local "
                    "segments, or mark true backdrop/water/sky work with the matching semantic_layer/tags. For a "
                    "platformer level extension, do not invent a ground-fill wall here: first have the LLM submit "
                    "explicit platforms/segments to validate_platform_level_plan with measured player ability, then apply "
                    "only its validated route batches."
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
                                        "description": (
                                            "Legacy readback metadata only. Do not send raw TileSet ids "
                                            "for fill; select resource/resource_key from the verified "
                                            "resource registry."
                                        ),
                                    },
                                    "atlas_x": {
                                        "type": "integer",
                                        "description": (
                                            "Legacy readback metadata only; fill must use a registered "
                                            "resource/resource_key."
                                        ),
                                    },
                                    "atlas_y": {
                                        "type": "integer",
                                        "description": (
                                            "Legacy readback metadata only; fill must use a registered "
                                            "resource/resource_key."
                                        ),
                                    },
                                    "reference_cell": {
                                        "type": "object",
                                        "description": (
                                            "Required for a ground-tagged 2D fill when the target layer already has tiles. "
                                            "Use an existing real ground cell read by describe_map_region; its atlas must "
                                            "match this fill, preventing a mislabeled bridge/decor tile from becoming terrain."
                                        ),
                                        "properties": {
                                            "x": {"type": "integer"},
                                            "y": {"type": "integer"},
                                        },
                                        "required": ["x", "y"],
                                    },
                                    "alternative_tile": {"type": "integer"},
                                    "item": {
                                        "type": "integer",
                                        "description": "3D MeshLibrary item id for fill. Raw ids are rejected unless resolved through a registered resource.",
                                    },
                                    "orientation": {
                                        "type": "integer",
                                        "description": "3D GridMap orthogonal orientation index.",
                                    },
                                    "resource": {
                                        "type": "string",
                                        "description": (
                                            "Registered semantic resource key from "
                                            "resource_registry.json. Required for fill operations; "
                                            "never invent a key or replace it with raw atlas ids."
                                        ),
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
                                    "visual_group_id": {
                                        "type": "string",
                                        "description": (
                                            "Optional stable id for one visible instance made from one or more tile cells "
                                            "(for example tree_01). Cells with the same id are summarized together in "
                                            "the edit result and spatial index."
                                        ),
                                    },
                                    "instance_id": {
                                        "type": "string",
                                        "description": "Alias for visual_group_id.",
                                    },
                                    "instance_kind": {
                                        "type": "string",
                                        "description": "Optional visible instance kind such as tree, bush, sign, trap, building.",
                                    },
                                    "required_cells": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "description": (
                                            "Minimum cells expected for this visual_group_id. If the group writes fewer "
                                            "cells, edit_map returns a visual_group_warning."
                                        ),
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
                                "Required self-check: the number of cells this batch is supposed to write. "
                                "The frontend rejects a mismatch before any tiles are written."
                            ),
                        },
                        "expected_visual_groups": {
                            "type": "integer",
                            "minimum": 0,
                            "description": (
                                "Optional expected count of visible instances represented by operation "
                                "visual_group_id/instance_id values. Use for decoration/object goals so completion "
                                "is checked by instance count, not just total cells."
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
                    "spatial index, and can record placements back into the index. The x/y coords are the object's "
                    "empty footprint cell, not the solid ground cell; ground support is checked below the footprint. "
                    "For coins use placement_kind='coin' or requires_support=false. Previewed and undoable."
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
                        "map_layer": {
                            "type": "integer",
                            "description": (
                                "Legacy TileMap layer used to check footprint emptiness and support. Required for "
                                "multi-layer TileMap; choose it from describe_map_region.layers."
                            ),
                        },
                        "ground_map_layer": {
                            "type": "integer",
                            "description": "Alias for map_layer when validating object support on the foreground layer.",
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
                                    "placement_kind": {
                                        "type": "string",
                                        "description": "Object placement preset such as coin, enemy, npc, tree, chest.",
                                    },
                                    "kind": {"type": "string"},
                                    "anchor": {
                                        "type": "string",
                                        "enum": [
                                            "bottom_center",
                                            "bottom_left",
                                            "bottom_right",
                                            "top_center",
                                            "top_left",
                                            "top_right",
                                            "center",
                                        ],
                                    },
                                    "surface_type": {
                                        "type": "string",
                                        "enum": [
                                            "ground",
                                            "wall",
                                            "water_surface",
                                            "water",
                                            "air",
                                            "room_center",
                                            "branch_end",
                                            "path_edge",
                                        ],
                                    },
                                    "footprint_width": {"type": "integer", "minimum": 1},
                                    "footprint_height": {"type": "integer", "minimum": 1},
                                    "requires_support": {"type": "boolean"},
                                    "support_layers": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "semantic_layer": {"type": "string"},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                    "visual_group_id": {
                                        "type": "string",
                                        "description": "Optional stable id for this visible object instance.",
                                    },
                                    "instance_id": {
                                        "type": "string",
                                        "description": "Alias for visual_group_id.",
                                    },
                                    "instance_kind": {
                                        "type": "string",
                                        "description": "Optional visible object kind such as tree, coin, enemy, sign.",
                                    },
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
                        "map_layer": {
                            "type": "integer",
                            "description": "Restrict 2D matches to one TileMap layer when entries record map_layer.",
                        },
                        "visual_group_id": {
                            "type": "string",
                            "description": "Match entries belonging to this visible instance/group id.",
                        },
                        "instance_id": {
                            "type": "string",
                            "description": "Alias for visual_group_id.",
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
                    "Declare actor occupancy with cell_occupancy, support requirements with requires_support, and "
                    "support occupancy with support_occupancy. Optionally override gravity direction via "
                    "gravity_axis/gravity_sign. It can also enforce "
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
                    "check, regardless of whether the gap was introduced by a recent edit or was already there. "
                    "The result includes `completion_allowed` and `blocking_completion`; final completion is only "
                    "allowed after a validation with real route endpoints/waypoints (or entrances/exits) passes and "
                    "`completion_allowed=true`. Oversized validation returns `blocking_completion=true` and must be "
                    "split into route segments instead of being ignored. HARD LIMIT: the region width*height (*depth) "
                    "must be <= 1600 cells per call; a larger region is rejected with error_code='region_too_large'. "
                    "Plan for this BEFORE calling — split a long route into segments and validate each with its own "
                    "start/goal, keeping each segment's support row inside its region, rather than sending one big "
                    "region and reacting to the error."
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
                            "description": "Layer index for a legacy TileMap. Omit for TileMapLayer and GridMap.",
                        },
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "z": {"type": "integer"},
                        "width": {"type": "integer", "minimum": 1},
                        "height": {"type": "integer", "minimum": 1},
                        "depth": {"type": "integer", "minimum": 1},
                        "validation_mode": {
                            "type": "string",
                            "enum": ["diagnostic", "completion"],
                            "description": (
                                "Use diagnostic only to locate one failure frontier after completion fails. "
                                "Use completion for the frozen user acceptance route; completion runs at most once "
                                "per map revision and its start/goal/waypoints/movement limits cannot drift. "
                                "Completion requires start+goal, non-empty entrances+exits, or at least two "
                                "waypoints. When omitted, calls with a real route infer completion; region/layer-only "
                                "checks infer diagnostic and never freeze a completion contract."
                            ),
                        },
                        "start": {
                            "type": "object",
                            "description": "Optional BFS start cell {x, y[, z]} in map coordinates.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "goal": {
                            "type": "object",
                            "description": "Optional BFS goal cell {x, y[, z]} in map coordinates.",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "waypoints": {
                            "type": "array",
                            "description": "Optional ordered cells that the path must pass through.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "entrances": {
                            "type": "array",
                            "description": "Optional entrance cells; each entrance must reach at least one exit.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "exits": {
                            "type": "array",
                            "description": "Optional exit cells used with entrances.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "cell_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "requires_support": {"type": "boolean"},
                        "support_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "planning_contract": {"type": "object"},
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
                        "actor_clearance_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "leap: actor collision height in cells; every sampled jump arc "
                                "and landing cell must have this much clearance."
                            ),
                        },
                        "actor_width_cells": {
                            "type": "integer",
                            "minimum": 1,
                            "description": (
                                "leap: actor collision width in cells; takeoff, every "
                                "sampled footprint, and landing support must fit."
                            ),
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
                    [
                        "x",
                        "y",
                        "width",
                        "height",
                        "movement_model",
                        "cell_occupancy",
                        "requires_support",
                        "support_occupancy",
                    ],
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
                    "connectivity it applies cell_occupancy to the corridor. When requires_support=true it instead "
                    "applies support_occupancy to the SUPPORT row beneath the path. For leap failures this normally fills the "
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
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "goal": {
                            "type": "object",
                            "properties": {
                                "x": {"type": "integer"},
                                "y": {"type": "integer"},
                                "z": {"type": "integer"},
                                "role": {"type": "string", "enum": ["actor_cell", "support_cell"]},
                            },
                        },
                        "waypoints": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "entrances": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "exits": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "x": {"type": "integer"},
                                    "y": {"type": "integer"},
                                    "z": {"type": "integer"},
                                    "role": {
                                        "type": "string",
                                        "enum": ["actor_cell", "support_cell"],
                                    },
                                },
                            },
                        },
                        "cell_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "requires_support": {"type": "boolean"},
                        "support_occupancy": {"type": "string", "enum": ["empty", "filled"]},
                        "movement_model": {
                            "type": "string",
                            "enum": ["grid", "leap", "free"],
                            "description": "Must match the movement_model used in validate_map_region. 'leap' bridges gaps by filling the support row beneath the path.",
                        },
                        "max_horizontal_gap": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "leap: max horizontal jump distance in cells.",
                        },
                        "max_rise": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "leap: max jump height gain in cells.",
                        },
                        "max_fall": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "leap: max drop in cells treated as reachable.",
                        },
                        "max_step": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "free: max single-move distance in cells.",
                        },
                        "gravity_axis": {
                            "type": "string",
                            "enum": ["x", "y", "z"],
                            "description": "Optional 'down' axis override; must match validate_map_region.",
                        },
                        "gravity_sign": {
                            "type": "integer",
                            "enum": [-1, 1],
                            "description": "Sign of the gravity axis; used with gravity_axis.",
                        },
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
                    [
                        "x",
                        "y",
                        "width",
                        "height",
                        "movement_model",
                        "cell_occupancy",
                        "requires_support",
                        "support_occupancy",
                    ],
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
                        "seed": {
                            "type": "integer",
                            "description": "Noise seed for reproducibility.",
                        },
                        "frequency": {
                            "type": "number",
                            "description": "Noise frequency; defaults to 0.05.",
                        },
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
                    "undoable. Entries are validated as resource contracts; only record resources you have "
                    "verified exist via describe_map_context or describe_map_region."
                ),
                "parameters": _object_schema(
                    {
                        "entries": {
                            "type": "object",
                            "description": (
                                "Object mapping each resource key to its definition object. Each entry must "
                                "declare kind and either 2D source_id+atlas_coords/atlas_x+atlas_y, 3D item/"
                                "mesh_library_item, or scene_path. footprint defaults to 1x1 and required_cells "
                                "defaults to footprint area."
                            ),
                            "additionalProperties": {
                                "type": "object",
                                "properties": {
                                    "kind": {"type": "string"},
                                    "display_name": {"type": "string"},
                                    "mode": {"type": "string", "enum": ["2d", "3d"]},
                                    "target": {"type": "string"},
                                    "source_id": {"type": "integer"},
                                    "atlas_x": {"type": "integer"},
                                    "atlas_y": {"type": "integer"},
                                    "atlas_coords": {
                                        "type": "object",
                                        "properties": {
                                            "x": {"type": "integer"},
                                            "y": {"type": "integer"},
                                        },
                                    },
                                    "item": {"type": "integer"},
                                    "mesh_library_item": {"type": "integer"},
                                    "scene_path": {"type": "string"},
                                    "footprint": {
                                        "type": "object",
                                        "properties": {
                                            "width": {"type": "integer", "minimum": 1},
                                            "height": {"type": "integer", "minimum": 1},
                                            "depth": {"type": "integer", "minimum": 1},
                                        },
                                    },
                                    "required_cells": {"type": "integer", "minimum": 1},
                                    "visual_group_id": {"type": "string"},
                                    "tags": {"type": "array", "items": {"type": "string"}},
                                    "cost": {"type": "number"},
                                },
                                "required": ["kind"],
                            },
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
                        "x": {
                            "type": "integer",
                            "description": "Destination origin x for the blueprint.",
                        },
                        "y": {
                            "type": "integer",
                            "description": "Destination origin y for the blueprint.",
                        },
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
