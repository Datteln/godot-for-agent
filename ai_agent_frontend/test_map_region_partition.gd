## 地图区域读取 400-cell 约束与有界分块测试（streaming-transcript-backpressure 任务 5.2 / 5.3）。
##
## `describe_map_region` 的分块/约束逻辑是纯静态函数，可在无编辑器场景下直接验证：
## - 分块矩形互不重叠、无遗漏，拼回后恰好等于原区域（语义保持）；
## - 每个分块单元数不超过 400；
## - 结构化错误携带上限与安全约束，供模型缩小请求重试（可恢复，不终止轮次）。
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
	_run_partition_covers_region_exactly_test()
	_run_partition_respects_cell_bound_test()
	_run_region_too_large_constraint_test()
	print("map region partition checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


## 分块必须恰好无重叠、无遗漏地覆盖原区域（语义保持，任务 5.2）。
func _run_partition_covers_region_exactly_test() -> void:
	var cases := [
		[0, 0, 25, 25],     # 625 cells -> 需要分块
		[3, -2, 30, 40],    # 1200 cells，带负原点
		[0, 0, 21, 1],      # 21 cells 单行（低于上限也验证形状）
		[0, 0, 450, 3],     # 宽超过单块上限，需列向切分
	]
	for case_value in cases:
		var case: Array = case_value
		var ox := int(case[0])
		var oy := int(case[1])
		var w := int(case[2])
		var h := int(case[3])
		var partitions: Array = MapTools._partition_region_2d(ox, oy, w, h)
		var covered := {}
		var total := 0
		var valid := true
		for partition_value in partitions:
			var partition: Dictionary = partition_value
			var px := int(partition.get("x", 0))
			var py := int(partition.get("y", 0))
			var pw := int(partition.get("width", 0))
			var ph := int(partition.get("height", 0))
			if pw <= 0 or ph <= 0:
				valid = false
				continue
			for yy in range(ph):
				for xx in range(pw):
					var key := "%d,%d" % [px + xx, py + yy]
					if covered.has(key):
						valid = false   # 重叠
					covered[key] = true
					total += 1
		_check(valid, "partition: no overlap / valid rectangles for %dx%d" % [w, h])
		_check(total == w * h, "partition: cell count preserved for %dx%d (%d == %d)" % [w, h, total, w * h])
		_check(covered.size() == w * h, "partition: full coverage for %dx%d" % [w, h])


## 每个分块的单元数都不得超过 400（有界，任务 5.2）。
func _run_partition_respects_cell_bound_test() -> void:
	var cases := [[0, 0, 25, 25], [0, 0, 450, 3], [0, 0, 20, 20], [0, 0, 400, 8]]
	for case_value in cases:
		var case: Array = case_value
		var partitions: Array = MapTools._partition_region_2d(int(case[0]), int(case[1]), int(case[2]), int(case[3]))
		var bounded := true
		for partition_value in partitions:
			var partition: Dictionary = partition_value
			if int(partition.get("cells", 0)) > MapTools.MAX_DESCRIBED_CELLS:
				bounded = false
		_check(bounded, "partition: every piece <= %d cells for %dx%d" % [MapTools.MAX_DESCRIBED_CELLS, int(case[2]), int(case[3])])


## 结构化错误必须携带上限与安全约束，供缩小请求重试（任务 5.1 / 5.3）。
func _run_region_too_large_constraint_test() -> void:
	var error: Dictionary = MapTools._region_too_large_error(5000)
	_check(bool(error.get("ok", true)) == false, "error: ok is false")
	_check(str(error.get("error_code", "")) == "region_too_large", "error: typed code")
	_check(int(error.get("max_cells", 0)) == MapTools.MAX_DESCRIBED_CELLS, "error: carries max_cells")
	_check(int(error.get("requested_cells", 0)) == 5000, "error: echoes requested cell count")
	var constraint: Dictionary = error.get("constraint", {}) if error.get("constraint", {}) is Dictionary else {}
	_check(int(constraint.get("max_total_cells", 0)) == MapTools.MAX_DESCRIBED_CELLS, "error: constraint total")
	_check(int(constraint.get("safe_width", 0)) > 0 and int(constraint.get("safe_height", 0)) > 0, "error: safe width/height suggested")
	_check(int(constraint.get("safe_width", 0)) * int(constraint.get("safe_height", 0)) <= MapTools.MAX_DESCRIBED_CELLS, "error: suggested size fits the limit")
