@tool
extends RefCounted

const MapValidator = preload("res://addons/ai_agent/tools/map_validator.gd")


static func build_algorithm_plan(input: Dictionary, context: Dictionary = {}) -> Dictionary:
	var mode := _mode_from_input(input)
	var dimension := 3 if mode == "3d" else 2
	var region := _region_from_input(input, dimension)
	var seed := int(input.get("seed", 0))
	var zone_plan := build_zone_plan(input, region, mode)
	var poisson := sample_poisson_points({
		"dimension": mode,
		"x": region["x"],
		"y": region["y"],
		"z": region["z"],
		"width": region["width"],
		"height": region["height"],
		"depth": region["depth"],
		"min_distance": input.get("min_object_distance", 4),
		"max_points": input.get("max_object_points", 48),
		"seed": seed,
		"zones": zone_plan.get("zones", []),
	})
	var grammar := compose_blueprint_grammar({
		"dimension": mode,
		"region": region,
		"zones": zone_plan.get("zones", []),
		"anchors": zone_plan.get("anchors", {}),
		"blueprints": input.get("blueprints", []),
		"pattern": input.get("pattern", input.get("theme", "generic")),
		"seed": seed,
	})
	var constraints := build_constraint_plan(input, region, mode, zone_plan, poisson, grammar)
	return {
		"ok": true,
		"algorithm_stack": [
			"zone_planning",
			"poisson_disk_sampling",
			"astar_or_navmesh_validation",
			"grammar_blueprint_composer",
			"constraint_validator_repair",
		],
		"mode": mode,
		"dimension": dimension,
		"region": region,
		"context_summary": _context_summary(context),
		"zones": zone_plan.get("zones", []),
		"anchors": zone_plan.get("anchors", {}),
		"poisson_points": poisson.get("points", []),
		"poisson": poisson,
		"grammar": grammar,
		"constraints": constraints,
		"execution_order": [
			"describe_map_region for boundary reality",
			"edit_map or paint_terrain_connect for base zones",
			"apply_map_blueprint for grammar/prefab stamps",
			"place_map_objects using poisson_points for props",
			"validate_map_region with astar / bake_navigation_mesh when NavigationRegion exists",
			"repair_map_region for connectivity, overlaps, and blocked-object issues",
		],
	}


static func build_zone_plan(input: Dictionary, region: Dictionary, mode: String = "") -> Dictionary:
	var resolved_mode := mode if mode != "" else _mode_from_input(input)
	if resolved_mode == "3d":
		return _build_3d_zones(input, region)
	return _build_2d_zones(input, region)


static func sample_poisson_points(input: Dictionary) -> Dictionary:
	var mode := _mode_from_input(input)
	var dimension := 3 if mode == "3d" else 2
	var region := _region_from_input(input, dimension)
	var min_distance := max(1, int(input.get("min_distance", 4)))
	var max_points := max(0, int(input.get("max_points", 64)))
	var seed := int(input.get("seed", 0))
	var zone_filter := str(input.get("zone", "")).strip_edges()
	var zones := input.get("zones", [])
	var candidates := _candidate_cells(region, dimension)
	candidates.sort_custom(func(a: Vector3i, b: Vector3i) -> bool:
		return _hash_cell(a, seed) < _hash_cell(b, seed)
	)
	var points: Array = []
	for cell in candidates:
		if max_points > 0 and points.size() >= max_points:
			break
		if zone_filter != "" and not _cell_in_named_zone(cell, zones, zone_filter, dimension):
			continue
		if _is_excluded(cell, input.get("exclude", []), dimension):
			continue
		if not _far_enough(cell, points, min_distance, dimension):
			continue
		points.append(_coord_payload(cell, dimension))
	return {
		"ok": true,
		"algorithm": "poisson_disk_sampling_grid",
		"dimension": dimension,
		"region": region,
		"min_distance": min_distance,
		"max_points": max_points,
		"seed": seed,
		"points": points,
		"count": points.size(),
	}


static func compose_blueprint_grammar(input: Dictionary) -> Dictionary:
	var mode := _mode_from_input(input)
	var dimension := 3 if mode == "3d" else 2
	var region := _region_from_input(input.get("region", input), dimension)
	var blueprints = input.get("blueprints", [])
	var zones = input.get("zones", [])
	var anchors: Dictionary = input.get("anchors", {}) if input.get("anchors", {}) is Dictionary else {}
	var seed := int(input.get("seed", 0))
	var pattern := str(input.get("pattern", "generic"))
	var stamps: Array = []
	var missing: Array = []
	if blueprints is Array and not (blueprints as Array).is_empty():
		var slots := _grammar_slots(region, dimension, zones, anchors)
		var index := 0
		for slot in slots:
			if index >= (blueprints as Array).size():
				index = 0
			var blueprint_name := str((blueprints as Array)[index]).strip_edges()
			if blueprint_name == "":
				missing.append("empty_blueprint_name")
				index += 1
				continue
			var coords: Vector3i = slot.get("coords", Vector3i.ZERO)
			stamps.append({
				"tool": "apply_map_blueprint",
				"name": blueprint_name,
				"x": coords.x,
				"y": coords.y,
				"z": coords.z if dimension == 3 else null,
				"role": str(slot.get("role", "module")),
			})
			index += 1
	else:
		stamps = _fallback_grammar_ops(region, dimension, pattern, seed)
	return {
		"ok": missing.is_empty(),
		"algorithm": "grammar_blueprint_composer",
		"pattern": pattern,
		"dimension": dimension,
		"stamps": stamps,
		"missing": missing,
		"notes": [
			"Use apply_map_blueprint for stamps with a name.",
			"Use edit_map/paint_terrain_connect for fallback operation drafts.",
		],
	}


static func build_constraint_plan(
	input: Dictionary,
	region: Dictionary,
	mode: String,
	zone_plan: Dictionary,
	poisson: Dictionary,
	grammar: Dictionary
) -> Dictionary:
	var anchors: Dictionary = zone_plan.get("anchors", {})
	var start = input.get("start", anchors.get("entry", {}))
	var goal = input.get("goal", anchors.get("exit", {}))
	var waypoints = input.get("waypoints", anchors.get("waypoints", []))
	var entrances = input.get("entrances", [anchors.get("entry", {})])
	var exits = input.get("exits", [anchors.get("exit", {})])
	var density_limit := max(1, int(input.get("max_density_per_100_cells", 18)))
	var cells := max(1, int(region.get("width", 1)) * int(region.get("height", 1)) * int(region.get("depth", 1)))
	var point_count := int(poisson.get("count", 0))
	var density_score := float(point_count) * 100.0 / float(cells)
	var issues: Array = []
	if density_score > density_limit:
		issues.append("poisson object density is above max_density_per_100_cells")
	if not bool(grammar.get("ok", true)):
		issues.append("grammar composer has missing blueprint names")
	return {
		"passed": issues.is_empty(),
		"issues": issues,
		"density_per_100_cells": density_score,
		"validate_map_region": {
			"x": region.get("x", 0),
			"y": region.get("y", 0),
			"z": region.get("z", 0) if mode == "3d" else null,
			"width": region.get("width", 1),
			"height": region.get("height", 1),
			"depth": region.get("depth", 1) if mode == "3d" else null,
			"start": start,
			"goal": goal,
			"entrances": entrances,
			"exits": exits,
			"waypoints": waypoints,
			"path_algorithm": "astar",
			"check_overlaps": true,
			"check_blocked_objects": true,
		},
		"repair_map_region": {
			"repair_overlaps": true,
			"repair_blocked_objects": true,
			"path_algorithm": "astar",
		},
	}


static func _build_2d_zones(input: Dictionary, region: Dictionary) -> Dictionary:
	var x := int(region.get("x", 0))
	var y := int(region.get("y", 0))
	var width := int(region.get("width", 1))
	var height := int(region.get("height", 1))
	var center_x := x + width / 2
	var center_y := y + height / 2
	var water_h := max(1, height / 5)
	var building_w := max(3, width / 4)
	var building_h := max(3, height / 4)
	var zones: Array = [
		{"name": "land", "semantic_layer": "ground", "rect": region, "priority": 10},
		{"name": "water", "semantic_layer": "water", "rect": {"x": x, "y": y + height - water_h, "width": width, "height": water_h}, "priority": 20},
		{"name": "main_path", "semantic_layer": "road", "line": {"from": {"x": x, "y": center_y}, "to": {"x": x + width - 1, "y": center_y}}, "priority": 30},
		{"name": "building", "semantic_layer": "building", "rect": {"x": center_x - building_w / 2, "y": y + 1, "width": building_w, "height": building_h}, "priority": 40},
		{"name": "obstacle", "semantic_layer": "obstacle", "rect": {"x": x + 1, "y": y + 1, "width": max(1, width - 2), "height": max(1, height - water_h - 2)}, "density": "low", "priority": 50},
		{"name": "decor", "semantic_layer": "decor", "rect": region, "density": str(input.get("density", "medium")), "priority": 60},
	]
	return {
		"zones": zones,
		"anchors": {
			"entry": {"x": x, "y": center_y},
			"exit": {"x": x + width - 1, "y": center_y},
			"center": {"x": center_x, "y": center_y},
			"waypoints": [{"x": center_x, "y": center_y}],
		},
	}


static func _build_3d_zones(input: Dictionary, region: Dictionary) -> Dictionary:
	var x := int(region.get("x", 0))
	var y := int(region.get("y", 0))
	var z := int(region.get("z", 0))
	var width := int(region.get("width", 1))
	var height := int(region.get("height", 1))
	var depth := int(region.get("depth", 1))
	var center_x := x + width / 2
	var center_y := y + height / 2
	var inner := {"x": x + 1, "y": y + 1, "z": z, "width": max(1, width - 2), "height": max(1, height - 2), "depth": depth}
	return {
		"zones": [
			{"name": "floor", "semantic_layer": "floor", "rect": region, "priority": 10},
			{"name": "walls", "semantic_layer": "wall", "perimeter": region, "priority": 20},
			{"name": "doors", "semantic_layer": "door", "points": [{"x": center_x, "y": y, "z": z}, {"x": center_x, "y": y + height - 1, "z": z}], "priority": 30},
			{"name": "obstacle", "semantic_layer": "obstacle", "rect": inner, "density": "low", "priority": 40},
			{"name": "props", "semantic_layer": "props", "rect": inner, "density": str(input.get("density", "medium")), "priority": 50},
		],
		"anchors": {
			"entry": {"x": center_x, "y": y, "z": z},
			"exit": {"x": center_x, "y": y + height - 1, "z": z},
			"center": {"x": center_x, "y": center_y, "z": z},
			"waypoints": [{"x": center_x, "y": center_y, "z": z}],
		},
	}


static func _candidate_cells(region: Dictionary, dimension: int) -> Array:
	var cells: Array = []
	for dz in range(int(region.get("depth", 1))):
		for dy in range(int(region.get("height", 1))):
			for dx in range(int(region.get("width", 1))):
				cells.append(Vector3i(int(region["x"]) + dx, int(region["y"]) + dy, int(region.get("z", 0)) + dz if dimension == 3 else 0))
	return cells


static func _far_enough(cell: Vector3i, points: Array, min_distance: int, dimension: int) -> bool:
	var min_squared := min_distance * min_distance
	for point_value in points:
		var point := MapValidator.coord_from_input(point_value, dimension)
		var dx := cell.x - point.x
		var dy := cell.y - point.y
		var dz := cell.z - point.z if dimension == 3 else 0
		if dx * dx + dy * dy + dz * dz < min_squared:
			return false
	return true


static func _is_excluded(cell: Vector3i, exclusions, dimension: int) -> bool:
	if not (exclusions is Array):
		return false
	for item in exclusions:
		if not (item is Dictionary):
			continue
		var excluded := MapValidator.coord_from_input(item, dimension)
		if excluded == cell:
			return true
	return false


static func _cell_in_named_zone(cell: Vector3i, zones, zone_name: String, dimension: int) -> bool:
	if not (zones is Array):
		return true
	for zone_value in zones:
		if not (zone_value is Dictionary):
			continue
		var zone: Dictionary = zone_value
		if str(zone.get("name", "")) != zone_name and str(zone.get("semantic_layer", "")) != zone_name:
			continue
		if _cell_in_zone(cell, zone, dimension):
			return true
	return false


static func _cell_in_zone(cell: Vector3i, zone: Dictionary, dimension: int) -> bool:
	var rect = zone.get("rect", {})
	if not (rect is Dictionary):
		return false
	var region := _region_from_input(rect, dimension)
	return cell.x >= int(region["x"]) and cell.x < int(region["x"]) + int(region["width"]) \
		and cell.y >= int(region["y"]) and cell.y < int(region["y"]) + int(region["height"]) \
		and (dimension == 2 or (cell.z >= int(region["z"]) and cell.z < int(region["z"]) + int(region["depth"])))


static func _grammar_slots(region: Dictionary, dimension: int, zones, anchors: Dictionary) -> Array:
	var slots: Array = []
	if anchors.has("entry"):
		slots.append({"role": "entry_module", "coords": MapValidator.coord_from_input(anchors["entry"], dimension)})
	if anchors.has("center"):
		slots.append({"role": "center_module", "coords": MapValidator.coord_from_input(anchors["center"], dimension)})
	if anchors.has("exit"):
		slots.append({"role": "exit_module", "coords": MapValidator.coord_from_input(anchors["exit"], dimension)})
	for zone_value in zones:
		if not (zone_value is Dictionary):
			continue
		var zone: Dictionary = zone_value
		if not zone.has("rect"):
			continue
		var rect := _region_from_input(zone["rect"], dimension)
		slots.append({
			"role": str(zone.get("name", "zone_module")),
			"coords": Vector3i(int(rect["x"]), int(rect["y"]), int(rect.get("z", 0))),
		})
	return slots


static func _fallback_grammar_ops(region: Dictionary, dimension: int, pattern: String, seed: int) -> Array:
	var x := int(region.get("x", 0))
	var y := int(region.get("y", 0))
	var z := int(region.get("z", 0))
	var width := int(region.get("width", 1))
	var height := int(region.get("height", 1))
	var mid_y := y + height / 2
	if dimension == 3:
		return [
			{"tool": "edit_map", "action": "fill", "semantic_layer": "floor", "x": x, "y": y, "z": z, "width": width, "height": height, "depth": 1},
			{"tool": "edit_map", "action": "fill", "semantic_layer": "wall", "note": "fill perimeter batches; leave door cells from anchors empty"},
		]
	return [
		{"tool": "edit_map", "action": "fill", "semantic_layer": "ground", "x": x, "y": mid_y, "width": width, "height": max(1, height / 2)},
		{"tool": "paint_terrain_connect", "semantic_layer": "road", "x": x, "y": mid_y, "width": width, "height": 1},
		{"tool": "sample_poisson_points", "semantic_layer": "decor", "seed": seed, "pattern": pattern},
	]


static func _region_from_input(value, dimension: int) -> Dictionary:
	var input: Dictionary = value if value is Dictionary else {}
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var z := int(input.get("z", 0)) if dimension == 3 else 0
	var width := max(1, int(input.get("width", 40 if dimension == 2 else 12)))
	var height := max(1, int(input.get("height", 30 if dimension == 2 else 12)))
	var depth := max(1, int(input.get("depth", 1))) if dimension == 3 else 1
	return {"x": x, "y": y, "z": z, "width": width, "height": height, "depth": depth}


static func _mode_from_input(input: Dictionary) -> String:
	var raw := str(input.get("dimension", input.get("mode", "2d"))).to_lower()
	return "3d" if raw == "3d" else "2d"


static func _coord_payload(cell: Vector3i, dimension: int) -> Dictionary:
	return {"x": cell.x, "y": cell.y, "z": cell.z} if dimension == 3 else {"x": cell.x, "y": cell.y}


static func _hash_cell(cell: Vector3i, seed: int) -> int:
	var value := int(cell.x) * 73856093 ^ int(cell.y) * 19349663 ^ int(cell.z) * 83492791 ^ seed * 2654435761
	return absi(value)


static func _context_summary(context: Dictionary) -> Dictionary:
	if context.is_empty():
		return {}
	return {
		"maps": (context.get("maps", []) as Array).size() if context.get("maps", []) is Array else 0,
		"spatial_index": context.get("spatial_index", {}),
		"registry_exists": bool(context.get("resource_registry", {}).get("exists", false)) if context.get("resource_registry", {}) is Dictionary else false,
	}
