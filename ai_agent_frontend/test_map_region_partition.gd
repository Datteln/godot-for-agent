## 地图区域读取有界观察与紧凑摘要测试。
##
## `describe_map_region` 的预算逻辑是纯静态函数，可在无编辑器场景下直接验证：
## - 扫描范围不超过观察上限；
## - 细节和紧凑摘要拥有独立上限；
## - 大范围请求由观察元数据引导后续聚焦查询，而不是导出全图。
extends SceneTree

const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")

var _failures := 0
var _checks := 0


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _init() -> void:
	_run_bounded_observation_extent_test()
	_run_detail_and_summary_budget_test()
	_run_next_query_progression_test()
	print("map region partition checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


## 请求再大也只扫描明确的连续前缀（任务 5.1 / 6.2）。
func _run_bounded_observation_extent_test() -> void:
	var cases := [[60, 100, 1, 2], [5000, 2, 1, 2], [20, 20, 20, 3]]
	for case_value in cases:
		var case: Array = case_value
		var extent: Dictionary = MapTools._bounded_observed_extent(int(case[0]), int(case[1]), int(case[2]), int(case[3]))
		var observed := int(extent.get("width", 0)) * int(extent.get("height", 0)) * int(extent.get("depth", 0))
		_check(observed > 0 and observed <= MapTools.MAX_OBSERVED_CELLS, "observation extent stays within scan budget")


## 两个返回预算都必须低于观察预算，避免模型上下文被单次请求撑爆。
func _run_detail_and_summary_budget_test() -> void:
	_check(MapTools.MAX_DESCRIBED_CELLS < MapTools.MAX_OBSERVED_CELLS, "detailed cells are capped below observed cells")
	_check(MapTools.MAX_SUMMARY_RUNS < MapTools.MAX_OBSERVED_CELLS, "summary runs are capped below observed cells")


## 后续查询提示应沿真正被截断的轴推进，避免重复观察同一块区域。
func _run_next_query_progression_test() -> void:
	var vertical: Dictionary = MapTools._next_query_hint(Vector3i(10, 20, 0), 60, 100, 1, 60, 53, 1, 2, 0)
	_check(int(vertical.get("x", -1)) == 10 and int(vertical.get("y", -1)) == 73, "2D hint advances y after height truncation")
	var horizontal: Dictionary = MapTools._next_query_hint(Vector3i(10, 20, 0), 5000, 2, 1, 3200, 1, 1, 2, 0)
	_check(int(horizontal.get("x", -1)) == 3210 and int(horizontal.get("y", -1)) == 20, "2D hint advances x after width truncation")
	var depth: Dictionary = MapTools._next_query_hint(Vector3i(1, 2, 3), 20, 20, 20, 20, 20, 8, 3, 0)
	_check(int(depth.get("z", -1)) == 11 and int(depth.get("depth", 0)) == 8, "3D hint advances depth after depth truncation")
