@tool
extends RefCounted

const MapValidator = preload("res://addons/ai_agent/tools/map_validator.gd")


static func plan_platform_level(input: Dictionary, context: Dictionary = {}) -> Dictionary:
	var region := _region_from_input(input)
	var ability := _ability_from_input(input)
	var ground_y := int(input.get("ground_y", int(region["y"]) + int(region["height"]) / 2))
	var seed := int(input.get("seed", 0))
	var route := _build_critical_route(region, ground_y, ability, seed)
	var platforms: Array = route.get("platforms", [])
	var jump_graph := _build_jump_graph(platforms, ability)
	var edit_batches := _emit_platform_tile_batches(platforms, input)
	var coin_arcs := _emit_coin_arcs(route.get("segments", []), ability)
	var enemy_slots := _emit_enemy_slots(platforms)
	var validation := _validation_plan(region, platforms, ability)
	var score := _score_level(route.get("segments", []), jump_graph, coin_arcs, enemy_slots)
	return {
		"ok": true,
		"algorithm": "platform_level_composer",
		"region": region,
		"ability": ability,
		"context_summary": _context_summary(context),
		"critical_route": route.get("segments", []),
		"platforms": platforms,
		"jump_graph": jump_graph,
		"edit_map_batches": edit_batches,
		"coin_arcs": coin_arcs,
		"enemy_slots": enemy_slots,
		"validation": validation,
		"score": score,
		"execution_order": [
			"describe_map_region at the current boundary",
			"apply edit_map_batches in small previewed chunks",
			"place coins along coin_arcs with edit_map/place_map_objects when resources exist",
			"place enemies only at enemy_slots with enough landing width",
			"validate_map_region using validation.validate_map_region",
			"repair_map_region with the same leap ability parameters if validation fails",
		],
	}


static func _build_critical_route(region: Dictionary, ground_y: int, ability: Dictionary, seed: int) -> Dictionary:
	var x := int(region["x"])
	var max_x := x + int(region["width"]) - 1
	var current_x := x
	var current_y := ground_y
	var segments: Array = []
	var platforms: Array = []
	var index := 0
	while current_x <= max_x:
		var progress := float(current_x - int(region["x"])) / float(max(1, int(region["width"])))
		var kind := _motif_for_progress(progress, index, seed)
		var built := _build_motif(kind, current_x, current_y, max_x, ability, index)
		var segment: Dictionary = built.get("segment", {})
		if segment.is_empty():
			break
		segments.append(segment)
		for platform in built.get("platforms", []):
			platforms.append(platform)
		var next_anchor: Dictionary = built.get("next_anchor", {})
		current_x = int(next_anchor.get("x", current_x + 1))
		current_y = int(next_anchor.get("y", current_y))
		index += 1
		if index > 64:
			break
	return {"segments": segments, "platforms": _merge_platforms(platforms)}


static func _build_motif(kind: String, x: int, y: int, max_x: int, ability: Dictionary, index: int) -> Dictionary:
	var max_gap := int(ability["max_horizontal_gap"])
	var max_rise := int(ability["max_rise"])
	var min_landing := int(ability["min_landing_width"])
	var platforms: Array = []
	var segment := {"index": index, "type": kind, "start": {"x": x, "y": y - 1}}
	match kind:
		"safe_intro", "rest":
			var width := min(max_x - x + 1, max(6, min_landing + 4))
			platforms.append(_platform(x, y, width, kind))
			segment["end"] = {"x": x + width - 1, "y": y - 1}
			segment["difficulty"] = 1
			return {"segment": segment, "platforms": platforms, "next_anchor": {"x": x + width, "y": y}}
		"gap_jump":
			var left_width := min(max_x - x + 1, max(min_landing, 4))
			var gap := clampi(max_gap - 1, 2, max_gap)
			var right_x := x + left_width + gap
			var right_width := min(max_x - right_x + 1, max(min_landing, 4))
			if right_width <= 0:
				platforms.append(_platform(x, y, max_x - x + 1, "finish"))
				segment["type"] = "finish"
				segment["end"] = {"x": max_x, "y": y - 1}
				segment["difficulty"] = 1
				return {"segment": segment, "platforms": platforms, "next_anchor": {"x": max_x + 1, "y": y}}
			platforms.append(_platform(x, y, left_width, "takeoff"))
			platforms.append(_platform(right_x, y, right_width, "landing"))
			segment["gap"] = gap
			segment["takeoff"] = {"x": x + left_width - 1, "y": y - 1}
			segment["landing"] = {"x": right_x, "y": y - 1}
			segment["end"] = {"x": right_x + right_width - 1, "y": y - 1}
			segment["difficulty"] = 3
			return {"segment": segment, "platforms": platforms, "next_anchor": {"x": right_x + right_width, "y": y}}
		"stair_up":
			var step_w := max(min_landing, 3)
			var rise := max(1, min(max_rise, 2))
			for i in range(3):
				var px := x + i * step_w
				if px > max_x:
					break
				platforms.append(_platform(px, y - i * rise, min(step_w, max_x - px + 1), "stair"))
			var end_x := min(max_x + 1, x + step_w * 3)
			segment["rise"] = rise
			segment["end"] = {"x": end_x - 1, "y": y - 2 * rise - 1}
			segment["difficulty"] = 2
			return {"segment": segment, "platforms": platforms, "next_anchor": {"x": end_x, "y": y - 2 * rise}}
		"stair_down":
			var step_width := max(min_landing, 3)
			for i in range(3):
				var sx := x + i * step_width
				if sx > max_x:
					break
				platforms.append(_platform(sx, y + i, min(step_width, max_x - sx + 1), "stair"))
			var down_end_x := min(max_x + 1, x + step_width * 3)
			segment["end"] = {"x": down_end_x - 1, "y": y + 2 - 1}
			segment["difficulty"] = 2
			return {"segment": segment, "platforms": platforms, "next_anchor": {"x": down_end_x, "y": y + 2}}
		"hazard_crossing":
			var bridge_width := min(max_x - x + 1, max(10, min_landing * 3))
			platforms.append(_platform(x, y, min_landing, "hazard_entry"))
			if x + bridge_width - min_landing <= max_x:
				platforms.append(_platform(x + bridge_width - min_landing, y, min_landing, "hazard_exit"))
			segment["hazard_rect"] = {"x": x + min_landing, "y": y + 1, "width": max(1, bridge_width - min_landing * 2), "height": 2}
			segment["end"] = {"x": min(max_x, x + bridge_width - 1), "y": y - 1}
			segment["difficulty"] = 4
			return {"segment": segment, "platforms": platforms, "next_anchor": {"x": x + bridge_width, "y": y}}
		_:
			var fallback_width := min(max_x - x + 1, max(5, min_landing))
			platforms.append(_platform(x, y, fallback_width, "flat"))
			segment["end"] = {"x": x + fallback_width - 1, "y": y - 1}
			segment["difficulty"] = 1
			return {"segment": segment, "platforms": platforms, "next_anchor": {"x": x + fallback_width, "y": y}}


static func _motif_for_progress(progress: float, index: int, seed: int) -> String:
	if progress < 0.12:
		return "safe_intro"
	if progress > 0.88:
		return "rest"
	var cycle := (index + seed) % 6
	match cycle:
		0:
			return "gap_jump"
		1:
			return "stair_up"
		2:
			return "rest"
		3:
			return "hazard_crossing"
		4:
			return "stair_down"
		_:
			return "gap_jump"


static func _build_jump_graph(platforms: Array, ability: Dictionary) -> Dictionary:
	var edges: Array = []
	var unreachable: Array = []
	for i in range(platforms.size()):
		for j in range(i + 1, min(platforms.size(), i + 4)):
			var edge := _jump_edge(platforms[i], platforms[j], ability)
			edges.append(edge)
			if not bool(edge.get("reachable", false)):
				unreachable.append(edge)
	return {"nodes": platforms.size(), "edges": edges, "unreachable_edges": unreachable, "passed": unreachable.is_empty()}


static func _jump_edge(from_platform: Dictionary, to_platform: Dictionary, ability: Dictionary) -> Dictionary:
	var from_x := int(from_platform["x"]) + int(from_platform["width"]) - 1
	var from_y := int(from_platform["y"]) - 1
	var to_x := int(to_platform["x"])
	var to_y := int(to_platform["y"]) - 1
	var horizontal := max(0, to_x - from_x)
	var vertical := from_y - to_y
	var reachable := horizontal <= int(ability["max_horizontal_gap"]) and vertical <= int(ability["max_rise"]) and -vertical <= int(ability["max_fall"])
	return {
		"from": from_platform.get("id", ""),
		"to": to_platform.get("id", ""),
		"from_cell": {"x": from_x, "y": from_y},
		"to_cell": {"x": to_x, "y": to_y},
		"horizontal_gap": horizontal,
		"vertical_delta": vertical,
		"reachable": reachable,
	}


static func _emit_platform_tile_batches(platforms: Array, input: Dictionary) -> Array:
	var batches: Array = []
	for platform in platforms:
		var remaining := int(platform["width"])
		var cursor := int(platform["x"])
		while remaining > 0:
			var batch_width := min(5, remaining)
			var op := {
				"action": "fill",
				"x": cursor,
				"y": int(platform["y"]),
				"width": batch_width,
				"height": int(input.get("platform_thickness", 1)),
				"resource": str(input.get("ground_resource", "ground")),
				"fallback_resource": str(input.get("fallback_ground_resource", "")),
				"semantic_layer": "ground",
				"tags": ["platform", str(platform.get("role", "platform"))],
			}
			batches.append({
				"tool": "edit_map",
				"operations": [op],
				"expected_cells": batch_width * int(input.get("platform_thickness", 1)),
				"platform_id": platform.get("id", ""),
				"note": "Apply after describe_map_region confirms this support row is safe to overwrite.",
			})
			cursor += batch_width
			remaining -= batch_width
	return batches


static func _emit_coin_arcs(segments: Array, ability: Dictionary) -> Array:
	var arcs: Array = []
	for segment in segments:
		if str(segment.get("type", "")) != "gap_jump":
			continue
		var takeoff := MapValidator.coord_from_input(segment.get("takeoff", {}), 2)
		var landing := MapValidator.coord_from_input(segment.get("landing", {}), 2)
		var cells: Array = []
		var steps := max(3, landing.x - takeoff.x)
		for i in range(steps + 1):
			var t := float(i) / float(max(1, steps))
			var x := int(round(lerpf(float(takeoff.x), float(landing.x), t)))
			var y := int(round(lerpf(float(takeoff.y), float(landing.y), t) - sin(t * PI) * max(1.0, float(ability["max_rise"]))))
			cells.append({"x": x, "y": y})
		arcs.append({"over_segment": segment.get("index", 0), "cells": cells, "resource": "coin", "semantic_layer": "reward"})
	return arcs


static func _emit_enemy_slots(platforms: Array) -> Array:
	var slots: Array = []
	for platform in platforms:
		var width := int(platform.get("width", 0))
		if width < 6:
			continue
		if str(platform.get("role", "")) in ["takeoff", "landing", "hazard_entry", "hazard_exit"]:
			continue
		slots.append({
			"x": int(platform["x"]) + width / 2,
			"y": int(platform["y"]) - 1,
			"platform_id": platform.get("id", ""),
			"min_patrol_width": width,
			"resource": "enemy",
		})
	return slots


static func _validation_plan(region: Dictionary, platforms: Array, ability: Dictionary) -> Dictionary:
	if platforms.is_empty():
		return {}
	var first: Dictionary = platforms.front()
	var last: Dictionary = platforms.back()
	return {
		"validate_map_region": {
			"x": region.get("x", 0),
			"y": region.get("y", 0),
			"width": region.get("width", 1),
			"height": region.get("height", 1),
			"start": {"x": int(first["x"]), "y": int(first["y"]) - 1},
			"goal": {"x": int(last["x"]) + int(last["width"]) - 1, "y": int(last["y"]) - 1},
			"movement_model": "leap",
			"max_horizontal_gap": ability["max_horizontal_gap"],
			"max_rise": ability["max_rise"],
			"max_fall": ability["max_fall"],
			"walkable_is_filled": false,
			"path_algorithm": "astar",
			"check_overlaps": true,
			"check_blocked_objects": true,
		}
	}


static func _score_level(segments: Array, jump_graph: Dictionary, coin_arcs: Array, enemy_slots: Array) -> Dictionary:
	var issues: Array = []
	if not bool(jump_graph.get("passed", true)):
		issues.append("one or more planned platform transitions exceed movement ability")
	if segments.size() < 3:
		issues.append("route has too few gameplay segments")
	var difficulty := 0
	for segment in segments:
		difficulty += int(segment.get("difficulty", 1))
	return {
		"passed": issues.is_empty(),
		"issues": issues,
		"segment_count": segments.size(),
		"estimated_difficulty": difficulty,
		"reward_arc_count": coin_arcs.size(),
		"enemy_slot_count": enemy_slots.size(),
	}


static func _merge_platforms(platforms: Array) -> Array:
	var result: Array = []
	for platform_value in platforms:
		if not (platform_value is Dictionary):
			continue
		var platform: Dictionary = platform_value
		if int(platform.get("width", 0)) <= 0:
			continue
		platform["id"] = "p%d" % result.size()
		result.append(platform)
	return result


static func _platform(x: int, y: int, width: int, role: String) -> Dictionary:
	return {"x": x, "y": y, "width": max(1, width), "height": 1, "role": role}


static func _ability_from_input(input: Dictionary) -> Dictionary:
	return {
		"max_horizontal_gap": max(2, int(input.get("max_horizontal_gap", 4))),
		"max_rise": max(0, int(input.get("max_rise", 2))),
		"max_fall": max(1, int(input.get("max_fall", 6))),
		"min_landing_width": max(2, int(input.get("min_landing_width", 3))),
	}


static func _region_from_input(input: Dictionary) -> Dictionary:
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var width := max(8, int(input.get("width", 40)))
	var height := max(8, int(input.get("height", 20)))
	return {"x": x, "y": y, "width": width, "height": height, "depth": 1}


static func _context_summary(context: Dictionary) -> Dictionary:
	if context.is_empty():
		return {}
	return {
		"maps": (context.get("maps", []) as Array).size() if context.get("maps", []) is Array else 0,
		"registry_exists": bool(context.get("resource_registry", {}).get("exists", false)) if context.get("resource_registry", {}) is Dictionary else false,
	}
