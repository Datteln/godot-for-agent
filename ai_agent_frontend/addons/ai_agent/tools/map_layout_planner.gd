@tool
extends RefCounted

const MapLayerScaffold = preload("res://addons/ai_agent/tools/map_layer_scaffold.gd")


static func plan(intent: Dictionary, context: Dictionary) -> Dictionary:
	var mode := str(intent.get("mode", "2d"))
	var region := _normalized_region(intent.get("region", {}), mode)
	var resource_keys := _resource_keys_for_intent(intent)
	var missing_resources := _missing_resources(resource_keys, context)
	var fallback_resources := _fallback_resources(missing_resources, context)
	var ready := missing_resources.is_empty() or _all_missing_have_fallback(missing_resources, fallback_resources)
	var steps := _plan_steps(intent, region, missing_resources, fallback_resources)
	return {
		"mode": mode,
		"task": str(intent.get("task", "generate")),
		"theme": str(intent.get("theme", "generic")),
		"region": region,
		"required_resources": resource_keys,
		"missing_resources": missing_resources,
		"fallback_resources": fallback_resources,
		"standard_layers": MapLayerScaffold.STANDARD_3D_LAYERS if mode == "3d" else MapLayerScaffold.STANDARD_2D_LAYERS,
		"layout": _layout_for_mode(intent, region),
		"steps": steps,
		"operation_drafts": _operation_drafts(intent, region, missing_resources),
		"validation": _validation_plan(intent, region),
		"path_constraints": _path_constraints(intent, region),
		"performance": _performance_plan(intent, region),
		"ready_for_edit_map": ready,
	}


static func _normalized_region(region_value, mode: String) -> Dictionary:
	var region: Dictionary = region_value if region_value is Dictionary else {}
	if str(region.get("type", "rect")) != "rect":
		return region
	return {
		"type": "rect",
		"x": int(region.get("x", 0)),
		"y": int(region.get("y", 0)),
		"z": int(region.get("z", 0)) if mode == "3d" else 0,
		"width": max(1, int(region.get("width", 12 if mode == "3d" else 40))),
		"height": max(1, int(region.get("height", 12 if mode == "3d" else 30))),
		"depth": max(1, int(region.get("depth", 1))),
	}


static func _layout_for_mode(intent: Dictionary, region: Dictionary) -> Dictionary:
	if str(intent.get("mode", "2d")) == "3d":
		return _layout_3d(intent, region)
	return _layout_2d(intent, region)


static func _layout_2d(intent: Dictionary, region: Dictionary) -> Dictionary:
	var width := int(region.get("width", 40))
	var height := int(region.get("height", 30))
	var x := int(region.get("x", 0))
	var y := int(region.get("y", 0))
	var center_x := x + width / 2
	var center_y := y + height / 2
	var water_height := max(1, height / 5)
	var road_y := center_y
	var building_w := max(3, width / 5)
	var building_h := max(3, height / 4)
	return {
		"zones": [
			{"name": "land_area", "semantic_layer": "ground", "rect": region},
			{"name": "water_area", "semantic_layer": "water", "rect": {"type": "rect", "x": x, "y": y + height - water_height, "width": width, "height": water_height}},
			{"name": "main_road", "semantic_layer": "road", "from": {"x": x, "y": road_y}, "to": {"x": x + width - 1, "y": road_y}},
			{"name": "building_area", "semantic_layer": "building", "rect": {"type": "rect", "x": center_x - building_w / 2, "y": y + 2, "width": building_w, "height": building_h}},
			{"name": "obstacle_area", "semantic_layer": "obstacle", "rect": {"type": "rect", "x": x + 1, "y": y + 1, "width": max(1, width - 2), "height": max(1, height - water_height - 2)}, "density": "low"},
			{"name": "decor_noise", "semantic_layer": "decor", "rect": region, "density": intent.get("style", {}).get("density", "medium")},
			{"name": "object_anchors", "semantic_layer": "object", "points": [{"x": center_x, "y": road_y - 2}, {"x": center_x + 3, "y": road_y + 2}]},
		],
		"anchors": {
			"center": {"x": center_x, "y": center_y},
			"entry": {"x": x, "y": center_y},
			"exit": {"x": x + width - 1, "y": center_y},
		},
	}


static func _layout_3d(intent: Dictionary, region: Dictionary) -> Dictionary:
	var width := int(region.get("width", 12))
	var height := int(region.get("height", 12))
	var x := int(region.get("x", 0))
	var y := int(region.get("y", 0))
	var z := int(region.get("z", 0))
	var center_x := x + width / 2
	var center_y := y + height / 2
	var inner := {"type": "rect", "x": x + 1, "y": y + 1, "z": z, "width": max(1, width - 2), "height": max(1, height - 2), "depth": int(region.get("depth", 1))}
	return {
		"zones": [
			{"name": "floor", "semantic_layer": "floor", "rect": region},
			{"name": "walls", "semantic_layer": "wall", "perimeter": region},
			{"name": "door_south", "semantic_layer": "door", "coord": {"x": center_x, "y": y, "z": z}},
			{"name": "door_north", "semantic_layer": "door", "coord": {"x": center_x, "y": y + height - 1, "z": z}},
			{"name": "obstacles", "semantic_layer": "obstacle", "rect": inner, "density": "low"},
			{"name": "props", "semantic_layer": "props", "rect": inner, "density": intent.get("style", {}).get("density", "medium")},
			{"name": "interactables", "semantic_layer": "interact", "points": [{"x": center_x, "y": center_y, "z": z}]},
			{"name": "lights", "semantic_layer": "lights", "points": [{"x": x + 1, "y": y + 1, "z": z}, {"x": x + width - 2, "y": y + height - 2, "z": z}]},
		],
		"anchors": {
			"center": {"x": center_x, "y": center_y, "z": z},
			"entry": {"x": center_x, "y": y, "z": z},
			"exit": {"x": center_x, "y": y + height - 1, "z": z},
		},
	}


static func _operation_drafts(intent: Dictionary, region: Dictionary, missing_resources: Array) -> Array:
	if not missing_resources.is_empty():
		return []
	if str(intent.get("mode", "2d")) == "3d":
		return [
			{"action": "fill", "semantic_layer": "floor", "resource": "floor", "x": region.get("x", 0), "y": region.get("y", 0), "z": region.get("z", 0), "width": region.get("width", 12), "height": region.get("height", 12), "depth": 1},
			{"action": "fill", "semantic_layer": "wall", "resource": "wall", "note": "Apply as perimeter batches, leaving door cells empty."},
			{"tool": "place_map_objects", "semantic_layer": "props", "note": "Instantiate PackedScene props under PropsRoot after floor/wall validation."},
		]
	return [
		{"action": "fill", "semantic_layer": "ground", "resource": "ground", "x": region.get("x", 0), "y": region.get("y", 0), "width": region.get("width", 40), "height": region.get("height", 30)},
		{"tool": "paint_terrain_connect", "semantic_layer": "water", "resource": "river", "note": "Use terrain_set/terrain when the registry provides terrain fields."},
		{"tool": "paint_terrain_connect", "semantic_layer": "road", "resource": "road", "note": "Use terrain connect for smooth road edges when terrain fields exist."},
		{"tool": "place_map_objects", "semantic_layer": "object", "note": "Instantiate PackedScene objects under ObjectLayer after overlap validation."},
	]


static func _validation_plan(intent: Dictionary, region: Dictionary) -> Dictionary:
	var layout := _layout_for_mode(intent, region)
	var anchors: Dictionary = layout.get("anchors", {})
	return {
		"tool": "validate_map_region",
		"target": "planned_target_path",
		"region": region,
		"start": anchors.get("entry", {}),
		"goal": anchors.get("exit", {}),
		"entrances": [anchors.get("entry", {})],
		"exits": [anchors.get("exit", {})],
		"waypoints": _path_constraints(intent, region).get("waypoints", []),
		"walkable_is_filled": false if str(intent.get("mode", "2d")) == "3d" else true,
		"path_algorithm": "astar",
		"check_overlaps": true,
		"check_blocked_objects": true,
	}


static func _path_constraints(intent: Dictionary, region: Dictionary) -> Dictionary:
	var layout := _layout_for_mode(intent, region)
	var anchors: Dictionary = layout.get("anchors", {})
	var waypoints: Array = []
	for constraint in intent.get("constraints", []):
		if str(constraint) == "must_pass_center":
			waypoints.append(anchors.get("center", {}))
	return {
		"entrances": [anchors.get("entry", {})],
		"exits": [anchors.get("exit", {})],
		"waypoints": waypoints,
	}


static func _plan_steps(intent: Dictionary, region: Dictionary, missing_resources: Array, fallback_resources: Dictionary) -> Array:
	var steps: Array = [
		{"tool": "describe_map_context", "why": "Confirm target maps, registry, and index status."},
		{"tool": "ensure_standard_map_layers", "why": "Create/reuse standard 2D/3D map structure when missing."},
	]
	if not missing_resources.is_empty():
		steps.append({"tool": "write_resource_registry", "why": "Missing semantic resources should be mapped before edit_map when no fallback is acceptable.", "missing": missing_resources, "fallback": fallback_resources})
		if not _all_missing_have_fallback(missing_resources, fallback_resources):
			return steps
	steps.append({"tool": "describe_map_region", "why": "Read real boundary/cell data before editing."})
	steps.append({"tool": "edit_map", "why": "Apply planned batches through preview and Undo."})
	steps.append({"tool": "paint_terrain_connect", "why": "Use TileSet terrain rules for water/road smoothing when terrain fields exist."})
	steps.append({"tool": "place_map_objects", "why": "Instantiate PackedScene resources on ObjectLayer/PropsRoot when the plan needs scene objects."})
	steps.append({"tool": "validate_map_region", "why": "Check connectivity/occupancy after edits."})
	return steps


static func _performance_plan(intent: Dictionary, region: Dictionary) -> Dictionary:
	var width := int(region.get("width", 1))
	var height := int(region.get("height", 1))
	var depth := int(region.get("depth", 1))
	var cells := width * height * depth
	var density := str(intent.get("style", {}).get("density", "medium"))
	var estimated_objects := max(1, cells / (20 if density == "high" else 40 if density == "medium" else 80))
	return {
		"estimated_cells": cells,
		"estimated_object_nodes": estimated_objects,
		"density": density,
		"warnings": ["large region; split edit_map batches"] if cells > 400 else [],
	}


static func _resource_keys_for_intent(intent: Dictionary) -> Array:
	var keys: Array = []
	for item in intent.get("objects", []):
		if not (item is Dictionary):
			continue
		var name := str(item.get("name", ""))
		if name in ["river", "water"]:
			keys.append("river")
		elif name in ["path", "road"]:
			keys.append("road")
		elif name in ["wall"]:
			keys.append("wall")
		elif name in ["door"]:
			keys.append("door")
		elif name in ["torch"]:
			keys.append("torch")
		elif name in ["chest"]:
			keys.append("chest")
		elif name in ["tree"]:
			keys.append("tree")
		elif name in ["house"]:
			keys.append("house")
		elif name in ["campfire"]:
			keys.append("campfire")
	if str(intent.get("mode", "2d")) == "3d":
		_add_unique(keys, "floor")
		_add_unique(keys, "wall")
	else:
		_add_unique(keys, "ground")
	return keys


static func _missing_resources(keys: Array, context: Dictionary) -> Array:
	var registry = context.get("resource_registry", {}).get("data", {}) if context.get("resource_registry", {}) is Dictionary else {}
	if not (registry is Dictionary):
		registry = {}
	var missing: Array = []
	for key in keys:
		if not (registry as Dictionary).has(str(key)):
			missing.append(str(key))
	return missing


static func _fallback_resources(missing_keys: Array, context: Dictionary) -> Dictionary:
	var registry = context.get("resource_registry", {}).get("data", {}) if context.get("resource_registry", {}) is Dictionary else {}
	if not (registry is Dictionary):
		return {}
	var fallback := {}
	for missing in missing_keys:
		var candidates: Array = []
		for key in (registry as Dictionary).keys():
			var entry = (registry as Dictionary)[key]
			if not (entry is Dictionary):
				continue
			var semantic_layer := str((entry as Dictionary).get("semantic_layer", (entry as Dictionary).get("target", "")))
			var tags = (entry as Dictionary).get("tags", [])
			if semantic_layer == str(missing):
				candidates.append(key)
			elif tags is Array and ((tags as Array).has(str(missing)) or (tags as Array).has(_fallback_tag_for(str(missing)))):
				candidates.append(key)
		if not candidates.is_empty():
			fallback[str(missing)] = candidates
	return fallback


static func _fallback_tag_for(resource_key: String) -> String:
	match resource_key:
		"river":
			return "water"
		"road":
			return "path"
		"house":
			return "building"
		_:
			return resource_key


static func _all_missing_have_fallback(missing_keys: Array, fallback_resources: Dictionary) -> bool:
	for key in missing_keys:
		if not fallback_resources.has(str(key)):
			return false
	return true


static func _add_unique(values: Array, value: String) -> void:
	if not values.has(value):
		values.append(value)
