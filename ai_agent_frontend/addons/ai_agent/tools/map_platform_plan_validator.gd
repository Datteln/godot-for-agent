@tool
extends RefCounted

static func plan_platform_level(input: Dictionary, context: Dictionary = {}) -> Dictionary:
	var region := _region_from_input(input)
	var ability := _ability_from_input(input)
	var ability_used_defaults := _ability_defaulted_keys(input)
	var entry_anchor := _entry_anchor_from_input(input)
	var requires_entry_anchor := bool(input.get("connect_from_existing", true))
	var platforms := _platforms_from_input(input)
	var segments := _dictionary_array(input.get("segments", []))
	var coin_arcs := _dictionary_array(input.get("coin_arcs", []))
	var enemy_slots := _dictionary_array(input.get("enemy_slots", []))
	var plan_issues := _validate_explicit_plan(platforms, segments, region)
	var graph_platforms := platforms.duplicate(true)
	if requires_entry_anchor and not entry_anchor.is_empty():
		var existing_entry := _platform(
			int(entry_anchor.get("x", 0)),
			int(entry_anchor.get("y", 0)) + 1,
			1,
			"existing_entry"
		)
		existing_entry["id"] = "__existing_entry"
		existing_entry["existing"] = true
		graph_platforms.push_front(existing_entry)
	var jump_graph := _build_jump_graph(graph_platforms, ability)
	var edit_batches := _emit_platform_tile_batches(platforms, input)
	var design_limits := _design_limits_from_input(input, ability)
	var validation := _validation_plan(region, platforms, ability, design_limits, entry_anchor)
	var score := _score_level(segments, platforms, jump_graph, coin_arcs, enemy_slots, design_limits)
	var blocked_reason := ""
	var error_code := ""
	if not plan_issues.is_empty():
		blocked_reason = "invalid_explicit_plan"
		error_code = str((plan_issues.front() as Dictionary).get("error_code", "invalid_explicit_plan"))
	elif bool(input.get("entry_anchor_scan_failed", false)) or (requires_entry_anchor and entry_anchor.is_empty()):
		blocked_reason = "entry_anchor_not_found"
		error_code = blocked_reason
	elif not bool(jump_graph.get("passed", true)):
		blocked_reason = "jump_graph_failed"
		error_code = blocked_reason
	elif not bool(score.get("passed", true)):
		blocked_reason = "score_issues"
		var score_details: Array = score.get("issue_details", [])
		error_code = str((score_details.front() as Dictionary).get("error_code", blocked_reason)) if not score_details.is_empty() else blocked_reason
	if blocked_reason != "":
		edit_batches = []
	return {
		"ok": plan_issues.is_empty(),
		"algorithm": "explicit_platform_plan_validator",
		"plan_source": "llm_explicit",
		"region": region,
		"ability": ability,
		"ability_used_defaults": ability_used_defaults,
		"context_summary": _context_summary(context),
		"entry_anchor": entry_anchor,
		"critical_route": segments,
		"platforms": platforms,
		"jump_graph": jump_graph,
		"edit_map_batches": edit_batches,
		"blocked_reason": blocked_reason,
		"error_code": error_code,
		"issues": plan_issues,
		"repair_plan": _repair_plan(plan_issues, jump_graph, score),
		"coin_arcs": coin_arcs,
		"enemy_slots": enemy_slots,
		"validation": validation,
		"score": score,
		"design_limits": design_limits,
		"execution_order": [
			"LLM submits ordered platforms and route segments after reading the real map boundary",
			"revise the explicit platforms/segments when issues or repair_plan are returned",
			"apply edit_map_batches in small previewed chunks",
			"place only the explicitly planned coin_arcs and enemy_slots when resources exist",
			"validate_map_region using validation.validate_map_region",
			"return validation failures to the LLM planner instead of auto-generating replacement geometry",
		],
	}


static func _platforms_from_input(input: Dictionary) -> Array:
	var platforms: Array = []
	var raw_platforms = input.get("platforms", [])
	if not (raw_platforms is Array):
		return platforms
	for value in raw_platforms:
		if not (value is Dictionary):
			continue
		var platform := (value as Dictionary).duplicate(true)
		if str(platform.get("id", "")).is_empty():
			platform["id"] = "p%d" % platforms.size()
		platform["height"] = 1
		platforms.append(platform)
	return platforms


static func _dictionary_array(value) -> Array:
	var result: Array = []
	if not (value is Array):
		return result
	for item in value:
		if item is Dictionary:
			result.append((item as Dictionary).duplicate(true))
	return result


static func _validate_explicit_plan(platforms: Array, segments: Array, region: Dictionary) -> Array:
	var issues: Array = []
	if platforms.is_empty():
		issues.append({
			"error_code": "explicit_platform_plan_required",
			"path": "platforms",
			"actual": 0,
			"required": "ordered non-empty platform array",
			"action": "LLM must submit the concrete platform geometry; automatic generation is disabled.",
		})
	if segments.is_empty():
		issues.append({
			"error_code": "explicit_route_segments_required",
			"path": "segments",
			"actual": 0,
			"required": "ordered non-empty route segment array",
			"action": "LLM must describe the critical route between the submitted platforms.",
		})
	var min_x := int(region.get("x", 0))
	var max_x := min_x + int(region.get("width", 1)) - 1
	var min_y := int(region.get("y", 0))
	var max_y := min_y + int(region.get("height", 1)) - 1
	var ids := {}
	for index in range(platforms.size()):
		var platform: Dictionary = platforms[index]
		var platform_id := str(platform.get("id", "p%d" % index))
		var width := int(platform.get("width", 0))
		var x := int(platform.get("x", min_x - 1))
		var y := int(platform.get("y", min_y - 1))
		if ids.has(platform_id):
			issues.append({
				"error_code": "duplicate_platform_id",
				"path": "platforms[%d].id" % index,
				"actual": platform_id,
				"required": "unique platform id",
				"action": "Rename this platform and update segment references.",
			})
		ids[platform_id] = true
		if width <= 0:
			issues.append({
				"error_code": "invalid_platform_width",
				"path": "platforms[%d].width" % index,
				"actual": width,
				"required": "integer >= 1",
				"action": "Set an explicit positive platform width.",
			})
		if x < min_x or x + width - 1 > max_x or y < min_y or y > max_y:
			issues.append({
				"error_code": "platform_out_of_bounds",
				"path": "platforms[%d]" % index,
				"actual": {"x": x, "y": y, "width": width},
				"required": region,
				"action": "Move or resize the platform so every support cell stays inside the requested region.",
			})
		if str(platform.get("role", "")).is_empty():
			issues.append({
				"error_code": "platform_role_required",
				"path": "platforms[%d].role" % index,
				"actual": "",
				"required": "semantic role such as safe_intro, takeoff, landing, stair, rest, or finish",
				"action": "Assign a role so design validation can evaluate the route.",
			})
	return issues


static func _build_jump_graph(platforms: Array, ability: Dictionary) -> Dictionary:
	var edges: Array = []
	var required_unreachable: Array = []
	var optional_unreachable: Array = []
	for i in range(platforms.size()):
		for j in range(i + 1, mini(platforms.size(), i + 4)):
			var edge := _jump_edge(platforms[i], platforms[j], ability)
			edge["required"] = j == i + 1
			edges.append(edge)
			if not bool(edge.get("reachable", false)):
				if bool(edge.get("required", false)):
					required_unreachable.append(edge)
				else:
					optional_unreachable.append(edge)
	return {
		"nodes": platforms.size(),
		"edges": edges,
		"unreachable_edges": required_unreachable,
		"required_unreachable_edges": required_unreachable,
		"optional_unreachable_edges": optional_unreachable,
		"passed": required_unreachable.is_empty(),
	}


static func _jump_edge(from_platform: Dictionary, to_platform: Dictionary, ability: Dictionary) -> Dictionary:
	var from_x := int(from_platform["x"]) + int(from_platform["width"]) - 1
	var from_y := int(from_platform["y"]) - 1
	var to_x := int(to_platform["x"])
	var to_y := int(to_platform["y"]) - 1
	var horizontal := maxi(0, to_x - from_x)
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
	var thickness := clampi(int(input.get("platform_thickness", 1)), 1, int(input.get("max_platform_thickness", 2)))
	for platform in platforms:
		if bool(platform.get("existing", false)):
			continue
		var remaining := int(platform["width"])
		var cursor := int(platform["x"])
		while remaining > 0:
			var batch_width := mini(5, remaining)
			var op := {
				"action": "fill",
				"x": cursor,
				"y": int(platform["y"]),
				"width": batch_width,
				"height": thickness,
				"resource": str(input.get("ground_resource", "ground")),
				"fallback_resource": str(input.get("fallback_ground_resource", "")),
				"semantic_layer": "ground",
				"tags": ["platform", str(platform.get("role", "platform"))],
			}
			batches.append({
				"tool": "edit_map",
				"operations": [op],
				"expected_cells": batch_width * thickness,
				"platform_id": platform.get("id", ""),
				"note": "Connection batch; apply before decorative/challenge platforms." if bool(platform.get("connection", false)) else "Apply after describe_map_region confirms this support row is safe to overwrite.",
			})
			cursor += batch_width
			remaining -= batch_width
	return batches


static func _validation_plan(region: Dictionary, platforms: Array, ability: Dictionary, design_limits: Dictionary, entry_anchor: Dictionary) -> Dictionary:
	if platforms.is_empty():
		return {}
	var first: Dictionary = platforms.front()
	var last: Dictionary = platforms.back()
	var start := {"x": int(first["x"]), "y": int(first["y"]) - 1}
	if not entry_anchor.is_empty():
		start = {"x": int(entry_anchor.get("x", start["x"])), "y": int(entry_anchor.get("y", start["y"]))}
	var min_x := int(region.get("x", 0))
	var max_x := min_x + int(region.get("width", 1)) - 1
	var min_y := int(region.get("y", 0))
	var max_y := min_y + int(region.get("height", 1)) - 1
	if not entry_anchor.is_empty():
		min_x = mini(min_x, int(entry_anchor.get("x", min_x)))
		max_x = maxi(max_x, int(entry_anchor.get("x", max_x)))
		min_y = mini(min_y, int(entry_anchor.get("y", min_y)) - 3)
		max_y = maxi(max_y, int(entry_anchor.get("y", max_y)) + 1)
	for platform in platforms:
		min_x = mini(min_x, int(platform.get("x", min_x)))
		max_x = maxi(max_x, int(platform.get("x", max_x)) + int(platform.get("width", 1)) - 1)
		min_y = mini(min_y, int(platform.get("y", min_y)) - 3)
		max_y = maxi(max_y, int(platform.get("y", max_y)) + 1)
	return {
		"validate_map_region": {
			"x": min_x,
			"y": min_y,
			"width": max_x - min_x + 1,
			"height": max_y - min_y + 1,
			"start": start,
			"goal": {"x": int(last["x"]) + int(last["width"]) - 1, "y": int(last["y"]) - 1},
			"movement_model": "leap",
			"max_horizontal_gap": ability["max_horizontal_gap"],
			"max_rise": ability["max_rise"],
			"max_fall": ability["max_fall"],
			"walkable_is_filled": false,
			"path_algorithm": "astar",
			"check_overlaps": true,
			"check_blocked_objects": true,
			"check_platform_design": true,
			"max_solid_run_width": design_limits.get("max_platform_width", 12),
			"max_solid_column_height": design_limits.get("max_solid_column_height", 5),
			"max_solid_mass_width": design_limits.get("max_solid_mass_width", 10),
			"max_solid_mass_height": design_limits.get("max_solid_mass_height", 4),
			"min_finish_buffer_width": design_limits.get("min_finish_buffer_width", 6),
		}
	}


static func _score_level(segments: Array, platforms: Array, jump_graph: Dictionary, coin_arcs: Array, enemy_slots: Array, limits: Dictionary) -> Dictionary:
	var issues: Array = []
	var issue_details: Array = []
	if not bool(jump_graph.get("passed", true)):
		issues.append("one or more planned platform transitions exceed movement ability")
		issue_details.append({
			"error_code": "platform_transition_unreachable",
			"path": "platforms",
			"actual": jump_graph.get("required_unreachable_edges", []),
			"required": {
				"max_horizontal_gap": limits.get("max_horizontal_gap", null),
				"max_rise": limits.get("max_rise", null),
				"max_fall": limits.get("max_fall", null),
			},
			"action": "Move, lower, widen, or insert an explicit landing platform, then update segments.",
		})
	if segments.size() < 3:
		issues.append("route has too few gameplay segments")
		issue_details.append({
			"error_code": "route_too_short",
			"path": "segments",
			"actual": segments.size(),
			"required": 3,
			"action": "LLM must add explicit gameplay segments and their supporting platforms.",
		})
	var long_platforms: Array = []
	var repeated_roles := 0
	var previous_role := ""
	for platform in platforms:
		var width := int(platform.get("width", 0))
		var role := str(platform.get("role", ""))
		if width > int(limits.get("max_platform_width", 8)) and not (role in ["safe_intro", "rest"]):
			long_platforms.append({"id": platform.get("id", ""), "x": platform.get("x", 0), "y": platform.get("y", 0), "width": width, "role": role})
		if role == previous_role and role in ["gap_jump", "hazard_entry", "hazard_exit", "stair"]:
			repeated_roles += 1
		previous_role = role
	if not long_platforms.is_empty():
		issues.append("planned route contains overly long non-rest platforms")
		issue_details.append({
			"error_code": "platform_too_wide",
			"path": "platforms",
			"actual": long_platforms,
			"required": {"max_non_rest_width": int(limits.get("max_platform_width", 8))},
			"action": "Split or shorten the listed non-rest platforms in the explicit plan.",
		})
	if repeated_roles > int(limits.get("max_repeated_challenge_roles", 2)):
		issues.append("planned route repeats the same challenge shape too often")
		issue_details.append({
			"error_code": "challenge_roles_repeated",
			"path": "platforms[*].role",
			"actual": repeated_roles,
			"required": {"maximum": int(limits.get("max_repeated_challenge_roles", 2))},
			"action": "LLM must vary the explicit platform roles and route geometry.",
		})
	var finish_buffer := _finish_buffer_width(platforms)
	if finish_buffer < int(limits.get("min_finish_buffer_width", 6)):
		issues.append("planned finish platform is too short for a safe ending buffer")
		issue_details.append({
			"error_code": "finish_buffer_too_short",
			"path": "platforms[%d].width" % maxi(0, platforms.size() - 1),
			"platform_id": (platforms.back() as Dictionary).get("id", "") if not platforms.is_empty() else "",
			"actual": finish_buffer,
			"required": int(limits.get("min_finish_buffer_width", 6)),
			"action": "Widen or replace the final explicit platform; changing seed or region width does not repair the plan.",
		})
	var difficulty := 0
	for segment in segments:
		difficulty += int(segment.get("difficulty", 1))
	return {
		"passed": issues.is_empty(),
		"issues": issues,
		"issue_details": issue_details,
		"long_platforms": long_platforms,
		"finish_buffer_width": finish_buffer,
		"segment_count": segments.size(),
		"estimated_difficulty": difficulty,
		"reward_arc_count": coin_arcs.size(),
		"enemy_slot_count": enemy_slots.size(),
	}


static func _repair_plan(plan_issues: Array, jump_graph: Dictionary, score: Dictionary) -> Array:
	var repairs := plan_issues.duplicate(true)
	if not bool(jump_graph.get("passed", true)):
		repairs.append({
			"error_code": "jump_graph_failed",
			"path": "platforms",
			"actual": jump_graph.get("required_unreachable_edges", []),
			"action": "Revise the explicit adjacent platforms identified by the unreachable edges.",
		})
	for detail in score.get("issue_details", []):
		if detail is Dictionary:
			repairs.append((detail as Dictionary).duplicate(true))
	return repairs


static func _finish_buffer_width(platforms: Array) -> int:
	if platforms.is_empty():
		return 0
	var last: Dictionary = platforms.back()
	return int(last.get("width", 0))


static func _design_limits_from_input(input: Dictionary, ability: Dictionary) -> Dictionary:
	return {
		"max_platform_width": maxi(5, int(input.get("max_platform_width", maxi(8, int(ability.get("max_horizontal_gap", 4)) + 4)))),
		"max_platform_thickness": clampi(int(input.get("max_platform_thickness", 2)), 1, 3),
		"min_finish_buffer_width": maxi(4, int(input.get("min_finish_buffer_width", maxi(6, int(ability.get("min_landing_width", 3)) + 3)))),
		"max_repeated_challenge_roles": maxi(1, int(input.get("max_repeated_challenge_roles", 2))),
		"max_solid_column_height": maxi(3, int(input.get("max_solid_column_height", 5))),
		"max_solid_mass_width": maxi(4, int(input.get("max_solid_mass_width", 10))),
		"max_solid_mass_height": maxi(3, int(input.get("max_solid_mass_height", 4))),
	}


static func _platform(x: int, y: int, width: int, role: String) -> Dictionary:
	return {"x": x, "y": y, "width": maxi(1, width), "height": 1, "role": role}


static func _ability_from_input(input: Dictionary) -> Dictionary:
	return {
		"max_horizontal_gap": maxi(2, int(input.get("max_horizontal_gap", 4))),
		"max_rise": maxi(0, int(input.get("max_rise", 2))),
		"max_fall": maxi(1, int(input.get("max_fall", 6))),
		"min_landing_width": maxi(2, int(input.get("min_landing_width", 3))),
	}


## 哪些跳跃能力字段是调用方没传、被静默填了默认值的——返回非空说明这次规划
## 没有用到真实角色脚本数据，调用方/agent 看到这个字段不该直接执行 edit_map_batches。
static func _ability_defaulted_keys(input: Dictionary) -> Array:
	var defaulted: Array = []
	for key in ["max_horizontal_gap", "max_rise", "max_fall", "min_landing_width"]:
		if not input.has(key):
			defaulted.append(key)
	return defaulted


static func _entry_anchor_from_input(input: Dictionary) -> Dictionary:
	var raw = input.get("entry_anchor", {})
	if not (raw is Dictionary) or (raw as Dictionary).is_empty():
		raw = input.get("frontier", {})
	if raw is Dictionary and (raw as Dictionary).get("cell", {}) is Dictionary:
		raw = (raw as Dictionary).get("cell", {})
	if not (raw is Dictionary) or (raw as Dictionary).is_empty():
		return {}
	return {"x": int((raw as Dictionary).get("x", 0)), "y": int((raw as Dictionary).get("y", 0))}


static func _region_from_input(input: Dictionary) -> Dictionary:
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var width := maxi(8, int(input.get("width", 40)))
	var height := maxi(8, int(input.get("height", 20)))
	return {"x": x, "y": y, "width": width, "height": height, "depth": 1}


static func _context_summary(context: Dictionary) -> Dictionary:
	if context.is_empty():
		return {}
	return {
		"maps": (context.get("maps", []) as Array).size() if context.get("maps", []) is Array else 0,
		"registry_exists": bool(context.get("resource_registry", {}).get("exists", false)) if context.get("resource_registry", {}) is Dictionary else false,
	}
