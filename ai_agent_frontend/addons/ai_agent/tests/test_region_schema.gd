extends SceneTree

## fix-map-region-schema-leak 的 GDScript 回归测试（headless）。
## 覆盖 region 规范化、缺失边界键失败闭环、occupancy typed error。

const MapValidator = preload("res://addons/ai_agent/tools/map_validator.gd")
const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")

var failures: Array = []


func _initialize() -> void:
	_test_region_bounds_error_typed()
	_test_in_region_fail_closed_no_raise()
	_test_entry_in_region_fail_closed()
	_test_occupancy_invalid_region_typed()
	_test_canonical_region_roundtrip()
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


func _good_region() -> Dictionary:
	return MapValidator.region_from_input({"x": 41, "y": -13, "width": 10, "height": 8}, 2)


func _bad_region() -> Dictionary:
	return {"x": 41, "y": -13, "z": 0, "width": 10, "height": 8, "depth": 1}


func _test_region_bounds_error_typed() -> void:
	_check(MapValidator.region_bounds_error(_good_region()) == "", "bounds_error_empty_for_canonical")
	var reason := MapValidator.region_bounds_error(_bad_region())
	_check(str(reason).begins_with("invalid_region_missing_"), "bounds_error_typed_for_handbuilt")


func _test_in_region_fail_closed_no_raise() -> void:
	_check(MapValidator.in_region(Vector3i(41, -13, 0), _good_region()), "in_region_true_inside_canonical")
	_check(not MapValidator.in_region(Vector3i(0, 0, 0), _bad_region()), "in_region_false_for_bad_region")


func _test_entry_in_region_fail_closed() -> void:
	var entry := {"coords": {"x": 41, "y": -13}}
	_check(MapTools._entry_in_region(entry, _good_region(), 2), "entry_in_region_true_canonical")
	_check(not MapTools._entry_in_region(entry, _bad_region(), 2), "entry_in_region_false_bad_region")


func _test_occupancy_invalid_region_typed() -> void:
	var result := MapTools._live_object_occupancy(null, _bad_region(), 2)
	_check(str(result.get("error_code", "")) == "invalid_region", "occupancy_typed_error_for_bad_region")
	_check(not bool(result.get("complete", true)), "occupancy_incomplete_for_bad_region")


func _test_canonical_region_roundtrip() -> void:
	var region := _good_region()
	_check(int(region.get("min_x", -999)) == 41, "canonical_min_x")
	_check(int(region.get("max_x", -999)) == 50, "canonical_max_x")
	_check(int(region.get("min_y", -999)) == -13, "canonical_min_y")
	_check(int(region.get("max_y", -999)) == -6, "canonical_max_y")
