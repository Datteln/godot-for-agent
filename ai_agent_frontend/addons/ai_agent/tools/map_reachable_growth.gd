@tool
extends RefCounted

const MapPlatformPlanValidator = preload("res://addons/ai_agent/tools/map_platform_plan_validator.gd")


static func plan_growth(input: Dictionary, context: Dictionary = {}) -> Dictionary:
	var profile := _profile_from_input(input)
	if profile == "platformer":
		var platform_plan := MapPlatformPlanValidator.validate_platform_level_plan(input, context)
		return _wrap_platform_plan(platform_plan, input, context)
	var dimension := 3 if profile == "3d_grid" else 2
	var region := _region_from_input(input, dimension)
	var frontier := _frontier_from_input(input, region, profile, dimension)
	var motifs := _motifs_for_profile(profile, frontier, region, input)
	var accepted := _accept_incremental_motifs(motifs, frontier, profile, input)
	var batches := _emit_batches_for_profile(profile, accepted, input)
	var validation := _validation_for_profile(profile, frontier, accepted, region, input)
	var repair := _repair_for_profile(profile)
	return {
		"ok": true,
		"algorithm": "reachable_map_growth",
		"profile": profile,
		"dimension": dimension,
		"region": region,
		"context_summary": _context_summary(context),
		"frontier": frontier,
		"candidates": motifs,
		"accepted_motifs": accepted,
		"edit_map_batches": batches,
		"validation": validation,
		"repair_strategies": repair,
		"execution_order": [
			"confirm the frontier against real map data",
			"apply accepted_motifs one by one through previewed edit_map/place_map_objects",
			"run validation after each growth step when the profile has gameplay risk",
			"apply the listed repair strategy only around the first failed motif",
		],
	}


static func _wrap_platform_plan(platform_plan: Dictionary, input: Dictionary, context: Dictionary) -> Dictionary:
	var accepted: Array = []
	for segment in platform_plan.get("critical_route", []):
		accepted.append(segment)
	return {
		"ok": bool(platform_plan.get("ok", true)),
		"algorithm": "reachable_map_growth",
		"profile": "platformer",
		"dimension": 2,
		"region": platform_plan.get("region", {}),
		"context_summary": _context_summary(context),
		"frontier": {
			"type": "rightmost_reachable_foothold",
			"cell": platform_plan.get("entry_anchor", input.get("frontier", {})),
		},
		"candidates": platform_plan.get("critical_route", []),
		"accepted_motifs": accepted,
		"edit_map_batches": platform_plan.get("edit_map_batches", []),
		"validation": platform_plan.get("validation", {}),
		"repair_strategies": platform_plan.get("repair_plan", []),
		"profile_plan": platform_plan,
	}


static func _motifs_for_profile(profile: String, frontier: Dictionary, region: Dictionary, input: Dictionary) -> Array:
	match profile:
		"topdown":
			return _topdown_motifs(frontier, region, input)
		"dungeon":
			return _dungeon_motifs(frontier, region, input)
		"3d_grid":
			return _grid_3d_motifs(frontier, region, input)
		_:
			return _topdown_motifs(frontier, region, input)


static func _topdown_motifs(frontier: Dictionary, region: Dictionary, input: Dictionary) -> Array:
	var start := _coord_from_dict(frontier.get("cell", {}), 2)
	var length := maxi(4, int(input.get("step_length", 8)))
	var road_y := start.y
	var max_x := int(region["x"]) + int(region["width"]) - 1
	var motifs: Array = []
	var cursor_x := maxi(int(region["x"]), start.x + 1)
	var index := 0
	while cursor_x <= max_x and index < int(input.get("max_steps", 8)):
		var width := mini(length, max_x - cursor_x + 1)
		motifs.append({
			"index": index,
			"type": "road_segment",
			"rect": {"x": cursor_x, "y": road_y, "width": width, "height": 1},
			"from": {"x": cursor_x - 1, "y": road_y},
			"to": {"x": cursor_x + width - 1, "y": road_y},
		})
		if index % 3 == 1:
			motifs.append({
				"index": index,
				"type": "plaza",
				"rect": {"x": cursor_x + maxi(0, width / 2 - 1), "y": road_y - 1, "width": mini(3, width), "height": 3},
			})
		cursor_x += width
		index += 1
	return motifs


static func _dungeon_motifs(frontier: Dictionary, region: Dictionary, input: Dictionary) -> Array:
	var start := _coord_from_dict(frontier.get("cell", {}), 2)
	var room_w := maxi(4, int(input.get("room_width", 8)))
	var room_h := maxi(4, int(input.get("room_height", 6)))
	var corridor := maxi(2, int(input.get("corridor_length", 4)))
	var motifs: Array = []
	var cursor_x := maxi(int(region["x"]), start.x + 1)
	var center_y := clampi(start.y, int(region["y"]) + room_h / 2, int(region["y"]) + int(region["height"]) - room_h / 2)
	for index in range(maxi(1, int(input.get("max_steps", 4)))):
		if cursor_x > int(region["x"]) + int(region["width"]) - 1:
			break
		motifs.append({
			"index": index,
			"type": "corridor",
			"rect": {"x": cursor_x, "y": center_y, "width": corridor, "height": 1},
		})
		cursor_x += corridor
		motifs.append({
			"index": index,
			"type": "room",
			"rect": {"x": cursor_x, "y": center_y - room_h / 2, "width": room_w, "height": room_h},
			"door": {"x": cursor_x, "y": center_y},
		})
		cursor_x += room_w
	return motifs


static func _grid_3d_motifs(frontier: Dictionary, region: Dictionary, input: Dictionary) -> Array:
	var start := _coord_from_dict(frontier.get("cell", {}), 3)
	var step := maxi(3, int(input.get("step_length", 6)))
	var motifs: Array = []
	var cursor_x := maxi(int(region["x"]), start.x + 1)
	var max_x := int(region["x"]) + int(region["width"]) - 1
	for index in range(maxi(1, int(input.get("max_steps", 6)))):
		if cursor_x > max_x:
			break
		var width := mini(step, max_x - cursor_x + 1)
		motifs.append({
			"index": index,
			"type": "floor_strip",
			"rect": {"x": cursor_x, "y": start.y, "z": start.z, "width": width, "height": 1, "depth": maxi(1, int(input.get("path_depth", 3)))},
			"from": {"x": cursor_x - 1, "y": start.y, "z": start.z},
			"to": {"x": cursor_x + width - 1, "y": start.y, "z": start.z},
		})
		cursor_x += width
	return motifs


static func _accept_incremental_motifs(motifs: Array, frontier: Dictionary, profile: String, input: Dictionary) -> Array:
	var accepted: Array = []
	var current := frontier.duplicate(true)
	var max_gap := maxi(1, int(input.get("max_gap", input.get("max_horizontal_gap", 6))))
	for motif_value in motifs:
		if not (motif_value is Dictionary):
			continue
		var motif: Dictionary = motif_value
		var from_cell := _coord_from_dict(current.get("cell", {}), 3 if profile == "3d_grid" else 2)
		var next_start := _motif_start(motif, 3 if profile == "3d_grid" else 2)
		var distance := absi(next_start.x - from_cell.x) + absi(next_start.y - from_cell.y) + absi(next_start.z - from_cell.z)
		if distance > max_gap and profile != "dungeon":
			accepted.append(_connector_motif(from_cell, next_start, profile, max_gap))
		motif["accepted"] = true
		accepted.append(motif)
		current["cell"] = _motif_end(motif, 3 if profile == "3d_grid" else 2)
	return accepted


static func _emit_batches_for_profile(profile: String, motifs: Array, input: Dictionary) -> Array:
	var batches: Array = []
	for motif_value in motifs:
		if not (motif_value is Dictionary):
			continue
		var motif: Dictionary = motif_value
		var rect: Dictionary = motif.get("rect", {})
		if rect.is_empty():
			continue
		match profile:
			"3d_grid":
				batches.append(_edit_batch(rect, "floor", input, ["reachable_growth", str(motif.get("type", ""))]))
			"dungeon":
				batches.append(_edit_batch(rect, "floor", input, ["reachable_growth", "dungeon", str(motif.get("type", ""))]))
			_:
				batches.append(_edit_batch(rect, "road", input, ["reachable_growth", str(motif.get("type", ""))]))
	return batches


static func _edit_batch(rect: Dictionary, semantic_layer: String, input: Dictionary, tags: Array) -> Dictionary:
	var operation := {
		"action": "fill",
		"x": int(rect.get("x", 0)),
		"y": int(rect.get("y", 0)),
		"z": int(rect.get("z", 0)),
		"width": int(rect.get("width", 1)),
		"height": int(rect.get("height", 1)),
		"depth": int(rect.get("depth", 1)),
		"resource": str(input.get(semantic_layer + "_resource", semantic_layer)),
		"fallback_resource": str(input.get("fallback_" + semantic_layer + "_resource", "")),
		"semantic_layer": semantic_layer,
		"tags": tags,
	}
	return {"tool": "edit_map", "operations": [operation], "expected_cells": int(rect.get("width", 1)) * int(rect.get("height", 1)) * int(rect.get("depth", 1)), "motif": tags.back()}


static func _validation_for_profile(profile: String, frontier: Dictionary, accepted: Array, region: Dictionary, input: Dictionary) -> Dictionary:
	if accepted.is_empty():
		return {}
	var dimension := 3 if profile == "3d_grid" else 2
	var start = frontier.get("cell", {"x": region.get("x", 0), "y": region.get("y", 0)})
	var goal = _motif_end(accepted.back(), dimension)
	# topdown/dungeon/3d_grid 在这里生成的都是连续填实的地板/走廊（每个 motif 内部不留缝），
	# 角色走在这些"地板即可走区域"上不涉及跳跃/失重，统一用 grid 即可；
	# "free" 是给飞行/游泳/幽灵类无重力移动用的（见 map-agent.md），3D 室内地板导航不该套这个，
	# 否则 free 允许跨越 max_step 格的洞而不被判失败，反而漏掉地板缺口这种真实问题。
	var movement := "grid"
	return {
		"validate_map_region": {
			"x": region.get("x", 0),
			"y": region.get("y", 0),
			"z": region.get("z", 0) if dimension == 3 else null,
			"width": region.get("width", 1),
			"height": region.get("height", 1),
			"depth": region.get("depth", 1) if dimension == 3 else null,
			"start": start,
			"goal": {"x": goal.x, "y": goal.y, "z": goal.z} if dimension == 3 else {"x": goal.x, "y": goal.y},
			"movement_model": movement,
			"walkable_is_filled": true,
			"path_algorithm": "astar",
			"check_overlaps": true,
			"check_blocked_objects": true,
		}
	}


static func _repair_for_profile(profile: String) -> Array:
	match profile:
		"platformer":
			return [{"name": "stepping_stones"}, {"name": "landing_widen"}, {"name": "stair_bridge"}]
		"dungeon":
			return [{"name": "carve_corridor"}, {"name": "add_door"}, {"name": "room_bridge"}]
		"3d_grid":
			return [{"name": "fill_floor_gap"}, {"name": "open_doorway"}, {"name": "move_blocking_prop"}]
		_:
			return [{"name": "carve_road"}, {"name": "remove_blocker"}, {"name": "widen_chokepoint"}]


static func _connector_motif(from_cell: Vector3i, to_cell: Vector3i, profile: String, max_gap: int) -> Dictionary:
	var width := maxi(1, mini(max_gap, absi(to_cell.x - from_cell.x)))
	return {
		"type": "connector",
		"accepted": true,
		"rect": {"x": mini(from_cell.x, to_cell.x) + 1, "y": from_cell.y, "z": from_cell.z, "width": width, "height": 1, "depth": 1},
		"from": {"x": from_cell.x, "y": from_cell.y, "z": from_cell.z},
		"to": {"x": to_cell.x, "y": to_cell.y, "z": to_cell.z},
		"profile": profile,
	}


static func _frontier_from_input(input: Dictionary, region: Dictionary, profile: String, dimension: int) -> Dictionary:
	var raw = input.get("frontier", input.get("entry_anchor", {}))
	if raw is Dictionary and not (raw as Dictionary).is_empty():
		return {"type": str(input.get("frontier_type", "provided")), "cell": _payload(_coord_from_dict(raw, dimension), dimension)}
	var cell := Vector3i(int(region["x"]) - 1, int(region["y"]) + int(region["height"]) / 2, int(region.get("z", 0)))
	if profile == "3d_grid":
		cell = Vector3i(int(region["x"]) - 1, int(region["y"]), int(region.get("z", 0)))
	return {"type": "left_edge_default", "cell": _payload(cell, dimension)}


static func _motif_start(motif: Dictionary, dimension: int) -> Vector3i:
	if motif.has("from"):
		return _coord_from_dict(motif["from"], dimension)
	if motif.has("rect"):
		var rect: Dictionary = motif["rect"]
		return Vector3i(int(rect.get("x", 0)), int(rect.get("y", 0)), int(rect.get("z", 0)) if dimension == 3 else 0)
	return Vector3i.ZERO


static func _motif_end(motif: Dictionary, dimension: int) -> Vector3i:
	if motif.has("to"):
		return _coord_from_dict(motif["to"], dimension)
	if motif.has("rect"):
		var rect: Dictionary = motif["rect"]
		return Vector3i(
			int(rect.get("x", 0)) + int(rect.get("width", 1)) - 1,
			int(rect.get("y", 0)) + int(rect.get("height", 1)) - 1,
			int(rect.get("z", 0)) + int(rect.get("depth", 1)) - 1 if dimension == 3 else 0
		)
	return Vector3i.ZERO


static func _region_from_input(input: Dictionary, dimension: int) -> Dictionary:
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var z := int(input.get("z", 0)) if dimension == 3 else 0
	var width := maxi(1, int(input.get("width", 40 if dimension == 2 else 12)))
	var height := maxi(1, int(input.get("height", 20 if dimension == 2 else 8)))
	var depth := maxi(1, int(input.get("depth", 1))) if dimension == 3 else 1
	return {"x": x, "y": y, "z": z, "width": width, "height": height, "depth": depth}


static func _coord_from_dict(value, dimension: int) -> Vector3i:
	var data: Dictionary = value if value is Dictionary else {}
	return Vector3i(int(data.get("x", 0)), int(data.get("y", 0)), int(data.get("z", 0)) if dimension == 3 else 0)


static func _payload(coords: Vector3i, dimension: int) -> Dictionary:
	return {"x": coords.x, "y": coords.y, "z": coords.z} if dimension == 3 else {"x": coords.x, "y": coords.y}


static func _profile_from_input(input: Dictionary) -> String:
	var raw := str(input.get("profile", "topdown")).to_lower()
	if raw in ["platform", "platformer", "side_scroller", "side-scroller"]:
		return "platformer"
	if raw in ["dungeon", "room_graph"]:
		return "dungeon"
	if raw in ["3d", "3d_grid", "grid3d"]:
		return "3d_grid"
	return "topdown"


static func _context_summary(context: Dictionary) -> Dictionary:
	if context.is_empty():
		return {}
	return {
		"maps": (context.get("maps", []) as Array).size() if context.get("maps", []) is Array else 0,
		"registry_exists": bool(context.get("resource_registry", {}).get("exists", false)) if context.get("resource_registry", {}) is Dictionary else false,
	}
