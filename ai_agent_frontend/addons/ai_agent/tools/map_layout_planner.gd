@tool
extends RefCounted

const MapLayerScaffold = preload("res://addons/ai_agent/tools/map_layer_scaffold.gd")


static func plan(intent: Dictionary, context: Dictionary) -> Dictionary:
	var mode := str(intent.get("mode", "2d"))
	var region := _normalized_region(intent.get("region", {}), mode)
	var resource_keys := _resource_keys_for_intent(intent)
	var missing_resources := _missing_resources(resource_keys, context)
	var steps := _plan_steps(intent, region, missing_resources)
	return {
		"mode": mode,
		"task": str(intent.get("task", "generate")),
		"theme": str(intent.get("theme", "generic")),
		"region": region,
		"required_resources": resource_keys,
		"missing_resources": missing_resources,
		"standard_layers": MapLayerScaffold.STANDARD_3D_LAYERS if mode == "3d" else MapLayerScaffold.STANDARD_2D_LAYERS,
		"layout": _layout_for_mode(intent, region),
		"steps": steps,
		"operation_drafts": _operation_drafts(intent, region, missing_resources),
		"validation": _validation_plan(intent, region),
		"ready_for_edit_map": missing_resources.is_empty(),
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
	return {
		"zones": [
			{"name": "ground_base", "semantic_layer": "ground", "rect": region},
			{"name": "main_path", "semantic_layer": "road", "from": {"x": x, "y": center_y}, "to": {"x": x + width - 1, "y": center_y}},
			{"name": "decor_noise", "semantic_layer": "decor", "rect": region, "density": intent.get("style", {}).get("density", "medium")},
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
	return {
		"zones": [
			{"name": "floor", "semantic_layer": "floor", "rect": region},
			{"name": "walls", "semantic_layer": "wall", "perimeter": region},
			{"name": "door_south", "semantic_layer": "door", "coord": {"x": center_x, "y": y, "z": z}},
			{"name": "door_north", "semantic_layer": "door", "coord": {"x": center_x, "y": y + height - 1, "z": z}},
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
		]
	return [
		{"action": "fill", "semantic_layer": "ground", "resource": "ground", "x": region.get("x", 0), "y": region.get("y", 0), "width": region.get("width", 40), "height": region.get("height", 30)},
		{"action": "fill", "semantic_layer": "road", "resource": "road", "note": "Apply as 1-cell high/width path batches after ground."},
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
		"walkable_is_filled": false if str(intent.get("mode", "2d")) == "3d" else true,
	}


static func _plan_steps(intent: Dictionary, region: Dictionary, missing_resources: Array) -> Array:
	var steps: Array = [
		{"tool": "describe_map_context", "why": "Confirm target maps, registry, and index status."},
		{"tool": "ensure_standard_map_layers", "why": "Create/reuse standard 2D/3D map structure when missing."},
	]
	if not missing_resources.is_empty():
		steps.append({"tool": "write_resource_registry", "why": "Missing semantic resources must be mapped before edit_map.", "missing": missing_resources})
		return steps
	steps.append({"tool": "describe_map_region", "why": "Read real boundary/cell data before editing."})
	steps.append({"tool": "edit_map", "why": "Apply planned batches through preview and Undo."})
	steps.append({"tool": "validate_map_region", "why": "Check connectivity/occupancy after edits."})
	return steps


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


static func _add_unique(values: Array, value: String) -> void:
	if not values.has(value):
		values.append(value)
