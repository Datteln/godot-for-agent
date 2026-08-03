extends SceneTree

# relax-map-platform-plan-gates 的 GDScript 校验器回归测试（headless）。
# 覆盖 6.1（扁平 entry_anchor 被接受）与 6.2（主观 over-width 降为 advisory）。

const PlanValidator = preload("res://addons/ai_agent/tools/map_platform_plan_validator.gd")
const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")

var failures: Array = []


func _initialize() -> void:
	_test_entry_anchor_flat_accepted()
	_test_score_advisory_overwidth()
	_test_score_advisory_plus_finish_buffer_blocks()
	_test_validate_plan_primary_error_is_finish_buffer()
	_test_tile_signature_absent_atlas()
	_test_recorded_plan_replay_ships()
	if failures.is_empty():
		print("ALL_GDSCRIPT_TESTS_PASSED")
		quit(0)
	else:
		print("GDSCRIPT_TEST_FAILURES:")
		for entry in failures:
			print("  - ", entry)
		quit(1)


func _check(condition: bool, name: String) -> void:
	if not condition:
		failures.append(name)


func _test_entry_anchor_flat_accepted() -> void:
	# 6.1：扁平 {x,y,role} anchor（无 cell 包装）应被直接消费，不被丢弃为空。
	var input := {"entry_anchor": {"x": 50, "y": -4, "role": "support_cell"}}
	var anchor := PlanValidator._entry_anchor_from_input(input)
	_check(not anchor.is_empty(), "entry_anchor_flat_not_empty")
	_check(int(anchor.get("x", -999)) == 50, "entry_anchor_flat_x")
	_check(int(anchor.get("y", -999)) == -4, "entry_anchor_flat_y")


func _test_score_advisory_overwidth() -> void:
	# 6.2：非休息平台超宽（主观）应标 advisory，且不阻断 passed。
	var platforms := [{"id": "p0", "x": 51, "y": -4, "width": 99, "role": "stair"}]
	var segments := [
		{"index": 0, "type": "walk", "from_platform": "p0", "to_platform": "p0", "start": {"x": 51, "y": -5}, "end": {"x": 52, "y": -5}, "difficulty": 0},
		{"index": 1, "type": "walk", "from_platform": "p0", "to_platform": "p0", "start": {"x": 52, "y": -5}, "end": {"x": 53, "y": -5}, "difficulty": 0},
		{"index": 2, "type": "walk", "from_platform": "p0", "to_platform": "p0", "start": {"x": 53, "y": -5}, "end": {"x": 54, "y": -5}, "difficulty": 0},
	]
	var jump_graph := {"passed": true}
	var limits := {
		"max_platform_width": 5,
		"max_repeated_challenge_roles": 2,
		"min_finish_buffer_width": 6,
	}
	var score := PlanValidator._score_level(segments, platforms, jump_graph, [], [], limits)
	_check(bool(score.get("passed", false)), "score_advisory_overwidth_passed_true")
	var found_advisory := false
	for detail in score.get("issue_details", []):
		if detail is Dictionary and str((detail as Dictionary).get("error_code", "")) == "platform_too_wide":
			found_advisory = bool((detail as Dictionary).get("advisory", false))
	_check(found_advisory, "score_advisory_overwidth_flagged_advisory")


func _test_tile_signature_absent_atlas() -> void:
	# 6.4：缺 atlas_coords → 返回 {}（修复前会返回 atlas_x:-1 的伪签名）。
	var sig1 := MapTools._entry_2d_tile_signature({"source_id": 0})
	_check(sig1.is_empty(), "entry_2d_tile_signature_absent_atlas_empty")
	var sig2 := MapTools._cell_2d_tile_signature({"source_id": 0})
	_check(sig2.is_empty(), "cell_2d_tile_signature_absent_atlas_empty")


func _test_recorded_plan_replay_ships() -> void:
	# 6.5：录制会话的 2nd-attempt plan（扁平 entry_anchor + 修正后端点 y=平台y-1）
	# 经 relax 修复后应通过 validate_platform_level_plan（不再 entry_anchor_not_found）。
	var input := {
		"target_path": "TileMap", "map_layer": 1,
		"x": 49, "y": -8, "width": 10, "height": 7,
		"connect_from_existing": true,
		"entry_anchor": {"x": 50, "y": -5, "role": "support_cell"},
		"max_horizontal_gap": 4, "max_rise": 2, "max_fall": 6,
		"min_landing_width": 2, "actor_clearance_cells": 2, "actor_width_cells": 1,
		"min_finish_buffer_width": 4, "max_platform_width": 5,
		"max_repeated_challenge_roles": 2,
		"platforms": [
			{"id": "p0", "x": 51, "y": -4, "width": 3, "role": "safe_intro"},
			{"id": "p1", "x": 54, "y": -4, "width": 4, "role": "finish"},
		],
		"segments": [
			{"index": 0, "type": "walk", "from_platform": "p0", "to_platform": "p1", "direction": 1, "start": {"x": 53, "y": -5}, "end": {"x": 54, "y": -5}, "difficulty": 0},
		],
	}
	var result := PlanValidator.validate_platform_level_plan(input, {}, {"cells": {}})
	# relax 修复：扁平 anchor 不再被误判 entry_anchor_not_found。
	_check(str(result.get("blocked_reason", "")) != "entry_anchor_not_found", "replay_not_entry_anchor_not_found")
	_check(str(result.get("error_code", "")) != "entry_anchor_not_found", "replay_error_code_not_entry_anchor")
	# 更强：经修复后整盘应通过（plan_issues/ability/entry_anchor/jump_graph/score 全过）。
	_check(bool(result.get("executable", false)), "replay_executable_true")


func _test_score_advisory_plus_finish_buffer_blocks() -> void:
	# 9.4：advisory（platform_too_wide）与 blocking（finish_buffer_too_short）共存；
	# passed=false（阻断），issue_details 与 _repair_plan 均保留 advisory。
	var platforms := [
		{"id": "p0", "x": 51, "y": -4, "width": 9, "role": "stair"},  # 超宽非休息平台 → advisory
		{"id": "p1", "x": 60, "y": -4, "width": 2, "role": "finish"},  # finish buffer < min → blocking
	]
	var segments := [
		{"index": 0, "type": "walk", "from_platform": "p0", "to_platform": "p1", "start": {"x": 59, "y": -5}, "end": {"x": 60, "y": -5}, "difficulty": 0},
		{"index": 1, "type": "walk", "from_platform": "p0", "to_platform": "p1", "start": {"x": 59, "y": -5}, "end": {"x": 60, "y": -5}, "difficulty": 0},
		{"index": 2, "type": "walk", "from_platform": "p0", "to_platform": "p1", "start": {"x": 59, "y": -5}, "end": {"x": 60, "y": -5}, "difficulty": 0},
	]
	var jump_graph := {"passed": true}
	var limits := {"max_platform_width": 5, "min_finish_buffer_width": 6, "max_repeated_challenge_roles": 2}
	var score := PlanValidator._score_level(segments, platforms, jump_graph, [], [], limits)
	_check(not bool(score.get("passed", true)), "score_advisory_plus_finish_buffer_blocks")
	var has_advisory := false
	var has_finish_buffer := false
	for detail in score.get("issue_details", []):
		if detail is Dictionary:
			var code := str((detail as Dictionary).get("error_code", ""))
			if code == "platform_too_wide":
				has_advisory = bool((detail as Dictionary).get("advisory", false))
			elif code == "finish_buffer_too_short":
				has_finish_buffer = not bool((detail as Dictionary).get("advisory", false))
	_check(has_advisory, "score_advisory_plus_finish_buffer_keeps_advisory")
	_check(has_finish_buffer, "score_advisory_plus_finish_buffer_has_blocking")
	# _repair_plan 保留 advisory（所有 issue_details 都进 repair_plan）。
	var repairs := PlanValidator._repair_plan([], jump_graph, score)
	var repair_has_advisory := false
	for item in repairs:
		if item is Dictionary and str((item as Dictionary).get("error_code", "")) == "platform_too_wide":
			repair_has_advisory = true
	_check(repair_has_advisory, "score_advisory_plus_finish_buffer_repair_retains_advisory")


func _test_validate_plan_primary_error_is_finish_buffer() -> void:
	# 9.4/9.3：同一盘中 advisory（platform_too_wide）先于 blocking（finish_buffer_too_short）
	# 时，validate_platform_level_plan 阻断并把首个非 advisory 的 finish_buffer_too_short
	# 报为 error_code，advisory 仍留在 score.issue_details 与 repair_plan。
	var input := {
		"target_path": "TileMap", "map_layer": 1,
		"x": 49, "y": -8, "width": 14, "height": 7,
		"connect_from_existing": true,
		"entry_anchor": {"x": 50, "y": -5, "role": "support_cell"},
		"max_horizontal_gap": 4, "max_rise": 2, "max_fall": 6,
		"min_landing_width": 2, "actor_clearance_cells": 2, "actor_width_cells": 1,
		"min_finish_buffer_width": 4, "max_platform_width": 5,
		"max_repeated_challenge_roles": 2,
		"platforms": [
			{"id": "p0", "x": 51, "y": -4, "width": 9, "role": "stair"},
			{"id": "p1", "x": 60, "y": -4, "width": 2, "role": "finish"},
		],
		"segments": [
			{"index": 0, "type": "walk", "from_platform": "p0", "to_platform": "p1", "direction": 1, "start": {"x": 59, "y": -5}, "end": {"x": 60, "y": -5}, "difficulty": 0},
		],
	}
	var result := PlanValidator.validate_platform_level_plan(input, {}, {"cells": {}})
	# 阻断执行（finish_buffer_too_short 是 blocking）。
	_check(not bool(result.get("executable", true)), "validate_advisory_plus_finish_buffer_blocks")
	# 首个非 advisory issue_detail 成为顶层 error_code。
	_check(str(result.get("error_code", "")) == "finish_buffer_too_short", "validate_primary_error_is_finish_buffer")
	# advisory 仍留在 score.issue_details 与 repair_plan。
	var score := result.get("score", {}) as Dictionary
	var has_advisory := false
	for detail in score.get("issue_details", []):
		if detail is Dictionary and str((detail as Dictionary).get("error_code", "")) == "platform_too_wide":
			has_advisory = bool((detail as Dictionary).get("advisory", false))
	_check(has_advisory, "validate_issue_details_retains_advisory")
	var repair_has_advisory := false
	for item in result.get("repair_plan", []):
		if item is Dictionary and str((item as Dictionary).get("error_code", "")) == "platform_too_wide":
			repair_has_advisory = true
	_check(repair_has_advisory, "validate_repair_plan_retains_advisory")
