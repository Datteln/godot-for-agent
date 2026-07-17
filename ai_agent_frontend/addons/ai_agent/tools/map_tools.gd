@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")
const MapValidator = preload("res://addons/ai_agent/tools/map_validator.gd")
const MapBlueprints = preload("res://addons/ai_agent/tools/map_blueprints.gd")
const MapLayerScaffold = preload("res://addons/ai_agent/tools/map_layer_scaffold.gd")
const MapIntentParser = preload("res://addons/ai_agent/tools/map_intent_parser.gd")
const MapLayoutPlanner = preload("res://addons/ai_agent/tools/map_layout_planner.gd")
const MapAlgorithms = preload("res://addons/ai_agent/tools/map_algorithms.gd")
const MapPlatformComposer = preload("res://addons/ai_agent/tools/map_platform_composer.gd")
const MapReachableGrowth = preload("res://addons/ai_agent/tools/map_reachable_growth.gd")

const MAX_EDITED_CELLS := 100000
const MAX_EDIT_MAP_BATCH_CELLS := 2000
const MAX_DESCRIBED_CELLS := 800
const DEFAULT_DESCRIBE_RETURNED_CELLS := 120
## describe_map_region 是纯内存读取（一次循环读完整片区域），800 只是响应体大小的策略上限，
## 不是 IO 成本。超过 800 但不超过这个上限时直接自动整片返回（标 auto_served），模型无需自己
## 分块多次查询；只有超过这个上限才回退成 region_too_large + suggested_regions。
const MAX_AUTOSERVED_DESCRIBED_CELLS := 1600
const MAX_NOISE_CELLS := 4096
## 空间索引整份读出/整份重写，条目数上限防止它随使用无限膨胀、拖慢每次 edit_map。
## 到顶后仍允许更新/删除已有坐标，只拒绝新增坐标，并在结果里给出 warning。
const MAX_SPATIAL_INDEX_ENTRIES := 20000
## 地图 agent 运行期生成的数据统一落在 Godot 项目的 res://.ai_agent_service/map_agent 里，
## 不再写进 addons（避免污染插件目录，也方便整目录清理）。
const MAP_DATA_DIR := "res://.ai_agent_service/map_agent"
const RESOURCE_REGISTRY_PATH := "res://.ai_agent_service/map_agent/resource_registry.json"
const SPATIAL_INDEX_PATH := "res://.ai_agent_service/map_agent/spatial_index.json"
const BLUEPRINTS_DIR := "res://.ai_agent_service/map_agent/blueprints"


static func _map_completion_blocker(reason: String, issues: Array = []) -> Dictionary:
	return {
		"completion_allowed": false,
		"blocking_completion": true,
		"validation": {
			"passed": false,
			"blocking_completion": true,
			"issues": issues if not issues.is_empty() else [reason],
		},
	}


static func _merge_map_completion_blocker(result: Dictionary, reason: String, issues: Array = []) -> Dictionary:
	var merged := result.duplicate(true)
	merged.merge(_map_completion_blocker(reason, issues), true)
	return merged


static func _operation_cells(operation: Dictionary, dimension: int) -> int:
	var width := max(1, int(operation.get("width", 1)))
	var height := max(1, int(operation.get("height", 1)))
	var depth := max(1, int(operation.get("depth", 1))) if dimension == 3 else 1
	return width * height * depth


static func _validate_edit_map_batch_shape(operations: Array, dimension: int) -> Dictionary:
	var total_cells := 0
	for operation_value in operations:
		if not (operation_value is Dictionary):
			return {"ok": false, "message": "each operation must be an object", "error_code": "invalid_operation"}
		var operation: Dictionary = operation_value
		var cells := _operation_cells(operation, dimension)
		total_cells += cells
		if cells > MAX_EDIT_MAP_BATCH_CELLS:
			return _merge_map_completion_blocker({
				"ok": false,
				"message": "single edit_map operation writes %d cells, over the %d-cell batch limit; split it into smaller previewed chunks." % [cells, MAX_EDIT_MAP_BATCH_CELLS],
				"error_code": "map_edit_batch_too_large",
				"cells": cells,
				"max_cells": MAX_EDIT_MAP_BATCH_CELLS,
				"hint": "Retry with smaller edit_map calls whose total expected_cells is <= max_cells.",
			}, "map_edit_batch_too_large")
	if total_cells > MAX_EDIT_MAP_BATCH_CELLS:
		return _merge_map_completion_blocker({
			"ok": false,
			"message": "edit_map batch writes %d cells in total, over the %d-cell batch limit; split it into smaller previewed chunks." % [total_cells, MAX_EDIT_MAP_BATCH_CELLS],
			"error_code": "map_edit_batch_too_large",
			"cells": total_cells,
			"max_cells": MAX_EDIT_MAP_BATCH_CELLS,
			"hint": "Retry with smaller edit_map calls whose total expected_cells is <= max_cells.",
		}, "map_edit_batch_too_large")
	return {"ok": true}


static func describe_selection(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	for node in editor_interface.get_selection().get_selected_nodes():
		if node != null and node.get_class() == "TileMapLayer":
			var path := str(root.get_path_to(node)) if root != null else str(node.get_path())
			return {"ok": true, "path": path, "type": "TileMapLayer"}

	if root == null:
		return {"ok": false, "message": "Select a TileMapLayer first"}
	var found: Array = []
	_collect_tilemap_layers(root, found)
	if found.size() == 1:
		var node: Node = found[0]
		return {"ok": true, "path": str(root.get_path_to(node)), "type": "TileMapLayer", "auto_detected": true}
	if found.size() > 1:
		var paths: Array = []
		for n in found:
			paths.append(str(root.get_path_to(n)))
		return {"ok": false, "message": "Multiple TileMapLayer nodes found, select one", "candidates": paths}
	return {"ok": false, "message": "Select a TileMapLayer first"}


static func _collect_tilemap_layers(node: Node, out: Array) -> void:
	if node.get_class() == "TileMapLayer":
		out.append(node)
	for child in node.get_children():
		_collect_tilemap_layers(child, out)


static func _count_scene_nodes(node: Node) -> int:
	var total := 1
	for child in node.get_children():
		total += _count_scene_nodes(child)
	return total


## 读取当前场景中的地图节点、资源语义表和空间索引状态，作为地图任务的项目认知入口。
static func describe_map_context(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No scene is currently being edited", "error_code": "no_edited_scene"}

	var found: Array = []
	_collect_map_nodes(root, found)
	var maps: Array = []
	for node in found:
		maps.append(_describe_map_node(root, node))

	var registry := _read_json_resource(RESOURCE_REGISTRY_PATH)
	var spatial_index := _read_json_resource(SPATIAL_INDEX_PATH)
	var entries_2d := _count_index_entries(spatial_index.get("data", {}).get("2d", {}))
	var entries_3d := _count_index_entries(spatial_index.get("data", {}).get("3d", {}))
	return {
		"ok": true,
		"scene": root.scene_file_path,
		"maps": maps,
		"performance": {
			"scene_node_count": _count_scene_nodes(root),
			"map_node_count": maps.size(),
			"spatial_index_entries": entries_2d + entries_3d,
		},
		"resource_registry": registry,
		"spatial_index": {
			"path": SPATIAL_INDEX_PATH,
			"exists": bool(spatial_index.get("exists", false)),
			"entries_2d": entries_2d,
			"entries_3d": entries_3d,
			"entries_total": entries_2d + entries_3d,
			"max_entries": MAX_SPATIAL_INDEX_ENTRIES,
			"usage_ratio": float(entries_2d + entries_3d) / float(MAX_SPATIAL_INDEX_ENTRIES),
		},
		"notes": [
			"Use describe_map_region for exact cells before editing a target area.",
			"Use edit_map with update_spatial_index=true when the task needs durable local modification context.",
		],
	}


## Edit serialized map content through Godot APIs. This deliberately never reads or rewrites
## TileMapLayer.tile_map_data/TileMap layer byte arrays directly.
static func edit_map(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var operations: Array = input.get("operations", [])
	if operations.is_empty():
		return {"ok": false, "message": "operations must not be empty", "error_code": "invalid_operations"}
	if operations.size() > 128:
		return {"ok": false, "message": "at most 128 operations are allowed", "error_code": "map_edit_too_large"}

	var dimension := 3 if target.get_class() == "GridMap" else 2
	if not input.has("expected_cells"):
		return _merge_map_completion_blocker({
			"ok": false,
			"message": "expected_cells is required for every edit_map call so the tool can reject off-by-one or under-specified map edits before writing.",
			"error_code": "expected_cells_required",
		}, "expected_cells_required")
	var batch_shape := _validate_edit_map_batch_shape(operations, dimension)
	if not bool(batch_shape.get("ok", false)):
		return batch_shape
	var map_layer := int(input.get("map_layer", 0))
	var allowed_bounds := _bounds_from_input(input, dimension)
	# 编辑前先记录"毯式图层"（背景/天空这类本来就铺满全图的图层）的现状，
	# 编辑后跟新的整体范围一比，就能发现哪些图层没跟着扩——不用 agent 自己记得去查，
	# edit_map 自己的返回结果里就会带出来。GridMap(3D)/无同组图层时这里自然是空操作。
	var coverage_edited_extent_before := _layer_used_extent_2d(target, map_layer)
	var coverage_siblings := _sibling_layers_for_coverage(target, map_layer)
	for sibling in coverage_siblings:
		var sibling_node: Node = (sibling as Dictionary)["node"]
		var sibling_map_layer := int((sibling as Dictionary)["map_layer"])
		(sibling as Dictionary)["extent"] = _layer_used_extent_2d(sibling_node, sibling_map_layer)
		(sibling as Dictionary)["columns"] = _used_columns_2d(sibling_node, sibling_map_layer)
	var before: Array = []
	var after: Array = []
	var touched := {}
	var pending_cells := {}
	for operation_value in operations:
		if not (operation_value is Dictionary):
			return {"ok": false, "message": "each operation must be an object", "error_code": "invalid_operation"}
		var operation: Dictionary = operation_value
		var resolved_resource_entry := _apply_registry_fallback_to_operation(operation, dimension)
		var resource_contract_check := _validate_operation_resource_contract(operation, resolved_resource_entry, dimension)
		if not bool(resource_contract_check.get("ok", true)):
			return resource_contract_check
		var ground_reference_check := _validate_ground_fill_reference(
			target, dimension, map_layer, operation, resolved_resource_entry
		)
		if not bool(ground_reference_check.get("ok", true)):
			return ground_reference_check
		var resolved_scene_path := str(resolved_resource_entry.get("scene_path", "")).strip_edges()
		if resolved_scene_path != "":
			# 语义表里这个资源是按 scene_path（对象/PackedScene）登记的，不是瓦片——用 edit_map
			# 硬拒绝，而不是只在 prompt 里提醒模型"别这么做"。否则模型容易用 fill 拼瓦片
			# 凑出一个视觉上不对的近似形状（比如把"树"垒成几块毫不相关的地形瓦片）。
			return {
				"ok": false,
				"message": (
					"Resource '%s' is registered with scene_path '%s' (an object/PackedScene), not a tile — " +
					"use place_map_objects to instantiate it instead of edit_map."
				) % [str(resolved_resource_entry.get("_resolved_resource", operation.get("resource", operation.get("resource_key", "")))), resolved_scene_path],
				"error_code": "resource_requires_object_placement",
				"resource": str(resolved_resource_entry.get("_resolved_resource", "")),
				"scene_path": resolved_scene_path,
			}
		var built := _build_map_operation(target, dimension, map_layer, operation, pending_cells)
		if not bool(built.get("ok", false)):
			return built
		for cell_value in built.get("cells", []):
			var cell: Dictionary = cell_value
			if not _cell_within_bounds(cell.get("coords", Vector3i.ZERO), allowed_bounds):
				return {
					"ok": false,
					"message": "map edit would write outside allowed_bounds",
					"error_code": "map_edit_out_of_bounds",
					"coords": MapValidator.coord_payload(cell.get("coords", Vector3i.ZERO), dimension),
					"allowed_bounds": allowed_bounds,
				}
			var key := _cell_key(cell, dimension, map_layer)
			if not touched.has(key):
				before.append(_read_map_cell(target, cell["coords"], dimension, map_layer))
				touched[key] = after.size()
			after.append(cell)
			pending_cells[key] = cell.duplicate(true)
			if after.size() > MAX_EDITED_CELLS:
				return {
					"ok": false,
					"message": "map edit exceeds the 100000-cell safety limit",
					"error_code": "map_edit_too_large"
				}

	# 调用方可声明 expected_cells（这一批本应写多少格）。不符时只发警告、不阻断写入——
	# 操作的坐标和宽高由 LLM 明确指定，实际格数由操作本身决定；expected_cells 只是自校验，
	# 不应该因为 LLM 算术错误（如 146 vs 149）就硬拒绝导致反复重试耗尽 max_turns。
	var cell_count_warning := ""
	if input.has("expected_cells"):
		var expected_cells := int(input.get("expected_cells"))
		if expected_cells != after.size():
			cell_count_warning = (
				"expected_cells=%d but operations actually write %d cells " +
				"(LLM self-check mismatch; operations executed as specified)."
			) % [expected_cells, after.size()]

	# 先写空间索引，再动瓦片：如果索引写盘失败，此时还没有任何瓦片落入 Undo 批次，
	# 可以直接返回错误而不留下"已改了瓦片却报失败"的半截状态（否则模型会拿着失败结果
	# 重试同一次 edit_map，而瓦片其实已经改了，造成静默双写/错位）。
	var index_result := _maybe_update_spatial_index(input, undo_manager, target, str(target_result.get("path", "")), dimension, after)
	if not bool(index_result.get("ok", true)):
		return index_result
	if undo_manager != null:
		undo_manager.record_tile_cells(target, before, after)
	else:
		for cell in after:
			_apply_map_cell(target, cell)
	var coverage_edited_extent_after := _layer_used_extent_2d(target, map_layer)
	var coverage_target_columns_after := _used_columns_2d(target, map_layer)
	var coverage_gaps := _compute_coverage_gaps(coverage_edited_extent_before, coverage_edited_extent_after, coverage_siblings, coverage_target_columns_after)
	var result := {
		"ok": true,
		"validation_required": true,
		"validation": {
			"status": "pending",
			"issues": ["map_edit_requires_followup_validation"],
		},
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"operations": operations.size(),
		"cells": after.size(),
		"spatial_index": index_result,
		"layer_coverage_gaps": coverage_gaps,
		"message": "Map edited through Godot native APIs; serialized map data was not modified directly."
	}
	var visual_groups := _summarize_visual_groups_from_cells(after)
	if int(visual_groups.get("count", 0)) > 0:
		result["visual_groups"] = visual_groups
		var expected_groups := int(input.get("expected_visual_groups", input.get("expected_instances", -1)))
		if expected_groups >= 0 and expected_groups != int(visual_groups.get("count", 0)):
			result["visual_group_warning"] = "expected_visual_groups=%d but this batch wrote %d visual groups; read back the region before treating decoration/object goals as complete." % [expected_groups, int(visual_groups.get("count", 0))]
		var incomplete_groups: Array = visual_groups.get("incomplete", [])
		if not incomplete_groups.is_empty():
			result["visual_group_warning"] = "one or more visual groups wrote fewer cells than their required_cells; read back and repair before completion."
	if not coverage_gaps.is_empty():
		result["coverage_gap_warning"] = "One or more full-coverage layers (background/sky/water etc.) now fall short of this map's overall extent. Extend them to match before treating this edit as finished."
	if cell_count_warning != "":
		result["cell_count_warning"] = cell_count_warning
	return result


## 单个 2D 图层（legacy TileMap 的某个 layer index，或某个 TileMapLayer 节点）当前
## 实际有瓦片覆盖的格子范围（min/max x/y，单位是 cell）。3D/GridMap、空图层都返回 {}
## ——跟 `_used_bounds_2d`/describe_map_context 已经在用的"空字典代表没数据"约定保持一致，
## TileMap 这边直接复用 `_used_bounds_2d`，不再重复写一遍 min/max 扫描。
static func _layer_used_extent_2d(node: Node, map_layer: int) -> Dictionary:
	if node == null:
		return {}
	if node.get_class() == "TileMapLayer":
		var rect: Rect2i = node.call("get_used_rect")
		if rect.size.x <= 0 or rect.size.y <= 0:
			return {}
		return {
			"min_x": rect.position.x,
			"max_x": rect.position.x + rect.size.x - 1,
			"min_y": rect.position.y,
			"max_y": rect.position.y + rect.size.y - 1,
		}
	if node.get_class() == "TileMap":
		return _used_bounds_2d(node.call("get_used_cells", map_layer))
	return {}


## 取一个 2D 图层当前"有瓦片的列"集合（x -> true）。光比 min/max 边界测不出中间的洞——
## 背景层最外边界可能早就跟上了，但夹在中间的一段从来没铺过；逐列扫一遍才能发现这种洞。
static func _used_columns_2d(node: Node, map_layer: int) -> Dictionary:
	var columns := {}
	if node == null:
		return columns
	var cells: Array = []
	if node.get_class() == "TileMapLayer":
		cells = node.call("get_used_cells")
	elif node.get_class() == "TileMap":
		cells = node.call("get_used_cells", map_layer)
	for cell_value in cells:
		var coords: Vector2i = cell_value
		columns[coords.x] = true
	return columns


## 把一串已排序的整数列号压缩成连续区间，报告里不堆一长串单独的列号。
static func _compress_columns_to_ranges(columns: Array) -> Array:
	if columns.is_empty():
		return []
	var ranges: Array = []
	var range_start := int(columns[0])
	var range_end := int(columns[0])
	for i in range(1, columns.size()):
		var value := int(columns[i])
		if value == range_end + 1:
			range_end = value
		else:
			ranges.append({"from": range_start, "to": range_end})
			range_start = value
			range_end = value
	ranges.append({"from": range_start, "to": range_end})
	return ranges


## 跟当前编辑的图层"同组"的其它图层：legacy TileMap 下是同一个节点的其它 layer index；
## 标准脚手架下是同一个父节点下的其它 TileMapLayer 兄弟节点。GridMap 没有这个概念，返回空。
static func _sibling_layers_for_coverage(target: Node, map_layer: int) -> Array:
	var siblings: Array = []
	if target.get_class() == "TileMap":
		var count := int(target.call("get_layers_count"))
		for idx in range(count):
			if idx == map_layer:
				continue
			var label := str(target.call("get_layer_name", idx))
			if label == "":
				label = "layer_%d" % idx
			siblings.append({"node": target, "map_layer": idx, "label": label})
	elif target.get_class() == "TileMapLayer":
		var parent := target.get_parent()
		if parent != null:
			for child in parent.get_children():
				if child != target and child.get_class() == "TileMapLayer":
					siblings.append({"node": child, "map_layer": 0, "label": str(child.name)})
	return siblings


## 合并多个 extent（跳过空字典），得到覆盖它们全部的最小范围。空输入返回 {}。
static func _merge_extents_2d(extents: Array) -> Dictionary:
	var result := {}
	for extent_value in extents:
		var extent: Dictionary = extent_value
		if extent.is_empty():
			continue
		if result.is_empty():
			result = {
				"min_x": int(extent["min_x"]),
				"max_x": int(extent["max_x"]),
				"min_y": int(extent["min_y"]),
				"max_y": int(extent["max_y"]),
			}
		else:
			result["min_x"] = mini(int(result["min_x"]), int(extent["min_x"]))
			result["max_x"] = maxi(int(result["max_x"]), int(extent["max_x"]))
			result["min_y"] = mini(int(result["min_y"]), int(extent["min_y"]))
			result["max_y"] = maxi(int(result["max_y"]), int(extent["max_y"]))
	return result


## "毯式图层"判定：覆盖范围在 x 或 y 任一轴上已经占到整图范围的 90% 以上，
## 就认为这个图层本来就该跟着地图整体范围走（背景/天空/水面渐变这类）。
## 阈值先用 0.9 试效果，不合适再调；目前 OR 判定对"又高又窄"的竖向装饰会误判，
## 是已知的待改进点，不是这次要解决的问题。
static func _is_blanket_layer(extent: Dictionary, union_extent: Dictionary) -> bool:
	if extent.is_empty() or union_extent.is_empty():
		return false
	var union_w := int(union_extent["max_x"]) - int(union_extent["min_x"]) + 1
	var union_h := int(union_extent["max_y"]) - int(union_extent["min_y"]) + 1
	var extent_w := int(extent["max_x"]) - int(extent["min_x"]) + 1
	var extent_h := int(extent["max_y"]) - int(extent["min_y"]) + 1
	var ratio_x := float(extent_w) / float(maxi(1, union_w))
	var ratio_y := float(extent_h) / float(maxi(1, union_h))
	return ratio_x >= 0.9 or ratio_y >= 0.9


## 哪些"毯式"图层的覆盖范围已经跟不上地图整体范围了。extent_before/extent_after 是
## 同一个目标图层自己的范围（`edit_map` 传编辑前后两个不同的值；`validate_map_region`
## 没有编辑动作，传同一个"当前范围"两次即可，逻辑完全复用）。siblings 是同组其它图层
## （每个元素带它们当前的 "extent" 和 "columns"，不受这次调用影响）。target_columns_after
## 是目标图层自己当前（`edit_map` 编辑后/`validate_map_region` 当前）的"有瓦片的列"集合，
## 用来做逐列空洞扫描——只比边界测不出"中间漏了一段"这种洞，边界对得上也可能有洞。
static func _compute_coverage_gaps(extent_before: Dictionary, extent_after: Dictionary, siblings: Array, target_columns_after: Dictionary = {}) -> Array:
	if extent_after.is_empty() or siblings.is_empty():
		return []
	var sibling_extents: Array = []
	for sibling_value in siblings:
		sibling_extents.append((sibling_value as Dictionary).get("extent", {}))
	var union_before := _merge_extents_2d([extent_before] + sibling_extents)
	var union_after := _merge_extents_2d([extent_after] + sibling_extents)
	if union_after.is_empty():
		return []
	var tolerance := 2
	var gaps: Array = []
	for sibling_value in siblings:
		var sibling: Dictionary = sibling_value
		var sibling_extent: Dictionary = sibling.get("extent", {})
		if sibling_extent.is_empty():
			continue
		if not _is_blanket_layer(sibling_extent, union_before):
			continue
		var shortfall := {}
		if int(sibling_extent["min_x"]) > int(union_after["min_x"]) + tolerance:
			shortfall["left"] = int(sibling_extent["min_x"]) - int(union_after["min_x"])
		if int(sibling_extent["max_x"]) < int(union_after["max_x"]) - tolerance:
			shortfall["right"] = int(union_after["max_x"]) - int(sibling_extent["max_x"])
		if int(sibling_extent["min_y"]) > int(union_after["min_y"]) + tolerance:
			shortfall["top"] = int(sibling_extent["min_y"]) - int(union_after["min_y"])
		if int(sibling_extent["max_y"]) < int(union_after["max_y"]) - tolerance:
			shortfall["bottom"] = int(union_after["max_y"]) - int(sibling_extent["max_y"])
		var sibling_columns: Dictionary = sibling.get("columns", {})
		var missing_columns: Array = []
		for column_key in target_columns_after.keys():
			if not sibling_columns.has(column_key):
				missing_columns.append(int(column_key))
		missing_columns.sort()
		var interior_holes := _compress_columns_to_ranges(missing_columns)
		if not shortfall.is_empty() or not interior_holes.is_empty():
			var gap_entry := {
				"layer": sibling.get("label", ""),
				"map_layer": sibling.get("map_layer", 0),
				"current_extent": {
					"min_x": sibling_extent["min_x"],
					"max_x": sibling_extent["max_x"],
					"min_y": sibling_extent["min_y"],
					"max_y": sibling_extent["max_y"],
				},
			}
			if not shortfall.is_empty():
				gap_entry["shortfall_cells"] = shortfall
			if not interior_holes.is_empty():
				gap_entry["interior_holes_x"] = interior_holes
			gaps.append(gap_entry)
	return gaps


## 使用 TileSet terrain connect API 绘制一组 2D terrain cell，让道路/水域边缘自动衔接。
static func validate_layer_coverage(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	if target.get_class() == "GridMap":
		return {"ok": true, "target": str(target_result.get("path", "")), "dimension": 3, "passed": true, "layer_coverage_gaps": []}
	var map_layer := int(input.get("map_layer", 0))
	var coverage_extent := _layer_used_extent_2d(target, map_layer)
	var coverage_siblings := _sibling_layers_for_coverage(target, map_layer)
	for sibling in coverage_siblings:
		var sibling_node: Node = (sibling as Dictionary)["node"]
		var sibling_map_layer := int((sibling as Dictionary)["map_layer"])
		(sibling as Dictionary)["extent"] = _layer_used_extent_2d(sibling_node, sibling_map_layer)
		(sibling as Dictionary)["columns"] = _used_columns_2d(sibling_node, sibling_map_layer)
	var coverage_target_columns := _used_columns_2d(target, map_layer)
	var coverage_gaps := _compute_coverage_gaps(coverage_extent, coverage_extent, coverage_siblings, coverage_target_columns)
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"dimension": 2,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"target_extent": coverage_extent,
		"passed": coverage_gaps.is_empty(),
		"layer_coverage_gaps": coverage_gaps,
	}


static func repair_layer_coverage(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	if target.get_class() == "GridMap":
		return {"ok": false, "message": "layer coverage repair is only available for 2D TileMap/TileMapLayer targets", "error_code": "unsupported_map_type"}
	var map_layer := int(input.get("map_layer", 0))
	var target_extent := _layer_used_extent_2d(target, map_layer)
	var siblings := _sibling_layers_for_coverage(target, map_layer)
	var sibling_extents: Array = []
	for sibling in siblings:
		var sibling_node: Node = (sibling as Dictionary)["node"]
		var sibling_map_layer := int((sibling as Dictionary)["map_layer"])
		(sibling as Dictionary)["extent"] = _layer_used_extent_2d(sibling_node, sibling_map_layer)
		(sibling as Dictionary)["columns"] = _used_columns_2d(sibling_node, sibling_map_layer)
		sibling_extents.append((sibling as Dictionary)["extent"])
	var target_columns := _used_columns_2d(target, map_layer)
	var gaps := _compute_coverage_gaps(target_extent, target_extent, siblings, target_columns)
	if gaps.is_empty():
		return {"ok": true, "target": str(target_result.get("path", "")), "repaired": false, "cells": 0, "layer_coverage_gaps": []}
	var union_extent := _merge_extents_2d([target_extent] + sibling_extents)
	var max_cells := max(1, int(input.get("max_cells", 4096)))
	var jobs: Array = []
	var repaired_layers := {}
	for gap_value in gaps:
		var gap: Dictionary = gap_value
		var repair_sibling := _coverage_sibling_for_gap(siblings, gap)
		if repair_sibling.is_empty():
			continue
		var repair_node: Node = repair_sibling["node"]
		var repair_layer := int(repair_sibling["map_layer"])
		var repair_extent: Dictionary = repair_sibling.get("extent", {})
		if repair_extent.is_empty():
			continue
		var columns := _coverage_repair_columns(gap, union_extent, target_columns)
		for x_value in columns:
			var x := int(x_value)
			var source_x := clampi(x, int(repair_extent["min_x"]), int(repair_extent["max_x"]))
			for y in range(int(union_extent["min_y"]), int(union_extent["max_y"]) + 1):
				var target_coords := Vector3i(x, y, 0)
				var source_coords := Vector3i(source_x, clampi(y, int(repair_extent["min_y"]), int(repair_extent["max_y"])), 0)
				var source_cell := _read_map_cell(repair_node, source_coords, 2, repair_layer)
				if _is_empty_cell(repair_node, source_cell):
					continue
				var current_cell := _read_map_cell(repair_node, target_coords, 2, repair_layer)
				if not _is_empty_cell(repair_node, current_cell):
					continue
				var next_cell := source_cell.duplicate(true)
				next_cell["coords"] = target_coords
				next_cell["map_layer"] = repair_layer
				jobs.append({"node": repair_node, "before": current_cell, "after": next_cell})
				repaired_layers[str(gap.get("layer", repair_layer))] = true
				if jobs.size() > max_cells:
					return {
						"ok": false,
						"message": "layer coverage repair exceeds max_cells; rerun with a smaller region or larger max_cells",
						"error_code": "coverage_repair_too_large",
						"cells": jobs.size(),
						"max_cells": max_cells,
						"layer_coverage_gaps": gaps,
					}
	if jobs.is_empty():
		return {
			"ok": false,
			"message": "no source background cells were available to copy into the coverage gap",
			"error_code": "coverage_repair_no_source_cells",
			"layer_coverage_gaps": gaps,
		}
	for job_value in jobs:
		var job: Dictionary = job_value
		if undo_manager != null:
			undo_manager.record_tile_cells(job["node"], [job["before"]], [job["after"]])
		else:
			_apply_map_cell(job["node"], job["after"])
	var remaining := validate_layer_coverage(input, editor_interface).get("layer_coverage_gaps", [])
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"repaired": true,
		"cells": jobs.size(),
		"layers": repaired_layers.keys(),
		"previous_layer_coverage_gaps": gaps,
		"layer_coverage_gaps": remaining,
	}


static func paint_terrain_connect(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	if target.get_class() == "GridMap":
		return {"ok": false, "message": "terrain_connect is only available for 2D TileMapLayer/TileMap targets", "error_code": "unsupported_map_type"}
	if not target.has_method("set_cells_terrain_connect"):
		return {"ok": false, "message": "Target does not support set_cells_terrain_connect", "error_code": "terrain_connect_unavailable"}
	var dimension := 2
	var map_layer := int(input.get("map_layer", 0))
	var allowed_bounds := _bounds_from_input(input, dimension)
	var coords_list := _terrain_coords_from_input(input)
	if coords_list.is_empty():
		return {"ok": false, "message": "terrain_connect requires cells or a positive width/height region", "error_code": "invalid_region"}
	if coords_list.size() > MAX_EDITED_CELLS:
		return {"ok": false, "message": "terrain_connect exceeds the cell safety limit", "error_code": "map_edit_too_large"}
	var before: Array = []
	for coords_2d in coords_list:
		var coords := Vector3i(coords_2d.x, coords_2d.y, 0)
		if not _cell_within_bounds(coords, allowed_bounds):
			return {
				"ok": false,
				"message": "terrain_connect would write outside allowed_bounds",
				"error_code": "map_edit_out_of_bounds",
				"coords": MapValidator.coord_payload(coords, dimension),
				"allowed_bounds": allowed_bounds,
			}
		before.append(_read_map_cell(target, coords, dimension, map_layer))
	var terrain_resource := _registry_entry_for_resource_input(input)
	var terrain_scene_path := str(terrain_resource.get("scene_path", "")).strip_edges()
	if terrain_scene_path != "" and not input.has("terrain_set") and not input.has("terrain"):
		return {
			"ok": false,
			"message": (
				"Resource '%s' is registered with scene_path '%s' (an object/PackedScene), not a terrain — " +
				"use place_map_objects instead of paint_terrain_connect."
			) % [str(terrain_resource.get("_resolved_resource", input.get("resource", input.get("resource_key", "")))), terrain_scene_path],
			"error_code": "resource_requires_object_placement",
			"resource": str(terrain_resource.get("_resolved_resource", "")),
			"scene_path": terrain_scene_path,
		}
	var terrain_set := int(input.get("terrain_set", terrain_resource.get("terrain_set", 0)))
	var terrain := int(input.get("terrain", terrain_resource.get("terrain", 0)))
	var ignore_empty := bool(input.get("ignore_empty_terrains", true))
	if target.get_class() == "TileMap":
		target.call("set_cells_terrain_connect", map_layer, coords_list, terrain_set, terrain, ignore_empty)
	else:
		target.call("set_cells_terrain_connect", coords_list, terrain_set, terrain, ignore_empty)
	var after: Array = []
	for coords_2d in coords_list:
		after.append(_read_map_cell(target, Vector3i(coords_2d.x, coords_2d.y, 0), dimension, map_layer))
	if undo_manager != null:
		undo_manager.record_tile_cells(target, before, after)
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"terrain_set": terrain_set,
		"terrain": terrain,
		"cells": after.size(),
		"message": "Terrain painted through set_cells_terrain_connect; edges were resolved by Godot TileSet terrain rules.",
	}


## 按地图 cell 坐标把 PackedScene 资源实例化到 ObjectLayer/PropsRoot 等对象层。
static func place_map_objects(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No scene is currently being edited", "error_code": "no_edited_scene"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var map_node: Node = target_result["node"]
	var dimension := 3 if map_node.get_class() == "GridMap" else 2
	var parent_result := _resolve_object_parent(input, root, map_node, dimension)
	if not bool(parent_result.get("ok", false)):
		return parent_result
	var parent: Node = parent_result["node"]
	var objects_value = input.get("objects", [])
	if not (objects_value is Array) or (objects_value as Array).is_empty():
		return {"ok": false, "message": "objects must be a non-empty array", "error_code": "invalid_objects"}
	if (objects_value as Array).size() > 128:
		return {"ok": false, "message": "at most 128 objects are allowed", "error_code": "map_object_batch_too_large"}
	var allowed_bounds := _bounds_from_input(input, dimension)
	var registry := _read_json_resource(RESOURCE_REGISTRY_PATH)
	var registry_data: Dictionary = registry.get("data", {}) if registry.get("data", {}) is Dictionary else {}
	var occupied := _object_occupancy_from_spatial_index(str(target_result.get("path", "")), dimension)
	var blocked_cells := _blocked_object_cells_from_spatial_index(str(target_result.get("path", "")), dimension)
	var planned := {}
	var placement_map_layer := map_layer_for_placement(input)
	var prepared: Array = []
	var relocated_objects: Array = []
	for object_index in range((objects_value as Array).size()):
		var object_value = (objects_value as Array)[object_index]
		if not (object_value is Dictionary):
			return _with_object_batch_failure(
				{"ok": false, "message": "each object must be an object", "error_code": "invalid_object"},
				object_index,
				{},
				placement_map_layer,
				{}
			)
		var object_spec: Dictionary = object_value
		var coords := Vector3i(
			int(object_spec.get("x", 0)),
			int(object_spec.get("y", 0)),
			int(object_spec.get("z", 0)) if dimension == 3 else 0
		)
		if not _cell_within_bounds(coords, allowed_bounds):
			return _with_object_batch_failure({
				"ok": false,
				"message": "object placement would write outside allowed_bounds",
				"error_code": "map_object_out_of_bounds",
				"coords": MapValidator.coord_payload(coords, dimension),
				"allowed_bounds": allowed_bounds,
			}, object_index, object_spec, placement_map_layer, {})
		var coord_key := MapValidator.coord_key(coords)
		if not bool(input.get("allow_overlap", false)) and (occupied.has(coord_key) or planned.has(coord_key)):
			return _with_object_batch_failure({
				"ok": false,
				"message": "object placement overlaps an existing or planned object",
				"error_code": "map_object_overlap",
				"coords": MapValidator.coord_payload(coords, dimension),
			}, object_index, object_spec, placement_map_layer, {})
		if not bool(input.get("allow_on_blocked", false)) and blocked_cells.has(coord_key):
			return _with_object_batch_failure({
				"ok": false,
				"message": "object placement is on a blocked/water/obstacle cell",
				"error_code": "map_object_blocked_cell",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_entry": blocked_cells[coord_key],
			}, object_index, object_spec, placement_map_layer, {})
		var resource_key := str(object_spec.get("resource", object_spec.get("resource_key", ""))).strip_edges()
		var resource_def: Dictionary = _registry_entry_with_fallback(registry_data, resource_key, str(object_spec.get("fallback_resource", "")))
		if resource_def.has("_resolved_resource"):
			resource_key = str(resource_def.get("_resolved_resource", resource_key))
		if not resource_def.is_empty():
			var object_contract_check := _validate_resource_contract_shape(resource_key, resource_def)
			if not bool(object_contract_check.get("ok", false)):
				return _with_object_batch_failure(object_contract_check, object_index, object_spec, placement_map_layer, {})
		var profile_fallback := input.duplicate(true)
		for key in resource_def.keys():
			if not profile_fallback.has(key):
				profile_fallback[key] = resource_def[key]
		if resource_def.has("required_cells") and not object_spec.has("required_cells"):
			object_spec["required_cells"] = int(resource_def.get("required_cells", 1))
		if resource_def.has("kind") and not object_spec.has("instance_kind"):
			object_spec["instance_kind"] = str(resource_def.get("kind", ""))
		var placement_profile := _placement_profile_from_spec(object_spec, profile_fallback)
		var placement_check := _validate_single_object_placement(
			map_node,
			str(target_result.get("path", "")),
			dimension,
			placement_map_layer,
			coords,
			placement_profile,
			occupied,
			blocked_cells,
			planned,
			input
		)
		if not bool(placement_check.get("ok", true)) \
				and str(object_spec.get("placement_kind", object_spec.get("kind", ""))).to_lower() == "coin" \
				and str(placement_check.get("error_code", "")) == "placement_cell_not_empty":
			for offset in range(1, 9):
				var candidate := coords + Vector3i(0, -offset, 0)
				var candidate_check := _validate_single_object_placement(
					map_node,
					str(target_result.get("path", "")),
					dimension,
					placement_map_layer,
					candidate,
					placement_profile,
					occupied,
					blocked_cells,
					planned,
					input
				)
				if bool(candidate_check.get("ok", false)):
					relocated_objects.append({
						"from": MapValidator.coord_payload(coords, dimension),
						"to": MapValidator.coord_payload(candidate, dimension),
					})
					coords = candidate
					coord_key = MapValidator.coord_key(coords)
					object_spec["x"] = coords.x
					object_spec["y"] = coords.y
					if dimension == 3:
						object_spec["z"] = coords.z
					placement_check = candidate_check
					break
		if not bool(placement_check.get("ok", true)):
			return _with_object_batch_failure(placement_check, object_index, object_spec, placement_map_layer, placement_profile)
		var scene_path := PathUtils.to_res_path(str(object_spec.get("scene_path", resource_def.get("scene_path", ""))))
		if scene_path == "" or not (scene_path.ends_with(".tscn") or scene_path.ends_with(".scn")):
			return _with_object_batch_failure({"ok": false, "message": "object requires a .tscn/.scn scene_path or resource registry entry", "error_code": "missing_scene_path", "resource": resource_key}, object_index, object_spec, placement_map_layer, placement_profile)
		if not FileAccess.file_exists(scene_path):
			return _with_object_batch_failure({"ok": false, "message": "scene file not found: " + scene_path, "error_code": "scene_not_found"}, object_index, object_spec, placement_map_layer, placement_profile)
		var packed = load(scene_path)
		if not (packed is PackedScene):
			return _with_object_batch_failure({"ok": false, "message": "Failed to load as PackedScene: " + scene_path, "error_code": "load_failed"}, object_index, object_spec, placement_map_layer, placement_profile)
		var instance := (packed as PackedScene).instantiate()
		if not (instance is Node):
			return _with_object_batch_failure({"ok": false, "message": "PackedScene did not instantiate a Node: " + scene_path, "error_code": "instantiate_failed"}, object_index, object_spec, placement_map_layer, placement_profile)
		var node: Node = instance
		if dimension == 2 and not (node is Node2D):
			return _with_object_batch_failure({"ok": false, "message": "2D map object must instantiate a Node2D scene: " + scene_path, "error_code": "object_type_mismatch"}, object_index, object_spec, placement_map_layer, placement_profile)
		if dimension == 3 and not (node is Node3D):
			return _with_object_batch_failure({"ok": false, "message": "3D map object must instantiate a Node3D scene: " + scene_path, "error_code": "object_type_mismatch"}, object_index, object_spec, placement_map_layer, placement_profile)
		node.name = _object_instance_name(object_spec, resource_key, scene_path)
		_apply_object_position(node, map_node, coords, dimension, parent)
		_apply_object_metadata(node, object_spec, resource_key, scene_path, coords, dimension)
		prepared.append({
			"node": node,
			"coords": coords,
			"scene_path": scene_path,
			"resource": resource_key,
			"spec": object_spec,
			"dimension": dimension,
			"map_layer": placement_map_layer,
			"visual_group_id": _visual_group_id_from_data(object_spec),
		})
		planned[coord_key] = true
	var parent_path_for_index := str(root.get_path_to(parent)) if parent != root else "."
	var index_result := _maybe_update_object_spatial_index(input, undo_manager, str(target_result.get("path", "")), parent_path_for_index, dimension, prepared)
	if not bool(index_result.get("ok", true)):
		return index_result
	var paths: Array = []
	for prepared_value in prepared:
		var item: Dictionary = prepared_value
		var node: Node = item["node"]
		parent.add_child(node)
		node.owner = root
		if undo_manager != null and undo_manager.has_method("record_node_added"):
			undo_manager.record_node_added(parent, node, root)
		paths.append(str(root.get_path_to(node)))
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"parent_path": str(root.get_path_to(parent)) if parent != root else ".",
		"dimension": dimension,
		"objects": prepared.size(),
		"relocated_objects": relocated_objects,
		"instance_summary": _summarize_object_instances(prepared),
		"paths": paths,
		"spatial_index": index_result,
	}


## 只读地查询一小块现有地图区域的真实瓦片/网格数据，外加地图节点自身的坐标系数。
## 用于在扩建/延伸地形前先弄清楚现有内容到底长什么样、世界坐标怎么换算，而不是
## 靠 tile_catalog 里"有哪些瓦片可用"自己瞎拼，或者假设 origin/tile_size 是常量。
static func find_placement_anchors(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := map_layer_for_placement(input)
	var region := MapValidator.region_from_input(input, dimension)
	if int(region["width"]) * int(region["height"]) * int(region["depth"]) > MAX_DESCRIBED_CELLS:
		return {
			"ok": false,
			"message": "anchor search region exceeds the %d-cell limit; search a smaller region" % MAX_DESCRIBED_CELLS,
			"error_code": "region_too_large",
		}
	var profile := _placement_profile_from_spec(input, input)
	var occupied := _object_occupancy_from_spatial_index(str(target_result.get("path", "")), dimension)
	var blocked_cells := _blocked_object_cells_from_spatial_index(str(target_result.get("path", "")), dimension)
	var protected_cells := _protected_cell_set(input, dimension)
	var candidate_result := _placement_candidate_cells(input, region, profile, dimension)
	if not bool(candidate_result.get("ok", false)):
		return candidate_result
	var anchors: Array = []
	var rejected := {
		"occupied_or_blocked": 0,
		"missing_support": 0,
		"not_empty": 0,
		"clearance": 0,
		"protected_path": 0,
	}
	for coords in candidate_result.get("candidates", []):
		var check := _validate_single_object_placement(
			target,
			str(target_result.get("path", "")),
			dimension,
			map_layer,
			coords,
			profile,
			occupied,
			blocked_cells,
			{},
			input
		)
		if bool(check.get("ok", false)):
			anchors.append({
				"coords": MapValidator.coord_payload(coords, dimension),
				"score": _score_placement_anchor(coords, profile, protected_cells, str(target_result.get("path", "")), input, dimension),
				"profile": profile.get("name", ""),
				"surface_source": candidate_result.get("source", "region"),
				"footprint_cells": check.get("footprint_cells", []),
				"support_cells": check.get("support_cells", []),
			})
		else:
			var reason := str(check.get("error_code", "rejected"))
			match reason:
				"placement_missing_support":
					rejected["missing_support"] = int(rejected["missing_support"]) + 1
				"placement_cell_not_empty":
					rejected["not_empty"] = int(rejected["not_empty"]) + 1
				"placement_clearance_blocked":
					rejected["clearance"] = int(rejected["clearance"]) + 1
				"placement_protected_cell":
					rejected["protected_path"] = int(rejected["protected_path"]) + 1
				_:
					rejected["occupied_or_blocked"] = int(rejected["occupied_or_blocked"]) + 1
	anchors.sort_custom(func(a, b): return float(a.get("score", 0.0)) > float(b.get("score", 0.0)))
	var max_results := max(1, int(input.get("max_results", 32)))
	if anchors.size() > max_results:
		anchors = anchors.slice(0, max_results)
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"region": region,
		"profile": profile,
		"candidate_source": candidate_result.get("source", "region"),
		"anchors": anchors,
		"rejected_summary": rejected,
	}


static func validate_object_placements(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := map_layer_for_placement(input)
	var objects_value = input.get("objects", [])
	if not (objects_value is Array) or (objects_value as Array).is_empty():
		return {"ok": false, "message": "objects must be a non-empty array", "error_code": "invalid_objects"}
	var occupied := _object_occupancy_from_spatial_index(str(target_result.get("path", "")), dimension)
	var blocked_cells := _blocked_object_cells_from_spatial_index(str(target_result.get("path", "")), dimension)
	var planned := {}
	var issues: Array = []
	var placements: Array = []
	for object_value in objects_value:
		if not (object_value is Dictionary):
			issues.append({"error_code": "invalid_object", "message": "each object must be an object"})
			continue
		var object_spec: Dictionary = object_value
		var coords := MapValidator.coord_from_input(object_spec, dimension)
		var profile := _placement_profile_from_spec(object_spec, input)
		var check := _validate_single_object_placement(
			target,
			str(target_result.get("path", "")),
			dimension,
			map_layer,
			coords,
			profile,
			occupied,
			blocked_cells,
			planned,
			input
		)
		var entry := {
			"coords": MapValidator.coord_payload(coords, dimension),
			"profile": profile.get("name", ""),
			"passed": bool(check.get("ok", false)),
		}
		if bool(check.get("ok", false)):
			entry["footprint_cells"] = check.get("footprint_cells", [])
			entry["support_cells"] = check.get("support_cells", [])
			planned[MapValidator.coord_key(coords)] = true
		else:
			entry["issue"] = check
			issues.append(entry)
		placements.append(entry)
	var repair_plan: Array = []
	for issue_value in issues:
		var issue: Dictionary = issue_value
		var issue_coords = issue.get("coords", {})
		if issue_coords is Dictionary:
			repair_plan.append({
				"type": "object_relocate",
				"action": "find_placement_anchors",
				"from": issue_coords,
				"profile": issue.get("profile", ""),
				"note": "Search nearby legal anchors with the same placement profile, then call place_map_objects on the chosen anchor.",
			})
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"passed": issues.is_empty(),
		"placements": placements,
		"issues": issues,
		"repair_plan": repair_plan,
	}


static func repair_placements(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No scene is currently being edited", "error_code": "no_edited_scene"}
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := map_layer_for_placement(input)
	var region := MapValidator.region_from_input(input, dimension)
	var target_path := str(target_result.get("path", ""))
	var entries := _repairable_placement_entries(input, target_path, region, dimension)
	if entries.is_empty():
		return {"ok": true, "changed": false, "message": "No indexed objects matched the repair request", "moved": []}
	var before_text := _read_text_file(SPATIAL_INDEX_PATH)
	var parsed = JSON.parse_string(before_text) if before_text != "" else {}
	var index: Dictionary = parsed if parsed is Dictionary else {}
	var occupied := _occupied_object_cells_for_repair(target_path, dimension)
	var blocked := _blocked_object_cells_from_spatial_index(target_path, dimension)
	var moved: Array = []
	var plans: Array = []
	for entry_value in entries:
		var entry: Dictionary = entry_value
		var old_coords := MapValidator.coord_from_input(entry.get("coords", {}), dimension)
		var old_key := MapValidator.coord_key(old_coords)
		var profile_input := input.duplicate(true)
		profile_input["ignore_object_coords"] = [MapValidator.coord_payload(old_coords, dimension)]
		if not profile_input.has("placement_kind") and not profile_input.has("kind"):
			profile_input["placement_kind"] = _placement_kind_from_entry(entry)
		var profile := _placement_profile_from_spec(profile_input, profile_input)
		occupied.erase(old_key)
		var current_check := _validate_single_object_placement(target, target_path, dimension, map_layer, old_coords, profile, occupied, blocked, {}, profile_input)
		if bool(current_check.get("ok", false)):
			occupied[old_key] = true
			continue
		var anchor_result := _best_placement_anchor(target, target_path, region, dimension, map_layer, profile, occupied, blocked, profile_input)
		if not bool(anchor_result.get("ok", false)):
			occupied[old_key] = true
			plans.append({"entry": entry, "issue": current_check, "repair": anchor_result})
			continue
		var new_coords := MapValidator.coord_from_input(anchor_result.get("coords", {}), dimension)
		var node := _resolve_indexed_object_node(root, entry)
		if node == null:
			occupied[old_key] = true
			plans.append({"entry": entry, "issue": current_check, "suggested_anchor": anchor_result, "reason": "indexed object has no resolvable node"})
			continue
		var before_position = null
		if node is Node3D:
			before_position = (node as Node3D).position
		elif node is Node2D:
			before_position = (node as Node2D).position
		_apply_object_position(node, target, new_coords, dimension)
		var after_position = null
		if node is Node3D:
			after_position = (node as Node3D).position
		elif node is Node2D:
			after_position = (node as Node2D).position
		if undo_manager != null and before_position != null and after_position != null and undo_manager.has_method("record_node_property"):
			undo_manager.record_node_property(node, "position", before_position, after_position)
		_update_object_entry_coords(index, entry, old_coords, new_coords, dimension)
		occupied[MapValidator.coord_key(new_coords)] = true
		moved.append({
			"node": str(root.get_path_to(node)),
			"from": MapValidator.coord_payload(old_coords, dimension),
			"to": MapValidator.coord_payload(new_coords, dimension),
			"score": anchor_result.get("score", 0.0),
			"issue": current_check.get("error_code", ""),
		})
	var after_text := JSON.stringify(index, "\t")
	var write_result := _write_json_file(SPATIAL_INDEX_PATH, before_text, after_text, undo_manager)
	if not bool(write_result.get("ok", false)):
		return write_result
	return {
		"ok": true,
		"changed": not moved.is_empty(),
		"target": target_path,
		"moved": moved,
		"unrepaired": plans,
		"spatial_index": {"ok": true, "updated": true, "path": SPATIAL_INDEX_PATH},
	}


## 把一块超过单次读取上限的区域切成若干 ≤max_cells 的对齐子区域。每块边长取 max_cells 的
## 维度方根量级（2D 方块 / 3D 立方块），保证 w*h*d ≤ max_cells，拼起来正好覆盖原区域无重叠。
static func _split_region(origin: Vector3i, width: int, height: int, depth: int, dimension: int, max_cells: int) -> Array:
	var step := maxi(1, int(floor(pow(float(max_cells), 1.0 / float(dimension)))))
	var regions: Array = []
	var z := 0
	while z < depth:
		var dz := mini(step, depth - z) if dimension == 3 else 1
		var y := 0
		while y < height:
			var dy := mini(step, height - y)
			var x := 0
			while x < width:
				var dx := mini(step, width - x)
				var entry := {"x": origin.x + x, "y": origin.y + y, "width": dx, "height": dy}
				if dimension == 3:
					entry["z"] = origin.z + z
					entry["depth"] = dz
				regions.append(entry)
				x += dx
			y += dy
		z += dz
	return regions


static func describe_map_region(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]

	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := int(input.get("map_layer", 0))
	var origin := Vector3i(
		int(input.get("x", 0)),
		int(input.get("y", 0)),
		int(input.get("z", 0)) if dimension == 3 else 0
	)
	var width := max(1, int(input.get("width", 1)))
	var height := max(1, int(input.get("height", 1)))
	var depth := max(1, int(input.get("depth", 1))) if dimension == 3 else 1
	var cells_format := str(input.get("cells_format", "summary_only")).to_lower()
	if not ["summary_only", "non_empty_only", "full"].has(cells_format):
		return {"ok": false, "message": "cells_format must be summary_only, non_empty_only, or full", "error_code": "invalid_cells_format"}
	var max_returned_cells := max(1, int(input.get("max_returned_cells", DEFAULT_DESCRIBE_RETURNED_CELLS)))
	var requested_cells: int = width * height * depth
	if requested_cells > MAX_AUTOSERVED_DESCRIBED_CELLS:
		# 超出自动整片返回的上限：回退成切好的 suggested_regions，模型照着逐块读即可，不用自己推拆分。
		return {
			"ok": false,
			"message": "requested region has %d cells, over the %d-cell auto-serve limit; issue these smaller queries instead (already split for you)." % [requested_cells, MAX_AUTOSERVED_DESCRIBED_CELLS],
			"error_code": "region_too_large",
			"cells": requested_cells,
			"max_cells": MAX_AUTOSERVED_DESCRIBED_CELLS,
			"suggested_regions": _split_region(origin, width, height, depth, dimension, MAX_DESCRIBED_CELLS),
		}

	var cells: Array = []
	var returned_cells: Array = []
	var non_empty_count := 0
	for z_offset in range(depth):
		for y_offset in range(height):
			for x_offset in range(width):
				var coords := origin + Vector3i(x_offset, y_offset, z_offset)
				var described := _describe_safe_cell(_read_map_cell(target, coords, dimension, map_layer), dimension)
				cells.append(described)
				var is_non_empty := _described_cell_is_non_empty(described, dimension)
				if is_non_empty:
					non_empty_count += 1
				if cells_format == "full":
					if returned_cells.size() < max_returned_cells:
						returned_cells.append(described)
				elif cells_format == "non_empty_only" and is_non_empty:
					if returned_cells.size() < max_returned_cells:
						returned_cells.append(described)

	var result := {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"cells_format": cells_format,
		"cells_total": requested_cells,
		"cells_returned": returned_cells.size(),
		"cells_omitted": max(0, requested_cells - returned_cells.size()) if cells_format == "full" else max(0, non_empty_count - returned_cells.size()),
		"non_empty_count": non_empty_count,
		"atlas_summary": _atlas_summary(cells, dimension),
	}
	if cells_format != "summary_only":
		result["cells"] = returned_cells
	if requested_cells > MAX_DESCRIBED_CELLS:
		# 这次区域大于单块上限，但工具已自动整片读完返回；告知模型不必再自己分块查询。
		result["auto_served"] = true
	if target is Node2D:
		var position_2d := (target as Node2D).position
		result["node_position"] = {"x": position_2d.x, "y": position_2d.y}
	elif target is Node3D:
		var position_3d := (target as Node3D).position
		result["node_position"] = {"x": position_3d.x, "y": position_3d.y, "z": position_3d.z}
	if dimension == 3 and "cell_size" in target:
		var cell_size: Vector3 = target.get("cell_size")
		result["cell_size"] = {"x": cell_size.x, "y": cell_size.y, "z": cell_size.z}
	elif dimension == 2 and "tile_set" in target and target.get("tile_set") != null:
		var tile_set = target.get("tile_set")
		var tile_size: Vector2i = tile_set.tile_size
		result["tile_size"] = {"x": tile_size.x, "y": tile_size.y}
	if target.get_class() == "TileMap":
		result["layers"] = _describe_tilemap_layers(target)
	return result


## 瓦片/网格坐标(cell) ↔ 世界坐标(world) 互转。直接走 Godot 原生 map_to_local/local_to_map
## + to_global/to_local，而不是让模型自己用 node_position + tile_size 手算——手算公式不处理瓦片
## 偏移/半格/等距投影，且容易让 map-agent 陷入对坐标系的反复推理循环（一次任务里曾空转 70+ 秒、
## 8000+ 字符）。传 `cells`（[{x,y[,z]}]）返回对应 `world`；传 `world` 返回对应 `cells`；可同时传。
## GridMap 用三维，TileMapLayer/legacy TileMap 用二维。
static func convert_map_coords(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var world_out: Array = []
	for raw in (input.get("cells", []) if input.get("cells", []) is Array else []):
		world_out.append(_cell_to_world(target, raw, dimension))
	var cells_out: Array = []
	for raw in (input.get("world", []) if input.get("world", []) is Array else []):
		cells_out.append(_world_to_cell(target, raw, dimension))
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"world": world_out,   # 与输入 cells 顺序一一对应
		"cells": cells_out,   # 与输入 world 顺序一一对应
	}


static func _cell_to_world(target: Node, raw: Variant, dimension: int) -> Dictionary:
	var d: Dictionary = raw if raw is Dictionary else {}
	if dimension == 3:
		var cell := Vector3i(int(d.get("x", 0)), int(d.get("y", 0)), int(d.get("z", 0)))
		var local: Vector3 = target.call("map_to_local", cell)
		var world: Vector3 = (target as Node3D).to_global(local)
		return {"x": world.x, "y": world.y, "z": world.z}
	var cell2 := Vector2i(int(d.get("x", 0)), int(d.get("y", 0)))
	var local2: Vector2 = target.call("map_to_local", cell2)
	var world2: Vector2 = (target as Node2D).to_global(local2)
	return {"x": world2.x, "y": world2.y}


static func _world_to_cell(target: Node, raw: Variant, dimension: int) -> Dictionary:
	var d: Dictionary = raw if raw is Dictionary else {}
	if dimension == 3:
		var world := Vector3(float(d.get("x", 0.0)), float(d.get("y", 0.0)), float(d.get("z", 0.0)))
		var local: Vector3 = (target as Node3D).to_local(world)
		var cell: Vector3i = target.call("local_to_map", local)
		return {"x": cell.x, "y": cell.y, "z": cell.z}
	var world2 := Vector2(float(d.get("x", 0.0)), float(d.get("y", 0.0)))
	var local2: Vector2 = (target as Node2D).to_local(world2)
	var cell2: Vector2i = target.call("local_to_map", local2)
	return {"x": cell2.x, "y": cell2.y}


## 一个 legacy TileMap 节点可能同时挂多个图层（比如 "Background"/"Mid"），
## 各图层互相独立、互不遮挡判定；不能假设 map_layer=0 就是承载碰撞的前景层。
## 调用方应该看这份列表自己选对 map_layer，而不是不传 map_layer 时悄悄默认成 0。
static func _describe_tilemap_layers(target: Node) -> Array:
	var layers: Array = []
	var count: int = target.get_layers_count()
	for layer_index in range(count):
		var used_cells: Array = target.call("get_used_cells", layer_index)
		layers.append({
			"index": layer_index,
			"name": str(target.get_layer_name(layer_index)),
			"enabled": bool(target.is_layer_enabled(layer_index)),
			"cell_count": used_cells.size(),
			# 这一层瓦片实际铺到哪——背景/天空这类"毯式"图层扩图时要不要跟着扩，
			# 直接看这个范围跟其它层比是不是已经跟不上，不用再去猜。
			"used_bounds": _used_bounds_2d(used_cells),
		})
	return layers


## 将图层信息格式化为 LLM 友好的文本，便于快速理解并选择正确的图层
static func _format_layers_for_llm(layers: Array) -> String:
	var lines: Array[String] = []
	for layer in layers:
		var index: int = layer.get("index", 0)
		var name: String = str(layer.get("name", ""))
		var enabled: bool = bool(layer.get("enabled", true))
		var cell_count: int = int(layer.get("cell_count", 0))
		var bounds: Dictionary = layer.get("used_bounds", {})

		var status := "enabled" if enabled else "disabled"
		var bounds_str := ""
		if bounds.is_empty():
			bounds_str = "empty (no tiles)"
		else:
			bounds_str = "x=%d..%d, y=%d..%d" % [
				int(bounds.get("min_x", 0)),
				int(bounds.get("max_x", 0)),
				int(bounds.get("min_y", 0)),
				int(bounds.get("max_y", 0))
			]

		lines.append("  - map_layer=%d: name='%s', status=%s, cells=%d, bounds=%s" % [
			index, name, status, cell_count, bounds_str
		])

	return "\n".join(lines)


static func _suggest_foreground_layer(layers: Array) -> Dictionary:
	var best: Dictionary = {}
	var best_cells := -1
	for layer in layers:
		var name := str(layer.get("name", "")).to_lower()
		var cells := int(layer.get("cell_count", 0))
		if cells <= 0 or name.find("background") >= 0 or name.find("bg") >= 0:
			continue
		if cells > best_cells:
			best = layer
			best_cells = cells
	return best


## 把 `_read_map_cell` 里的 Vector2i/Vector3i 折算成 JSON 可序列化的 `{x,y[,z]}`。
static func _describe_safe_cell(cell: Dictionary, dimension: int) -> Dictionary:
	var safe := cell.duplicate()
	var coords: Vector3i = safe.get("coords", Vector3i.ZERO)
	safe["coords"] = {"x": coords.x, "y": coords.y, "z": coords.z} if dimension == 3 else {"x": coords.x, "y": coords.y}
	if safe.has("atlas_coords"):
		var atlas: Vector2i = safe["atlas_coords"]
		safe["atlas_coords"] = {"x": atlas.x, "y": atlas.y}
	return safe


static func _described_cell_is_non_empty(cell: Dictionary, dimension: int) -> bool:
	if dimension == 3:
		return int(cell.get("item", -1)) != -1
	return int(cell.get("source_id", -1)) != -1


## 写入/校验类工具传 require_explicit_map_layer=true：legacy TileMap 常见同时挂多个互不遮挡的
## 图层（比如背景 "Background" 是 layer 0，真正承载碰撞的地面在另一层），不传 map_layer 时
## 这里曾经悄悄默认成 0，写错层/校验错层不会有任何报错信号。现在改成在工具层直接拒绝，
## 而不是只在 prompt 里提醒模型"自己记得选对层"——错误信息本身就在决策当下把 layers 列表和
## 下一步动作（先调用 describe_map_region 确认层，再带着确认过的 map_layer 重试）一起带出来。
static func _resolve_map_target(
	input: Dictionary,
	editor_interface: EditorInterface,
	require_explicit_map_layer: bool = false
) -> Dictionary:
	var result := _resolve_map_target_unchecked(input, editor_interface)
	if not require_explicit_map_layer or not bool(result.get("ok", false)):
		return result
	var node: Node = result["node"]
	var has_explicit_layer := input.has("map_layer") or input.has("ground_map_layer")
	if node.get_class() != "TileMap" or has_explicit_layer:
		return result
	var layer_count: int = node.get_layers_count()
	if layer_count <= 1:
		return result

	# TileMap 有多层时，必须让 LLM 明确选择图层，不能自动推断——
	# 背景/天空和主地形往往在不同层，自动选错会导致静默写错层。
	var layers := _describe_tilemap_layers(node)
	var suggested := _suggest_foreground_layer(layers)
	var suggestion_text := ""
	if not suggested.is_empty():
		suggestion_text = "\nLikely foreground/collision layer for platform validation/editing: map_layer=%d (name='%s')." % [
			int(suggested.get("index", 0)),
			str(suggested.get("name", "")),
		]
	return {
		"ok": false,
		"message": (
			"Target TileMap '%s' has %d layers and no map_layer was given. " +
			"You must explicitly specify map_layer in your next call. " +
			"Available layers:\n%s\n" +
			"Retry the same operation with map_layer set to the index of the layer you want to edit.%s"
		) % [str(result.get("path", "")), layer_count, _format_layers_for_llm(layers), suggestion_text],
		"error_code": "map_layer_required_for_multilayer_tilemap",
		"target": str(result.get("path", "")),
		"layers": layers,
		"suggested_map_layer": int(suggested.get("index", -1)),
	}


static func _resolve_map_target_unchecked(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No scene is currently being edited", "error_code": "no_edited_scene"}
	var requested_path := str(input.get("target_path", "")).strip_edges()
	if requested_path != "":
		requested_path = _normalize_map_target_path(root, requested_path)
		var requested := root if requested_path == "." else root.get_node_or_null(NodePath(requested_path))
		if requested == null:
			# 精确路径找不到时，尝试按节点名在所有地图节点中模糊匹配
			var fuzzy := _fuzzy_match_map_node(root, requested_path)
			if fuzzy != null:
				var fuzzy_path := str(root.get_path_to(fuzzy["node"]))
				return {"ok": true, "node": fuzzy["node"], "path": fuzzy_path, "fuzzy_matched": true, "original": requested_path}
			var candidates := _map_node_paths(root)
			# 如果路径看起来像类名而非节点路径，给出更明确的提示
			var hint := "Use a path from describe_map_context.maps[].path; do not use class names like 'TileMapLayer' as target_path."
			if requested_path in ["TileMapLayer", "TileMap", "GridMap"]:
				hint = ("target_path must be a node path (e.g. 'Level/Ground'), not a class name. " +
					"Call describe_map_context first to get the correct path from maps[].path. " +
					"Available map nodes: " + str(candidates))
			return {
				"ok": false,
				"message": "Map node was not found: " + requested_path,
				"error_code": "map_not_found",
				"candidates": candidates,
				"hint": hint,
			}
		if not _is_map_node(requested):
			return {
				"ok": false,
				"message": "Target must be a TileMapLayer, TileMap, or GridMap",
				"error_code": "unsupported_map_type"
			}
		return {"ok": true, "node": requested, "path": requested_path}
	for selected in editor_interface.get_selection().get_selected_nodes():
		if selected != null and _is_map_node(selected):
			return {"ok": true, "node": selected, "path": str(root.get_path_to(selected)), "selected": true}
	var found: Array = []
	_collect_map_nodes(root, found)
	if found.size() == 1:
		return {"ok": true, "node": found[0], "path": str(root.get_path_to(found[0])), "auto_detected": true}
	var candidates: Array[String] = []
	for node in found:
		candidates.append(str(root.get_path_to(node)))
	return {
		"ok": false,
		"message": "Select a map node or provide target_path" if found.is_empty() else "Multiple map nodes found; provide target_path",
		"error_code": "map_target_required",
		"candidates": candidates
	}


static func _is_map_node(node: Node) -> bool:
	return node.get_class() in ["TileMapLayer", "TileMap", "GridMap"]


static func _normalize_map_target_path(root: Node, requested_path: String) -> String:
	var root_prefix := str(root.name) + "/"
	if requested_path.begins_with(root_prefix):
		return requested_path.substr(root_prefix.length())
	return requested_path


static func _map_node_paths(root: Node) -> Array[String]:
	var found: Array = []
	_collect_map_nodes(root, found)
	var candidates: Array[String] = []
	for node in found:
		candidates.append(str(root.get_path_to(node)))
	return candidates


static func _collect_map_nodes(node: Node, out: Array) -> void:
	if _is_map_node(node):
		out.append(node)
	for child in node.get_children():
		_collect_map_nodes(child, out)


## 在所有地图节点中按名称做模糊匹配，返回第一个命中的节点或 null。
## 优先精确匹配节点名，其次忽略大小写，最后匹配子串。
## 只有一个地图节点时直接返回，避免 LLM 不知道路径时也能工作。
static func _fuzzy_match_map_node(root: Node, query: String) -> Variant:
	var found: Array = []
	_collect_map_nodes(root, found)
	if found.is_empty():
		return null
	var query_lower := query.to_lower()
	# 第一轮：精确匹配节点名
	for node in found:
		if str(node.name) == query:
			return {"node": node}
	# 第二轮：精确匹配节点名（忽略大小写）
	for node in found:
		if str(node.name).to_lower() == query_lower:
			return {"node": node}
	# 第三轮：如果只有一个地图节点，直接返回
	if found.size() == 1:
		return {"node": found[0]}
	# 第四轮：节点名包含查询字符串
	for node in found:
		if str(node.name).to_lower().find(query_lower) != -1:
			return {"node": node}
	return null


static func _build_map_operation(
	target: Node,
	dimension: int,
	map_layer: int,
	operation: Dictionary,
	pending_cells: Dictionary
) -> Dictionary:
	var action := str(operation.get("action", ""))
	if action not in ["fill", "erase", "copy"]:
		return {"ok": false, "message": "Unsupported map action: " + action, "error_code": "invalid_map_action"}
	var width := int(operation.get("width", 1))
	var height := int(operation.get("height", 1))
	var depth := int(operation.get("depth", 1)) if dimension == 3 else 1
	if width <= 0 or height <= 0 or depth <= 0:
		return {"ok": false, "message": "width, height, and depth must be positive", "error_code": "invalid_map_region"}
	if width * height * depth > MAX_EDITED_CELLS:
		return {"ok": false, "message": "operation exceeds the cell safety limit", "error_code": "map_edit_too_large"}

	var cells: Array = []
	if action == "copy":
		var source_origin := Vector3i(
			int(operation.get("from_x", 0)),
			int(operation.get("from_y", 0)),
			int(operation.get("from_z", 0)) if dimension == 3 else 0
		)
		var destination_origin := Vector3i(
			int(operation.get("to_x", operation.get("x", 0))),
			int(operation.get("to_y", operation.get("y", 0))),
			int(operation.get("to_z", operation.get("z", 0))) if dimension == 3 else 0
		)
		# Snapshot all source cells before this copy writes. Earlier operations in the same
		# tool call are visible through pending_cells, while overlap within this copy is safe.
		var source_snapshot: Array = []
		for z_offset in range(depth):
			for y_offset in range(height):
				for x_offset in range(width):
					var offset := Vector3i(x_offset, y_offset, z_offset)
					var source_coords := source_origin + offset
					var source_key := _cell_key({"coords": source_coords}, dimension, map_layer)
					var source_cell: Dictionary
					if pending_cells.has(source_key):
						source_cell = pending_cells[source_key].duplicate(true)
					else:
						source_cell = _read_map_cell(target, source_coords, dimension, map_layer)
					source_snapshot.append({"cell": source_cell, "offset": offset})
		for snapshot_value in source_snapshot:
			var snapshot: Dictionary = snapshot_value
			var source_cell: Dictionary = snapshot["cell"]
			var offset: Vector3i = snapshot["offset"]
			source_cell["coords"] = destination_origin + offset
			_copy_operation_metadata(operation, source_cell)
			cells.append(source_cell)
		return {"ok": true, "cells": cells}

	var origin := Vector3i(
		int(operation.get("x", 0)),
		int(operation.get("y", 0)),
		int(operation.get("z", 0)) if dimension == 3 else 0
	)
	for z_offset in range(depth):
		for y_offset in range(height):
			for x_offset in range(width):
				var cell := {"coords": origin + Vector3i(x_offset, y_offset, z_offset)}
				if dimension == 3:
					cell["item"] = -1 if action == "erase" else int(operation.get("item", -1))
					cell["orientation"] = int(operation.get("orientation", 0))
				else:
					cell["map_layer"] = map_layer
					cell["source_id"] = -1 if action == "erase" else int(operation.get("source_id", -1))
					cell["atlas_coords"] = Vector2i(
						int(operation.get("atlas_x", -1)),
						int(operation.get("atlas_y", -1))
					)
					cell["alternative_tile"] = int(operation.get("alternative_tile", 0))
				_copy_operation_metadata(operation, cell)
				cells.append(cell)
	return {"ok": true, "cells": cells}


static func _copy_operation_metadata(operation: Dictionary, cell: Dictionary) -> void:
	for key in [
		"resource", "resource_key", "semantic_layer", "tags", "cost",
		"visual_group_id", "instance_id", "instance_kind", "required_cells",
	]:
		if operation.has(key):
			cell[key] = operation[key]


static func _apply_registry_fallback_to_operation(operation: Dictionary, dimension: int) -> Dictionary:
	var resource_entry := _registry_entry_for_resource_input(operation)
	if resource_entry.is_empty():
		return resource_entry
	if resource_entry.has("_resolved_resource") and not operation.has("resource"):
		operation["resource"] = str(resource_entry.get("_resolved_resource", ""))
	if resource_entry.has("kind") and not operation.has("instance_kind"):
		operation["instance_kind"] = str(resource_entry.get("kind", ""))
	if resource_entry.has("required_cells") and not operation.has("required_cells"):
		operation["required_cells"] = int(resource_entry.get("required_cells", 1))
	if dimension == 3:
		if not operation.has("item") and resource_entry.has("item"):
			operation["item"] = int(resource_entry.get("item", -1))
		elif not operation.has("item") and resource_entry.has("mesh_library_item"):
			operation["item"] = int(resource_entry.get("mesh_library_item", -1))
	else:
		if not operation.has("source_id") and resource_entry.has("source_id"):
			operation["source_id"] = int(resource_entry.get("source_id", -1))
		if not operation.has("atlas_x") and resource_entry.has("atlas_x"):
			operation["atlas_x"] = int(resource_entry.get("atlas_x", -1))
		if not operation.has("atlas_y") and resource_entry.has("atlas_y"):
			operation["atlas_y"] = int(resource_entry.get("atlas_y", -1))
		var atlas = resource_entry.get("atlas_coords", null)
		if atlas is Dictionary:
			if not operation.has("atlas_x"):
				operation["atlas_x"] = int((atlas as Dictionary).get("x", -1))
			if not operation.has("atlas_y"):
				operation["atlas_y"] = int((atlas as Dictionary).get("y", -1))
	return resource_entry


static func _validate_operation_resource_contract(operation: Dictionary, resource_entry: Dictionary, dimension: int) -> Dictionary:
	var action := str(operation.get("action", "fill"))
	if action == "erase" or action == "copy":
		return {"ok": true}
	var resource_key := str(operation.get("resource", operation.get("resource_key", ""))).strip_edges()
	if resource_key == "":
		if dimension == 3 and operation.has("item") and int(operation.get("item", -1)) >= 0:
			return {"ok": false, "message": "raw GridMap item ids are not allowed in edit_map fill; register the verified resource first and use resource/resource_key.", "error_code": "unregistered_map_resource"}
		if dimension == 2 and operation.has("source_id") and operation.has("atlas_x") and operation.has("atlas_y") and int(operation.get("source_id", -1)) >= 0:
			return {"ok": false, "message": "raw TileSet atlas ids are not allowed in edit_map fill; register the verified resource first and use resource/resource_key.", "error_code": "unregistered_map_resource"}
		return {"ok": true}
	if resource_entry.is_empty():
		var registry := _read_json_resource(RESOURCE_REGISTRY_PATH)
		var registry_data: Dictionary = registry.get("data", {}) if registry.get("data", {}) is Dictionary else {}
		var available_resources: Array = registry_data.keys()
		available_resources.sort()
		return {
			"ok": false,
			"message": "resource '%s' is not registered; do not retry edit_map. Read verified map data, call write_resource_registry, then use one of the registered resource keys." % resource_key,
			"error_code": "unregistered_map_resource",
			"resource": resource_key,
			"available_resources": available_resources,
			"hint": "Never invent semantic resource keys or atlas coordinates. Use describe_map_context/describe_map_region and write_resource_registry first.",
		}
	var shape_check := _validate_resource_contract_shape(resource_key, resource_entry)
	if not bool(shape_check.get("ok", false)):
		return shape_check
	if int(resource_entry.get("required_cells", 1)) > 1 and _visual_group_id_from_data(operation) == "":
		return {
			"ok": false,
			"message": "resource '%s' requires visual_group_id/instance_id because required_cells=%d" % [resource_key, int(resource_entry.get("required_cells", 1))],
			"error_code": "missing_visual_group_id",
			"resource": resource_key,
		}
	if dimension == 3:
		if not resource_entry.has("item") and not resource_entry.has("mesh_library_item"):
			return {"ok": false, "message": "registered resource '%s' has no item/mesh_library_item" % resource_key, "error_code": "invalid_resource_contract", "resource": resource_key}
		return {"ok": true}
	if not resource_entry.has("source_id"):
		return {"ok": false, "message": "registered resource '%s' has no source_id" % resource_key, "error_code": "invalid_resource_contract", "resource": resource_key}
	if _registry_2d_tile_signature(resource_entry).is_empty():
		return {"ok": false, "message": "registered resource '%s' has no atlas_coords/atlas_x/atlas_y" % resource_key, "error_code": "invalid_resource_contract", "resource": resource_key}
	return {"ok": true}


static func _validate_ground_fill_reference(
	target: Node,
	dimension: int,
	map_layer: int,
	operation: Dictionary,
	resource_entry: Dictionary
) -> Dictionary:
	# 已有前景地面时，扩建必须锚定一个同 atlas 的真实格子，避免把错误 registry
	# 条目（例如桥梁）误当成 ground 后连续 fill 到整段路线。
	if dimension != 2 or str(operation.get("action", "")) != "fill":
		return {"ok": true}
	var tags: Array = resource_entry.get("tags", []) if resource_entry.get("tags", []) is Array else []
	if not tags.has("ground"):
		return {"ok": true}
	var used_cells: Array = target.call("get_used_cells", map_layer) if target.get_class() == "TileMap" else target.call("get_used_cells")
	if used_cells.is_empty():
		return {"ok": true}
	var reference_value = operation.get("reference_cell", null)
	if not (reference_value is Dictionary):
		return {
			"ok": false,
			"message": "ground fill requires reference_cell from a real existing ground tile; read describe_map_region with cells_format=non_empty_only first.",
			"error_code": "ground_reference_required",
			"hint": "Set reference_cell to the x/y of an existing foreground ground cell whose source_id/atlas_coords match this fill.",
		}
	var reference: Dictionary = reference_value
	var reference_cell := _read_map_cell(
		target, Vector3i(int(reference.get("x", 0)), int(reference.get("y", 0)), 0), dimension, map_layer
	)
	var reference_atlas: Vector2i = reference_cell.get("atlas_coords", Vector2i(-1, -1))
	if int(reference_cell.get("source_id", -1)) != int(operation.get("source_id", -1)) \
			or reference_atlas != Vector2i(int(operation.get("atlas_x", -1)), int(operation.get("atlas_y", -1))):
		return {
			"ok": false,
			"message": "ground fill atlas does not match reference_cell; do not use a registry label as proof of terrain semantics.",
			"error_code": "ground_reference_mismatch",
			"reference_cell": {"x": int(reference.get("x", 0)), "y": int(reference.get("y", 0))},
			"reference_signature": {"source_id": int(reference_cell.get("source_id", -1)), "atlas_x": reference_atlas.x, "atlas_y": reference_atlas.y},
			"requested_signature": {"source_id": int(operation.get("source_id", -1)), "atlas_x": int(operation.get("atlas_x", -1)), "atlas_y": int(operation.get("atlas_y", -1))},
			"hint": "Use the exact source_id/atlas coordinates returned for the real ground reference cell, then retry the small batch.",
		}
	return {"ok": true}


static func _validate_resource_contract_shape(resource_key: String, resource_entry: Dictionary) -> Dictionary:
	if not resource_entry.has("kind") or str(resource_entry.get("kind", "")).strip_edges() == "":
		return {"ok": false, "message": "registered resource '%s' must declare kind; rewrite it with write_resource_registry from verified map data" % resource_key, "error_code": "invalid_resource_contract", "resource": resource_key}
	if not (resource_entry.get("footprint", null) is Dictionary):
		return {"ok": false, "message": "registered resource '%s' must declare footprint; rewrite it with write_resource_registry from verified map data" % resource_key, "error_code": "invalid_resource_contract", "resource": resource_key}
	if not resource_entry.has("required_cells"):
		return {"ok": false, "message": "registered resource '%s' must declare required_cells; rewrite it with write_resource_registry from verified map data" % resource_key, "error_code": "invalid_resource_contract", "resource": resource_key}
	return {"ok": true}


static func _registry_entry_for_resource_input(input: Dictionary) -> Dictionary:
	var registry := _read_json_resource(RESOURCE_REGISTRY_PATH)
	var registry_data: Dictionary = registry.get("data", {}) if registry.get("data", {}) is Dictionary else {}
	var resource_key := str(input.get("resource", input.get("resource_key", ""))).strip_edges()
	var fallback_key := str(input.get("fallback_resource", input.get("fallback_resource_key", ""))).strip_edges()
	var resolved := _registry_entry_with_fallback(registry_data, resource_key, fallback_key)
	if not resolved.is_empty() or resource_key != "":
		return resolved
	return _registry_entry_for_raw_2d_tile(registry_data, input)


static func _registry_entry_for_raw_2d_tile(registry_data: Dictionary, input: Dictionary) -> Dictionary:
	if not input.has("source_id") or not input.has("atlas_x") or not input.has("atlas_y"):
		return {}
	var wanted := {
		"source_id": int(input.get("source_id", -1)),
		"atlas_x": int(input.get("atlas_x", -1)),
		"atlas_y": int(input.get("atlas_y", -1)),
	}
	if wanted["source_id"] < 0 or wanted["atlas_x"] < 0 or wanted["atlas_y"] < 0:
		return {}
	for key in registry_data.keys():
		var value = registry_data.get(key, {})
		if not (value is Dictionary):
			continue
		var entry: Dictionary = value
		if not bool(_validate_resource_contract_shape(str(key), entry).get("ok", false)):
			continue
		if _registry_2d_tile_signature(entry) != wanted:
			continue
		var resolved := entry.duplicate(true)
		resolved["_resolved_resource"] = str(key)
		resolved["_fallback_for"] = "raw_atlas"
		return resolved
	return {}


static func _registry_entry_with_fallback(registry_data: Dictionary, resource_key: String, fallback_key: String) -> Dictionary:
	if resource_key != "" and registry_data.get(resource_key, {}) is Dictionary:
		var primary: Dictionary = (registry_data.get(resource_key, {}) as Dictionary).duplicate(true)
		if bool(_validate_resource_contract_shape(resource_key, primary).get("ok", false)):
			primary["_resolved_resource"] = resource_key
			return primary
		# Older registries may leave a semantic alias without a contract. Prefer the
		# verified *_real entry instead of making every edit fail on that stale alias.
		var real_key := resource_key + "_real"
		if registry_data.get(real_key, {}) is Dictionary:
			var real_entry: Dictionary = (registry_data.get(real_key, {}) as Dictionary).duplicate(true)
			if bool(_validate_resource_contract_shape(real_key, real_entry).get("ok", false)):
				real_entry["_resolved_resource"] = real_key
				real_entry["_fallback_for"] = resource_key
				return real_entry
	if fallback_key != "" and registry_data.get(fallback_key, {}) is Dictionary:
		var fallback: Dictionary = (registry_data.get(fallback_key, {}) as Dictionary).duplicate(true)
		if bool(_validate_resource_contract_shape(fallback_key, fallback).get("ok", false)):
			fallback["_resolved_resource"] = fallback_key
			fallback["_fallback_for"] = resource_key
			return fallback
	return {}


static func _read_map_cell(target: Node, coords: Vector3i, dimension: int, map_layer: int) -> Dictionary:
	if dimension == 3:
		return {
			"coords": coords,
			"item": int(target.call("get_cell_item", coords)),
			"orientation": int(target.call("get_cell_item_orientation", coords))
		}
	var coords_2d := Vector2i(coords.x, coords.y)
	var source_id: int
	var atlas_coords: Vector2i
	var alternative_tile: int
	if target.get_class() == "TileMap":
		source_id = int(target.call("get_cell_source_id", map_layer, coords_2d))
		atlas_coords = target.call("get_cell_atlas_coords", map_layer, coords_2d)
		alternative_tile = int(target.call("get_cell_alternative_tile", map_layer, coords_2d))
	else:
		source_id = int(target.call("get_cell_source_id", coords_2d))
		atlas_coords = target.call("get_cell_atlas_coords", coords_2d)
		alternative_tile = int(target.call("get_cell_alternative_tile", coords_2d))
	return {
		"coords": coords,
		"map_layer": map_layer,
		"source_id": source_id,
		"atlas_coords": atlas_coords,
		"alternative_tile": alternative_tile
	}


static func _describe_map_node(root: Node, node: Node) -> Dictionary:
	var path := str(root.get_path_to(node))
	var result := {
		"path": path,
		"type": node.get_class(),
		"dimension": 3 if node.get_class() == "GridMap" else 2,
		"name": node.name,
	}
	if node.get_class() == "TileMap":
		result["layers"] = _describe_tilemap_layers(node)
		var layer_counts: Array = []
		for layer in result["layers"]:
			var layer_index := int(layer.get("index", 0))
			var used_cells: Array = node.call("get_used_cells", layer_index)
			layer_counts.append({"index": layer_index, "used_cells": used_cells.size(), "used_bounds": _used_bounds_2d(used_cells)})
		result["layer_cell_counts"] = layer_counts
	elif node.get_class() == "GridMap":
		var used_cells_3d: Array = node.call("get_used_cells")
		result["used_cells"] = used_cells_3d.size()
		result["used_bounds"] = _used_bounds_3d(used_cells_3d)
		if "mesh_library" in node and node.get("mesh_library") != null:
			var mesh_library = node.get("mesh_library")
			result["mesh_library"] = mesh_library.resource_path
		if "cell_size" in node:
			var cell_size: Vector3 = node.get("cell_size")
			result["cell_size"] = {"x": cell_size.x, "y": cell_size.y, "z": cell_size.z}
	else:
		var used_cells_2d: Array = node.call("get_used_cells") if node.has_method("get_used_cells") else []
		result["used_cells"] = used_cells_2d.size()
		result["used_bounds"] = _used_bounds_2d(used_cells_2d)
		if "tile_set" in node and node.get("tile_set") != null:
			var tile_set = node.get("tile_set")
			result["tile_set"] = tile_set.resource_path
			var tile_size: Vector2i = tile_set.tile_size
			result["tile_size"] = {"x": tile_size.x, "y": tile_size.y}
	return result


static func _read_json_resource(path: String) -> Dictionary:
	var absolute := ProjectSettings.globalize_path(path)
	if not FileAccess.file_exists(absolute):
		return {"exists": false, "path": path, "data": {}}
	var text := FileAccess.get_file_as_string(absolute)
	var parsed = JSON.parse_string(text)
	if not (parsed is Dictionary):
		return {"exists": true, "path": path, "data": {}, "warning": "JSON root is not an object"}
	return {"exists": true, "path": path, "data": parsed}


static func _used_bounds_2d(cells: Array) -> Dictionary:
	if cells.is_empty():
		return {}
	var min_x := 2147483647
	var min_y := 2147483647
	var max_x := -2147483648
	var max_y := -2147483648
	for value in cells:
		var coords: Vector2i = value
		min_x = min(min_x, coords.x)
		min_y = min(min_y, coords.y)
		max_x = max(max_x, coords.x)
		max_y = max(max_y, coords.y)
	return {"x": min_x, "y": min_y, "width": max_x - min_x + 1, "height": max_y - min_y + 1, "min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y}


static func _used_bounds_3d(cells: Array) -> Dictionary:
	if cells.is_empty():
		return {}
	var min_x := 2147483647
	var min_y := 2147483647
	var min_z := 2147483647
	var max_x := -2147483648
	var max_y := -2147483648
	var max_z := -2147483648
	for value in cells:
		var coords: Vector3i = value
		min_x = min(min_x, coords.x)
		min_y = min(min_y, coords.y)
		min_z = min(min_z, coords.z)
		max_x = max(max_x, coords.x)
		max_y = max(max_y, coords.y)
		max_z = max(max_z, coords.z)
	return {
		"x": min_x, "y": min_y, "z": min_z,
		"width": max_x - min_x + 1, "height": max_y - min_y + 1, "depth": max_z - min_z + 1,
		"min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y, "min_z": min_z, "max_z": max_z,
	}


static func _count_index_entries(index_branch) -> int:
	if not (index_branch is Dictionary):
		return 0
	var total := 0
	for target_path in (index_branch as Dictionary).keys():
		var entries = index_branch[target_path]
		if entries is Dictionary:
			total += entries.size()
	return total


static func _prune_spatial_index_to_cap(index: Dictionary, cap: int) -> Dictionary:
	var total := _count_index_entries(index.get("2d", {})) + _count_index_entries(index.get("3d", {}))
	if total <= cap:
		return {"removed": 0}
	var removed := 0
	for branch_key in ["2d", "3d"]:
		var branch = index.get(branch_key, {})
		if not (branch is Dictionary):
			continue
		for target_path in (branch as Dictionary).keys():
			var entries = branch[target_path]
			if not (entries is Dictionary):
				continue
			var keys := (entries as Dictionary).keys()
			for key in keys:
				if total <= cap:
					return {"removed": removed}
				(entries as Dictionary).erase(key)
				total -= 1
				removed += 1
			if (entries as Dictionary).is_empty():
				(branch as Dictionary).erase(target_path)
	return {"removed": removed}


static func _maybe_update_spatial_index(
	input: Dictionary,
	undo_manager: Node,
	target: Node,
	target_path: String,
	dimension: int,
	after_cells: Array
) -> Dictionary:
	if not bool(input.get("update_spatial_index", false)):
		return {"ok": true, "updated": false}
	var absolute := ProjectSettings.globalize_path(SPATIAL_INDEX_PATH)
	var before_exists := FileAccess.file_exists(absolute)
	var before_text := FileAccess.get_file_as_string(absolute) if before_exists else ""
	var parsed = JSON.parse_string(before_text) if before_text != "" else {}
	var index: Dictionary = parsed if parsed is Dictionary else {}
	var branch_key := "3d" if dimension == 3 else "2d"
	if not index.has(branch_key) or not (index[branch_key] is Dictionary):
		index[branch_key] = {}
	if not index[branch_key].has(target_path) or not (index[branch_key][target_path] is Dictionary):
		index[branch_key][target_path] = {}
	var target_index: Dictionary = index[branch_key][target_path]
	# 当前索引总条目数（两个维度分支合计），用于到顶后只更新/删除、不再新增坐标。
	var total_entries := _count_index_entries(index.get("2d", {})) + _count_index_entries(index.get("3d", {}))
	var added := 0
	var removed := 0
	var hit_cap := false
	var registry_warnings := _spatial_registry_warnings(after_cells, dimension)
	for value in after_cells:
		if not (value is Dictionary):
			continue
		var cell: Dictionary = value
		var key := _spatial_tile_index_key(cell, dimension)
		var legacy_key := _index_coord_key(cell.get("coords", Vector3i.ZERO), dimension)
		var existing_key := ""
		if target_index.has(key):
			existing_key = key
		elif target_index.has(legacy_key):
			existing_key = legacy_key
		if _is_empty_cell(target, cell):
			if existing_key != "":
				target_index.erase(existing_key)
				total_entries -= 1
				removed += 1
			if legacy_key != key and target_index.has(legacy_key):
				target_index.erase(legacy_key)
				total_entries -= 1
				removed += 1
		elif existing_key != "":
			# 原地更新已有坐标，不增加体量。
			target_index[key] = _describe_safe_cell(cell, dimension)
			if existing_key != key:
				target_index.erase(existing_key)
			elif legacy_key != key and target_index.has(legacy_key):
				target_index.erase(legacy_key)
				total_entries -= 1
				removed += 1
		elif total_entries >= MAX_SPATIAL_INDEX_ENTRIES:
			# 已到上限，拒绝再写入新坐标，避免文件无限膨胀。
			hit_cap = true
		else:
			target_index[key] = _describe_safe_cell(cell, dimension)
			total_entries += 1
			added += 1
	var after_text := JSON.stringify(index, "\t")
	if undo_manager != null and undo_manager.has_method("record_file_write"):
		var error: Error = undo_manager.record_file_write(SPATIAL_INDEX_PATH, before_text, after_text, before_exists)
		if error != OK:
			return {"ok": false, "message": "failed to write spatial index", "error_code": "spatial_index_write_failed", "error": error}
	else:
		var dir_error := DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(MAP_DATA_DIR))
		if dir_error != OK:
			return {"ok": false, "message": "failed to create spatial index directory", "error_code": "spatial_index_write_failed", "error": dir_error}
		var file := FileAccess.open(absolute, FileAccess.WRITE)
		if file == null:
			return {"ok": false, "message": "failed to open spatial index", "error_code": "spatial_index_write_failed", "error": FileAccess.get_open_error()}
		file.store_string(after_text)
	var result := {
		"ok": true,
		"updated": true,
		"path": SPATIAL_INDEX_PATH,
		"cells": after_cells.size(),
		"added": added,
		"removed": removed,
		"total_entries": total_entries,
	}
	if hit_cap:
		result["warning"] = "spatial index reached the %d-entry cap; new coordinates were skipped — clear it or stop passing update_spatial_index" % MAX_SPATIAL_INDEX_ENTRIES
	if not registry_warnings.is_empty():
		result["warnings"] = registry_warnings
	return result


static func _spatial_registry_warnings(after_cells: Array, dimension: int) -> Array:
	if dimension != 2:
		return []
	var registry := _read_json_resource(RESOURCE_REGISTRY_PATH)
	var registry_data: Dictionary = registry.get("data", {}) if registry.get("data", {}) is Dictionary else {}
	if registry_data.is_empty():
		return []
	var warnings: Array = []
	var seen := {}
	for value in after_cells:
		if not (value is Dictionary):
			continue
		var cell: Dictionary = value
		if not cell.has("atlas_coords"):
			continue
		var resource_key := str(cell.get("resource", cell.get("resource_key", ""))).strip_edges()
		if resource_key == "" or seen.has(resource_key):
			continue
		var entry = registry_data.get(resource_key, {})
		if not (entry is Dictionary):
			continue
		var expected := _registry_2d_tile_signature(entry)
		if expected.is_empty():
			continue
		var atlas: Vector2i = cell["atlas_coords"]
		var actual := {"source_id": int(cell.get("source_id", -1)), "atlas_x": atlas.x, "atlas_y": atlas.y}
		if int(expected.get("source_id", actual["source_id"])) == actual["source_id"] \
				and int(expected.get("atlas_x", actual["atlas_x"])) == actual["atlas_x"] \
				and int(expected.get("atlas_y", actual["atlas_y"])) == actual["atlas_y"]:
			continue
		seen[resource_key] = true
		warnings.append({
			"type": "resource_registry_mismatch",
			"resource": resource_key,
			"registry": expected,
			"actual": actual,
			"hint": "真实写入以 edit_map/describe_map_region 的 source_id+atlas_coords 为准；resource_registry/spatial_index 语义只作提示。",
		})
	return warnings


static func _visual_group_id_from_data(data: Dictionary) -> String:
	var visual_group_id := str(data.get("visual_group_id", "")).strip_edges()
	if visual_group_id != "":
		return visual_group_id
	return str(data.get("instance_id", "")).strip_edges()


static func _summarize_visual_groups_from_cells(cells: Array) -> Dictionary:
	var by_id := {}
	for value in cells:
		if not (value is Dictionary):
			continue
		var cell: Dictionary = value
		var group_id := _visual_group_id_from_data(cell)
		if group_id == "":
			continue
		if not by_id.has(group_id):
			by_id[group_id] = {
				"id": group_id,
				"kind": str(cell.get("instance_kind", "")),
				"cells": 0,
				"required_cells": int(cell.get("required_cells", 0)),
			}
		var group: Dictionary = by_id[group_id]
		group["cells"] = int(group.get("cells", 0)) + 1
		group["required_cells"] = max(int(group.get("required_cells", 0)), int(cell.get("required_cells", 0)))
	var groups: Array = []
	var incomplete: Array = []
	for group_id in by_id.keys():
		var group: Dictionary = by_id[group_id]
		groups.append(group)
		var required_cells := int(group.get("required_cells", 0))
		if required_cells > 0 and int(group.get("cells", 0)) < required_cells:
			incomplete.append(group)
	return {"count": groups.size(), "groups": groups, "incomplete": incomplete}


static func _registry_2d_tile_signature(entry: Dictionary) -> Dictionary:
	var atlas_value = entry.get("atlas_coords", {})
	var atlas_x = entry.get("atlas_x", null)
	var atlas_y = entry.get("atlas_y", null)
	if atlas_value is Dictionary:
		atlas_x = (atlas_value as Dictionary).get("x", atlas_x)
		atlas_y = (atlas_value as Dictionary).get("y", atlas_y)
	if atlas_x == null or atlas_y == null:
		return {}
	return {
		"source_id": int(entry.get("source_id", 0)),
		"atlas_x": int(atlas_x),
		"atlas_y": int(atlas_y),
	}


static func _index_coord_key(coords: Vector3i, dimension: int) -> String:
	return "%d,%d,%d" % [coords.x, coords.y, coords.z] if dimension == 3 else "%d,%d" % [coords.x, coords.y]


static func _index_layer_coord_key(coords: Vector3i, dimension: int, map_layer: int) -> String:
	if dimension == 3:
		return _index_coord_key(coords, dimension)
	return "layer:%d:%s" % [map_layer, _index_coord_key(coords, dimension)]


static func _spatial_tile_index_key(cell: Dictionary, dimension: int) -> String:
	return _index_layer_coord_key(cell.get("coords", Vector3i.ZERO), dimension, int(cell.get("map_layer", 0)))


static func _is_empty_cell(target: Node, cell: Dictionary) -> bool:
	if target.get_class() == "GridMap":
		return int(cell.get("item", -1)) == -1
	return int(cell.get("source_id", -1)) == -1


static func _atlas_summary(cells: Array, dimension: int) -> Array:
	if dimension != 2:
		return []
	var counts := {}
	for value in cells:
		if not (value is Dictionary):
			continue
		var cell: Dictionary = value
		if int(cell.get("source_id", -1)) == -1:
			continue
		var atlas = cell.get("atlas_coords", {})
		if not (atlas is Dictionary):
			continue
		var key := "%d:%d,%d" % [
			int(cell.get("source_id", -1)),
			int((atlas as Dictionary).get("x", -1)),
			int((atlas as Dictionary).get("y", -1)),
		]
		counts[key] = int(counts.get(key, 0)) + 1
	var summary: Array = []
	for key in counts.keys():
		var parts := str(key).split(":")
		var atlas_parts := str(parts[1]).split(",") if parts.size() > 1 else PackedStringArray(["-1", "-1"])
		summary.append({
			"source_id": int(parts[0]),
			"atlas_coords": {"x": int(atlas_parts[0]), "y": int(atlas_parts[1])},
			"count": int(counts[key]),
		})
	summary.sort_custom(func(a, b): return int(a.get("count", 0)) > int(b.get("count", 0)))
	return summary


static func _spatial_entry_stale_for_cell(entry: Dictionary, cell: Dictionary, dimension: int) -> Dictionary:
	if dimension != 2:
		return {"checked": false}
	if not entry.has("source_id") or not entry.has("atlas_coords"):
		return {"checked": false}
	var expected := _entry_2d_tile_signature(entry)
	var actual := _cell_2d_tile_signature(cell)
	if expected.is_empty() or actual.is_empty():
		return {"checked": false}
	var stale := int(expected.get("source_id", -1)) != int(actual.get("source_id", -1)) \
			or int(expected.get("atlas_x", -1)) != int(actual.get("atlas_x", -1)) \
			or int(expected.get("atlas_y", -1)) != int(actual.get("atlas_y", -1))
	return {
		"checked": true,
		"stale": stale,
		"index": expected,
		"actual": actual,
	}


static func _spatial_entry_stale_for_index_hit(
	editor_interface: EditorInterface,
	target_cache: Dictionary,
	target_path: String,
	dimension: int,
	map_layer: int,
	entry: Dictionary
) -> Dictionary:
	if editor_interface == null or dimension != 2:
		return {"checked": false}
	if not (entry.get("coords", {}) is Dictionary):
		return {"checked": false}
	map_layer = int(entry.get("map_layer", map_layer))
	var cache_key := "%s:%d" % [target_path, map_layer]
	var target_result: Dictionary
	if target_cache.has(cache_key):
		target_result = target_cache[cache_key]
	else:
		target_result = _resolve_map_target({"target_path": target_path, "map_layer": map_layer}, editor_interface)
		target_cache[cache_key] = target_result
	if not bool(target_result.get("ok", false)):
		return {"checked": false, "target_error": target_result}
	var coords := MapValidator.coord_from_input(entry.get("coords", {}), dimension)
	var actual_cell := _read_map_cell(target_result["node"], coords, dimension, map_layer)
	return _spatial_entry_stale_for_cell(entry, actual_cell, dimension)


static func _entry_2d_tile_signature(entry: Dictionary) -> Dictionary:
	var atlas = entry.get("atlas_coords", {})
	if not (atlas is Dictionary):
		return {}
	return {
		"source_id": int(entry.get("source_id", -1)),
		"atlas_x": int((atlas as Dictionary).get("x", -1)),
		"atlas_y": int((atlas as Dictionary).get("y", -1)),
	}


static func _cell_2d_tile_signature(cell: Dictionary) -> Dictionary:
	var atlas = cell.get("atlas_coords", {})
	if atlas is Vector2i:
		return {
			"source_id": int(cell.get("source_id", -1)),
			"atlas_x": (atlas as Vector2i).x,
			"atlas_y": (atlas as Vector2i).y,
		}
	if atlas is Dictionary:
		return {
			"source_id": int(cell.get("source_id", -1)),
			"atlas_x": int((atlas as Dictionary).get("x", -1)),
			"atlas_y": int((atlas as Dictionary).get("y", -1)),
		}
	return {}


static func _apply_spatial_metadata_to_cell(target_path: String, coords: Vector3i, dimension: int, cell: Dictionary) -> void:
	var parsed := _read_json_resource(SPATIAL_INDEX_PATH)
	if not bool(parsed.get("exists", false)):
		return
	var data = parsed.get("data", {})
	if not (data is Dictionary):
		return
	var branch = (data as Dictionary).get("3d" if dimension == 3 else "2d", {})
	if not (branch is Dictionary):
		return
	var target_entries = (branch as Dictionary).get(target_path, {})
	if not (target_entries is Dictionary):
		return
	var layer_key := _index_layer_coord_key(coords, dimension, int(cell.get("map_layer", 0)))
	var entry = (target_entries as Dictionary).get(layer_key, {})
	if not (entry is Dictionary):
		entry = (target_entries as Dictionary).get(_index_coord_key(coords, dimension), {})
	if not (entry is Dictionary):
		return
	var stale := _spatial_entry_stale_for_cell(entry, cell, dimension)
	if bool(stale.get("checked", false)) and bool(stale.get("stale", false)):
		cell["_spatial_index_stale"] = true
		cell["_spatial_index_stale_detail"] = stale
		return
	for key in ["resource", "resource_key", "semantic_layer", "tags", "cost"]:
		if (entry as Dictionary).has(key):
			cell[key] = entry[key]


static func _collect_filled_cells(target: Node, region: Dictionary, dimension: int, map_layer: int) -> Dictionary:
	var filled := {}
	for dz in range(int(region["depth"])):
		for dy in range(int(region["height"])):
			for dx in range(int(region["width"])):
				var coords := Vector3i(
					int(region["x"]) + dx,
					int(region["y"]) + dy,
					int(region["z"]) + dz
				)
				var cell := _read_map_cell(target, coords, dimension, map_layer)
				if not _is_empty_cell(target, cell):
					filled[MapValidator.coord_key(coords)] = true
	return filled


static func _repair_cells_from_plan(
	input: Dictionary,
	target: Node,
	dimension: int,
	map_layer: int,
	repair_plan: Array
) -> Dictionary:
	var cells: Array = []
	for plan_value in repair_plan:
		if not (plan_value is Dictionary):
			continue
		var plan: Dictionary = plan_value
		var action := str(plan.get("action", "erase"))
		var plan_cells = plan.get("cells", [])
		if not (plan_cells is Array):
			continue
		for coord_value in plan_cells:
			var coords := MapValidator.coord_from_input(coord_value, dimension)
			var cell := {"coords": coords}
			if action == "fill":
				var filled := _filled_repair_cell(input, dimension, map_layer, coords)
				if not bool(filled.get("ok", false)):
					return filled
				cell = filled["cell"]
			elif target.get_class() == "GridMap":
				cell["item"] = -1
				cell["orientation"] = 0
			else:
				cell["map_layer"] = map_layer
				cell["source_id"] = -1
				cell["atlas_coords"] = Vector2i(-1, -1)
				cell["alternative_tile"] = 0
			cells.append(cell)
	return {"ok": true, "cells": cells}


static func _filled_repair_cell(input: Dictionary, dimension: int, map_layer: int, coords: Vector3i) -> Dictionary:
	var cell := {"coords": coords}
	if dimension == 3:
		if not input.has("item") and not input.has("fill_item"):
			return {"ok": false, "message": "fill repair requires item/fill_item for GridMap", "error_code": "missing_repair_resource"}
		cell["item"] = int(input.get("fill_item", input.get("item", -1)))
		cell["orientation"] = int(input.get("orientation", 0))
	else:
		if not input.has("source_id") and not input.has("fill_source_id"):
			return {"ok": false, "message": "fill repair requires source_id/fill_source_id for TileMap", "error_code": "missing_repair_resource"}
		cell["map_layer"] = map_layer
		cell["source_id"] = int(input.get("fill_source_id", input.get("source_id", -1)))
		cell["atlas_coords"] = Vector2i(
			int(input.get("fill_atlas_x", input.get("atlas_x", -1))),
			int(input.get("fill_atlas_y", input.get("atlas_y", -1)))
		)
		cell["alternative_tile"] = int(input.get("alternative_tile", 0))
	_copy_operation_metadata(input, cell)
	return {"ok": true, "cell": cell}


static func _cell_key(cell: Dictionary, dimension: int, map_layer: int) -> String:
	var coords: Vector3i = cell["coords"]
	return "%d:%d:%d:%d" % [map_layer if dimension == 2 else 0, coords.x, coords.y, coords.z]


static func _apply_map_cell(target: Node, cell: Dictionary) -> void:
	var coords: Vector3i = cell.get("coords", Vector3i.ZERO)
	if target.get_class() == "GridMap":
		target.call("set_cell_item", coords, int(cell.get("item", -1)), int(cell.get("orientation", 0)))
	elif target.get_class() == "TileMap":
		target.call(
			"set_cell",
			int(cell.get("map_layer", 0)),
			Vector2i(coords.x, coords.y),
			int(cell.get("source_id", -1)),
			cell.get("atlas_coords", Vector2i(-1, -1)),
			int(cell.get("alternative_tile", 0))
		)
	else:
		target.call(
			"set_cell",
			Vector2i(coords.x, coords.y),
			int(cell.get("source_id", -1)),
			cell.get("atlas_coords", Vector2i(-1, -1)),
			int(cell.get("alternative_tile", 0))
		)


static func _terrain_coords_from_input(input: Dictionary) -> Array:
	var coords_list: Array = []
	var cells_value = input.get("cells", [])
	if cells_value is Array and not (cells_value as Array).is_empty():
		for cell_value in cells_value:
			if cell_value is Dictionary:
				coords_list.append(Vector2i(int(cell_value.get("x", 0)), int(cell_value.get("y", 0))))
		return coords_list
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var width := max(0, int(input.get("width", 0)))
	var height := max(0, int(input.get("height", 0)))
	for dy in range(height):
		for dx in range(width):
			coords_list.append(Vector2i(x + dx, y + dy))
	return coords_list


static func _resolve_object_parent(input: Dictionary, root: Node, map_node: Node, dimension: int) -> Dictionary:
	var parent_path := str(input.get("parent_path", "")).strip_edges()
	if parent_path != "":
		var explicit: Node = root if parent_path == "." else root.get_node_or_null(NodePath(parent_path))
		if explicit == null:
			return {"ok": false, "message": "Object parent not found: " + parent_path, "error_code": "object_parent_not_found"}
		return {"ok": true, "node": explicit}
	var wanted_name := "PropsRoot" if dimension == 3 else "ObjectLayer"
	var parent := map_node.get_parent()
	if parent != null:
		var sibling := parent.get_node_or_null(NodePath(wanted_name))
		if sibling != null:
			return {"ok": true, "node": sibling}
	var found := _find_first_node_named(root, wanted_name)
	if found != null:
		return {"ok": true, "node": found}
	if dimension == 2 and map_node.get_parent() is Node2D:
		return {"ok": true, "node": map_node.get_parent(), "fallback_parent": true}
	if dimension == 3 and map_node.get_parent() is Node3D:
		return {"ok": true, "node": map_node.get_parent(), "fallback_parent": true}
	return {
		"ok": false,
		"message": "No object parent found; call ensure_standard_map_layers first or pass parent_path",
		"error_code": "object_parent_required",
		"expected_name": wanted_name,
	}


static func _find_first_node_named(node: Node, wanted_name: String) -> Node:
	if node.name == wanted_name:
		return node
	for child in node.get_children():
		var found := _find_first_node_named(child, wanted_name)
		if found != null:
			return found
	return null


static func _object_instance_name(object_spec: Dictionary, resource_key: String, scene_path: String) -> String:
	var explicit := str(object_spec.get("name", "")).strip_edges()
	if explicit != "":
		return explicit
	if resource_key != "":
		return resource_key.capitalize().replace(" ", "")
	return scene_path.get_file().get_basename()


static func _apply_object_position(node: Node, map_node: Node, coords: Vector3i, dimension: int, parent: Node = null) -> void:
	var target_parent := parent if parent != null else node.get_parent()
	if dimension == 3 and node is Node3D:
		if map_node.has_method("map_to_local") and target_parent is Node3D:
			var local_3d: Vector3 = map_node.call("map_to_local", coords)
			var world_3d: Vector3 = (map_node as Node3D).to_global(local_3d)
			(node as Node3D).position = (target_parent as Node3D).to_local(world_3d)
		elif "cell_size" in map_node:
			var cell_size: Vector3 = map_node.get("cell_size")
			var base_3d := (map_node as Node3D).position if map_node is Node3D else Vector3.ZERO
			(node as Node3D).position = base_3d + Vector3(coords.x * cell_size.x, coords.y * cell_size.y, coords.z * cell_size.z)
	elif dimension == 2 and node is Node2D:
		if map_node.has_method("map_to_local") and target_parent is Node2D:
			var local_2d: Vector2 = map_node.call("map_to_local", Vector2i(coords.x, coords.y))
			var world_2d: Vector2 = (map_node as Node2D).to_global(local_2d)
			(node as Node2D).position = (target_parent as Node2D).to_local(world_2d)
		else:
			var tile_size := Vector2i.ONE
			if "tile_set" in map_node and map_node.get("tile_set") != null:
				tile_size = map_node.get("tile_set").tile_size
			var base_2d := (map_node as Node2D).position if map_node is Node2D else Vector2.ZERO
			(node as Node2D).position = base_2d + Vector2(coords.x * tile_size.x, coords.y * tile_size.y)


static func _apply_object_metadata(
	node: Node,
	object_spec: Dictionary,
	resource_key: String,
	scene_path: String,
	coords: Vector3i,
	dimension: int
) -> void:
	node.set_meta("map_agent_scene_path", scene_path)
	node.set_meta("map_agent_resource", resource_key)
	node.set_meta("map_agent_coords", MapValidator.coord_payload(coords, dimension))
	if object_spec.has("semantic_layer"):
		node.set_meta("map_agent_semantic_layer", str(object_spec.get("semantic_layer", "")))
	if object_spec.has("tags"):
		node.set_meta("map_agent_tags", object_spec.get("tags", []))
	for key in ["visual_group_id", "instance_id", "instance_kind"]:
		if object_spec.has(key):
			node.set_meta("map_agent_" + key, str(object_spec.get(key, "")))


static func _summarize_object_instances(prepared: Array) -> Dictionary:
	var instances: Array = []
	for item_value in prepared:
		if not (item_value is Dictionary):
			continue
		var item: Dictionary = item_value
		var spec: Dictionary = item.get("spec", {})
		var group_id := str(item.get("visual_group_id", ""))
		var node_name := ""
		if item.get("node", null) is Node:
			var node: Node = item.get("node")
			node_name = node.name
		instances.append({
			"id": group_id if group_id != "" else node_name,
			"kind": str(spec.get("instance_kind", spec.get("semantic_layer", "object"))),
			"resource": str(item.get("resource", "")),
			"scene_path": str(item.get("scene_path", "")),
			"coords": MapValidator.coord_payload(item.get("coords", Vector3i.ZERO), int(item.get("dimension", 2))),
		})
	return {
		"requested_instances": prepared.size(),
		"written_instances": prepared.size(),
		"failed_instances": [],
		"instances": instances,
	}


static func map_layer_for_placement(input: Dictionary) -> int:
	return int(input.get("map_layer", input.get("ground_map_layer", 0)))


static func _placement_profile_from_spec(spec: Dictionary, fallback: Dictionary) -> Dictionary:
	var kind := ""
	for key in ["placement_kind", "kind", "resource", "resource_key", "scene_path"]:
		if spec.has(key) and str(spec.get(key, "")).strip_edges() != "":
			kind = str(spec.get(key, "")).strip_edges()
			break
	if kind == "":
		for key in ["placement_kind", "kind", "resource", "resource_key", "scene_path"]:
			if fallback.has(key) and str(fallback.get(key, "")).strip_edges() != "":
				kind = str(fallback.get(key, "")).strip_edges()
				break
	kind = _placement_kind_token(kind)
	var profile := {
		"name": kind if kind != "" else "generic",
		"anchor": "bottom_center",
		"surface_type": "ground",
		"footprint_width": 1,
		"footprint_height": 1,
		"requires_support": true,
		"support_mode": "bottom",
		"support_layers": [],
		"forbidden_layers": ["water", "hazard", "blocked", "obstacle"],
		"clearance_left": 0,
		"clearance_right": 0,
		"clearance_up": 0,
		"clearance_down": 0,
		"clearance_front": 0,
		"clearance_back": 0,
		"avoid_protected_cells": true,
		"requires_reachable": false,
		"reachability_point": "anchor",
		"interaction_offset": {"x": 0, "y": 0, "z": 0},
		"entrance_offset": {"x": 0, "y": 0, "z": 0},
	}
	match kind:
		"tree", "rock", "bush", "decor":
			profile["footprint_width"] = 1
			profile["footprint_height"] = 3 if kind == "tree" else 1
			profile["clearance_left"] = 1 if kind == "tree" else 0
			profile["clearance_right"] = 1 if kind == "tree" else 0
		"building", "house", "hut":
			profile["footprint_width"] = 4
			profile["footprint_height"] = 4
			profile["clearance_left"] = 1
			profile["clearance_right"] = 1
			profile["clearance_up"] = 1
		"npc", "enemy":
			profile["footprint_width"] = 1
			profile["footprint_height"] = 2
			profile["clearance_left"] = 1
			profile["clearance_right"] = 1
		"chest", "pickup", "save_point":
			profile["footprint_width"] = 1
			profile["footprint_height"] = 1
			profile["clearance_left"] = 1
			profile["clearance_right"] = 1
		"coin", "flying", "air":
			profile["requires_support"] = false
			profile["surface_type"] = "air"
			profile["footprint_width"] = 1
			profile["footprint_height"] = 1
	var footprint_value = spec.get("footprint", fallback.get("footprint", {}))
	if footprint_value is Dictionary:
		var footprint: Dictionary = footprint_value
		profile["footprint_width"] = max(1, int(footprint.get("width", profile["footprint_width"])))
		profile["footprint_height"] = max(1, int(footprint.get("height", profile["footprint_height"])))
		if footprint.has("depth"):
			profile["footprint_depth"] = max(1, int(footprint.get("depth", 1)))
	profile["anchor"] = str(spec.get("anchor", fallback.get("anchor", profile["anchor"]))).to_lower()
	profile["surface_type"] = str(spec.get("surface_type", fallback.get("surface_type", profile["surface_type"]))).to_lower()
	if profile["surface_type"] in ["water", "water_surface"]:
		profile["support_layers"] = ["water"]
		profile["requires_support"] = true
	if profile["surface_type"] == "wall":
		profile["support_mode"] = "wall"
	profile["footprint_width"] = max(1, int(spec.get("footprint_width", fallback.get("footprint_width", profile["footprint_width"]))))
	profile["footprint_height"] = max(1, int(spec.get("footprint_height", fallback.get("footprint_height", profile["footprint_height"]))))
	profile["requires_support"] = bool(spec.get("requires_support", fallback.get("requires_support", profile["requires_support"])))
	profile["support_mode"] = str(spec.get("support_mode", fallback.get("support_mode", profile["support_mode"])))
	profile["support_layers"] = _string_array_from_value(spec.get("support_layers", fallback.get("support_layers", profile["support_layers"])))
	profile["forbidden_layers"] = _string_array_from_value(spec.get("forbidden_layers", fallback.get("forbidden_layers", profile["forbidden_layers"])))
	var clearance_value := max(0, int(spec.get("clearance", fallback.get("clearance", 0))))
	profile["clearance_left"] = max(0, int(spec.get("clearance_left", fallback.get("clearance_left", profile["clearance_left"])))) + clearance_value
	profile["clearance_right"] = max(0, int(spec.get("clearance_right", fallback.get("clearance_right", profile["clearance_right"])))) + clearance_value
	profile["clearance_up"] = max(0, int(spec.get("clearance_up", fallback.get("clearance_up", profile["clearance_up"])))) + clearance_value
	profile["clearance_down"] = max(0, int(spec.get("clearance_down", fallback.get("clearance_down", profile["clearance_down"])))) + clearance_value
	profile["clearance_front"] = max(0, int(spec.get("clearance_front", fallback.get("clearance_front", profile["clearance_front"])))) + clearance_value
	profile["clearance_back"] = max(0, int(spec.get("clearance_back", fallback.get("clearance_back", profile["clearance_back"])))) + clearance_value
	profile["min_distance_to_protected"] = max(0, int(spec.get("min_distance_to_protected", fallback.get("min_distance_to_protected", 0))))
	profile["preferred_distance_to_protected"] = max(0, int(spec.get("preferred_distance_to_protected", fallback.get("preferred_distance_to_protected", 0))))
	profile["min_distance_from_same_kind"] = max(0, int(spec.get("min_distance_from_same_kind", fallback.get("min_distance_from_same_kind", 0))))
	profile["requires_reachable"] = bool(spec.get("requires_reachable", fallback.get("requires_reachable", profile["requires_reachable"])))
	profile["reachability_point"] = str(spec.get("reachability_point", fallback.get("reachability_point", profile["reachability_point"]))).to_lower()
	profile["interaction_offset"] = _coord_dict_from_value(spec.get("interaction_offset", fallback.get("interaction_offset", profile["interaction_offset"])), dimension_from_profile(fallback))
	profile["entrance_offset"] = _coord_dict_from_value(spec.get("entrance_offset", fallback.get("entrance_offset", profile["entrance_offset"])), dimension_from_profile(fallback))
	if kind == "coin":
		profile["surface_type"] = "air"
		profile["requires_support"] = false
		profile["footprint_width"] = 1
		profile["footprint_height"] = 1
	return profile


## 摆放被拒绝时直接在返回结果里带上下一步动作，而不是只在 prompt 里要求模型"记得改用
## find_placement_anchors"——错误发生的当下就给出具体指引，比期望模型从一堆铁律里
## 翻出对应那条更可靠。
static func _placement_kind_token(value: String) -> String:
	var token := value.strip_edges().to_lower()
	if token.begins_with("res://") or token.ends_with(".tscn") or token.ends_with(".scn"):
		token = token.get_file().get_basename()
	if token == "coins":
		return "coin"
	return token


const _PLACEMENT_RETRY_HINT := "Do not retry with another guessed coordinate. Call find_placement_anchors (same target_path/profile/region) to get a real legal anchor, or validate_object_placements to check a specific candidate."

static func _with_placement_retry_hint(check: Dictionary) -> Dictionary:
	var retryable_codes := [
		"placement_missing_support",
		"placement_support_layer_mismatch",
		"placement_forbidden_layer",
		"placement_same_kind_too_close",
		"placement_unreachable",
		"placement_cell_not_empty",
		"placement_clearance_blocked",
		"placement_protected_cell",
	]
	var error_code := str(check.get("error_code", ""))
	if error_code in retryable_codes:
		var prefix := ""
		if error_code == "placement_cell_not_empty":
			prefix = "The input coords are the object's footprint cell, not the ground/support cell. For an object standing on terrain, use the empty cell above the solid support cell; for coins use placement_kind='coin' or requires_support=false. "
		elif error_code == "placement_missing_support":
			prefix = "The footprint is empty but there is no solid support below it on the checked map_layer. For floating coins use placement_kind='coin' or requires_support=false; for ground objects choose an empty cell directly above real terrain. "
		check["hint"] = prefix + _PLACEMENT_RETRY_HINT
	return check


static func _with_object_batch_failure(check: Dictionary, object_index: int, object_spec: Dictionary, map_layer: int, profile: Dictionary) -> Dictionary:
	var enriched := check.duplicate(true)
	enriched["failed_index"] = object_index
	enriched["failed_object"] = object_spec.duplicate(true)
	enriched["map_layer"] = map_layer
	enriched["batch_atomic"] = true
	enriched["message"] = str(enriched.get("message", "")) + " (no objects were placed from this batch)"
	if not profile.is_empty():
		enriched["placement_profile"] = profile.duplicate(true)
	return _with_placement_retry_hint(enriched)


static func _validate_single_object_placement(
	map_node: Node,
	target_path: String,
	dimension: int,
	map_layer: int,
	coords: Vector3i,
	profile: Dictionary,
	occupied: Dictionary,
	blocked_cells: Dictionary,
	planned: Dictionary,
	input: Dictionary
) -> Dictionary:
	var footprint := _placement_footprint(coords, profile, dimension)
	var support := _placement_support_cells(coords, profile, dimension)
	var protected_cells := _protected_cell_set(input, dimension)
	var forbidden_layers: Array = profile.get("forbidden_layers", [])
	var support_layers: Array = profile.get("support_layers", [])
	for cell in footprint:
		var key := MapValidator.coord_key(cell)
		if occupied.has(key) or blocked_cells.has(key) or planned.has(key):
			return {
				"ok": false,
				"message": "object footprint overlaps an occupied/blocked/protected map cell",
				"error_code": "placement_clearance_blocked",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_cell": MapValidator.coord_payload(cell, dimension),
				"target": target_path,
			}
		var semantic_hit := _cell_has_any_semantic_layer(target_path, cell, dimension, forbidden_layers)
		if bool(semantic_hit.get("hit", false)):
			return {
				"ok": false,
				"message": "object footprint intersects a forbidden semantic layer",
				"error_code": "placement_forbidden_layer",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_cell": MapValidator.coord_payload(cell, dimension),
				"semantic": semantic_hit,
			}
		if protected_cells.has(key) and bool(profile.get("avoid_protected_cells", true)):
			return {
				"ok": false,
				"message": "object footprint intersects a protected route/frontier cell",
				"error_code": "placement_protected_cell",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_cell": MapValidator.coord_payload(cell, dimension),
			}
		var map_cell := _read_map_cell(map_node, cell, dimension, map_layer)
		if not _is_empty_cell(map_node, map_cell):
			return {
				"ok": false,
				"message": "object footprint must be empty",
				"error_code": "placement_cell_not_empty",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_cell": MapValidator.coord_payload(cell, dimension),
			}
	var clearance_cells := _placement_clearance_cells(coords, profile, dimension)
	for cell in clearance_cells:
		var key := MapValidator.coord_key(cell)
		if protected_cells.has(key):
			return {
				"ok": false,
				"message": "object clearance intersects a protected route/frontier cell",
				"error_code": "placement_protected_cell",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_cell": MapValidator.coord_payload(cell, dimension),
			}
		if occupied.has(key) or blocked_cells.has(key) or planned.has(key):
			return {
				"ok": false,
				"message": "object clearance overlaps an occupied/blocked map cell",
				"error_code": "placement_clearance_blocked",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_cell": MapValidator.coord_payload(cell, dimension),
			}
		var clearance_semantic_hit := _cell_has_any_semantic_layer(target_path, cell, dimension, forbidden_layers)
		if bool(clearance_semantic_hit.get("hit", false)):
			return {
				"ok": false,
				"message": "object clearance intersects a forbidden semantic layer",
				"error_code": "placement_forbidden_layer",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_cell": MapValidator.coord_payload(cell, dimension),
				"semantic": clearance_semantic_hit,
			}
	if bool(profile.get("requires_support", true)):
		var supported := false
		var support_layer_match := support_layers.is_empty()
		for cell in support:
			var support_cell := _read_map_cell(map_node, cell, dimension, map_layer)
			if not _is_empty_cell(map_node, support_cell):
				supported = true
				if support_layers.is_empty() or bool(_cell_has_any_semantic_layer(target_path, cell, dimension, support_layers).get("hit", false)):
					support_layer_match = true
				break
		if not supported:
			return {
				"ok": false,
				"message": "object placement requires solid support under its footprint",
				"error_code": "placement_missing_support",
				"coords": MapValidator.coord_payload(coords, dimension),
				"support_cells": _coord_payloads(support, dimension),
			}
		if not support_layer_match:
			return {
				"ok": false,
				"message": "object support exists but does not match required support_layers",
				"error_code": "placement_support_layer_mismatch",
				"coords": MapValidator.coord_payload(coords, dimension),
				"support_cells": _coord_payloads(support, dimension),
				"support_layers": support_layers,
			}
	var surface_check := _validate_placement_surface(map_node, target_path, coords, profile, dimension, map_layer)
	if not bool(surface_check.get("ok", true)):
		return surface_check
	var reachability_check := _validate_placement_reachability(map_node, coords, profile, input, dimension, map_layer)
	if not bool(reachability_check.get("ok", true)):
		return reachability_check
	var same_kind_distance := int(profile.get("min_distance_from_same_kind", 0))
	if same_kind_distance > 0:
		var nearest_same := _nearest_same_kind_distance(coords, profile, target_path, input, dimension)
		if nearest_same < same_kind_distance:
			return {
				"ok": false,
				"message": "object placement is too close to another object of the same kind/resource",
				"error_code": "placement_same_kind_too_close",
				"coords": MapValidator.coord_payload(coords, dimension),
				"nearest_same_kind_distance": nearest_same,
				"min_distance_from_same_kind": same_kind_distance,
			}
	return {
		"ok": true,
		"coords": MapValidator.coord_payload(coords, dimension),
		"footprint_cells": _coord_payloads(footprint, dimension),
		"support_cells": _coord_payloads(support, dimension),
	}


static func _placement_footprint(anchor: Vector3i, profile: Dictionary, dimension: int) -> Array:
	var width := max(1, int(profile.get("footprint_width", 1)))
	var height := max(1, int(profile.get("footprint_height", 1)))
	var cells: Array = []
	var bounds := _placement_bounds(anchor, profile, dimension)
	if dimension == 3:
		for x in range(int(bounds["min_x"]), int(bounds["max_x"]) + 1):
			for y in range(int(bounds["min_y"]), int(bounds["max_y"]) + 1):
				for z in range(int(bounds["min_z"]), int(bounds["max_z"]) + 1):
					cells.append(Vector3i(x, y, z))
	else:
		for x in range(int(bounds["min_x"]), int(bounds["max_x"]) + 1):
			for y in range(int(bounds["min_y"]), int(bounds["max_y"]) + 1):
				cells.append(Vector3i(x, y, 0))
	return cells


static func _placement_support_cells(anchor: Vector3i, profile: Dictionary, dimension: int) -> Array:
	var bounds := _placement_bounds(anchor, profile, dimension)
	var cells: Array = []
	if dimension == 3:
		for x in range(int(bounds["min_x"]), int(bounds["max_x"]) + 1):
			for z in range(int(bounds["min_z"]), int(bounds["max_z"]) + 1):
				cells.append(Vector3i(x, int(bounds["min_y"]) - 1, z))
	else:
		for x in range(int(bounds["min_x"]), int(bounds["max_x"]) + 1):
			cells.append(Vector3i(x, int(bounds["max_y"]) + 1, 0))
	return cells


static func _placement_clearance_cells(anchor: Vector3i, profile: Dictionary, dimension: int) -> Array:
	var left := max(0, int(profile.get("clearance_left", 0)))
	var right := max(0, int(profile.get("clearance_right", 0)))
	var up := max(0, int(profile.get("clearance_up", 0)))
	var down := max(0, int(profile.get("clearance_down", 0)))
	var front := max(0, int(profile.get("clearance_front", 0)))
	var back := max(0, int(profile.get("clearance_back", 0)))
	if left + right + up + down + front + back <= 0:
		return []
	var bounds := _placement_bounds(anchor, profile, dimension)
	var footprint_keys := {}
	for cell in _placement_footprint(anchor, profile, dimension):
		footprint_keys[MapValidator.coord_key(cell)] = true
	var cells: Array = []
	for x in range(int(bounds["min_x"]) - left, int(bounds["max_x"]) + right + 1):
		for y in range(int(bounds["min_y"]) - up, int(bounds["max_y"]) + down + 1):
			for z in range((int(bounds["min_z"]) - back) if dimension == 3 else 0, ((int(bounds["max_z"]) + front + 1) if dimension == 3 else 1)):
				var next := Vector3i(x, y, z)
				var key := MapValidator.coord_key(next)
				if not footprint_keys.has(key):
					cells.append(next)
					footprint_keys[key] = true
	return cells


static func _placement_bounds(anchor: Vector3i, profile: Dictionary, dimension: int) -> Dictionary:
	var width := max(1, int(profile.get("footprint_width", 1)))
	var height := max(1, int(profile.get("footprint_height", 1)))
	var anchor_mode := str(profile.get("anchor", "bottom_center")).to_lower()
	var min_x := anchor.x
	var max_x := anchor.x
	var min_y := anchor.y
	var max_y := anchor.y
	if anchor_mode.ends_with("left"):
		min_x = anchor.x
		max_x = anchor.x + width - 1
	elif anchor_mode.ends_with("right"):
		min_x = anchor.x - width + 1
		max_x = anchor.x
	else:
		var left := int(floor(float(width - 1) / 2.0))
		min_x = anchor.x - left
		max_x = min_x + width - 1
	if anchor_mode.begins_with("top"):
		min_y = anchor.y
		max_y = anchor.y + height - 1
	elif anchor_mode == "center":
		var up := int(floor(float(height - 1) / 2.0))
		min_y = anchor.y - up
		max_y = min_y + height - 1
	else:
		min_y = anchor.y - height + 1
		max_y = anchor.y
	var result := {"min_x": min_x, "max_x": max_x, "min_y": min_y, "max_y": max_y, "min_z": anchor.z, "max_z": anchor.z}
	if dimension == 3:
		var depth := max(1, int(profile.get("footprint_depth", profile.get("footprint_width", 1))))
		var back := int(floor(float(depth - 1) / 2.0))
		result["min_z"] = anchor.z - back
		result["max_z"] = int(result["min_z"]) + depth - 1
	return result


static func _validate_placement_surface(map_node: Node, target_path: String, coords: Vector3i, profile: Dictionary, dimension: int, map_layer: int) -> Dictionary:
	var surface_type := str(profile.get("surface_type", "ground")).to_lower()
	if surface_type in ["", "ground", "air", "water", "water_surface", "room_center", "branch_end", "path_edge"]:
		return {"ok": true}
	if surface_type == "wall":
		var bounds := _placement_bounds(coords, profile, dimension)
		var wall_layers: Array = profile.get("support_layers", [])
		var candidates: Array = []
		if dimension == 3:
			for y in range(int(bounds["min_y"]), int(bounds["max_y"]) + 1):
				for z in range(int(bounds["min_z"]), int(bounds["max_z"]) + 1):
					candidates.append(Vector3i(int(bounds["min_x"]) - 1, y, z))
					candidates.append(Vector3i(int(bounds["max_x"]) + 1, y, z))
		else:
			for y in range(int(bounds["min_y"]), int(bounds["max_y"]) + 1):
				candidates.append(Vector3i(int(bounds["min_x"]) - 1, y, 0))
				candidates.append(Vector3i(int(bounds["max_x"]) + 1, y, 0))
		for cell in candidates:
			var map_cell := _read_map_cell(map_node, cell, dimension, map_layer)
			if _is_empty_cell(map_node, map_cell):
				continue
			if wall_layers.is_empty() or bool(_cell_has_any_semantic_layer(target_path, cell, dimension, wall_layers).get("hit", false)):
				return {"ok": true}
		return {
			"ok": false,
			"message": "wall placement requires a solid/semantic wall cell next to the footprint",
			"error_code": "placement_missing_wall_support",
			"coords": MapValidator.coord_payload(coords, dimension),
			"support_layers": wall_layers,
		}
	return {"ok": true}


static func _validate_placement_reachability(map_node: Node, coords: Vector3i, profile: Dictionary, input: Dictionary, dimension: int, map_layer: int) -> Dictionary:
	if not bool(profile.get("requires_reachable", false)):
		return {"ok": true}
	if not input.has("start"):
		return {
			"ok": false,
			"message": "placement requires reachability but no start was provided",
			"error_code": "placement_reachability_start_required",
			"coords": MapValidator.coord_payload(coords, dimension),
		}
	var region := MapValidator.region_from_input(input, dimension)
	var goal := _placement_reachability_point(coords, profile, dimension)
	if not MapValidator.in_region(goal, region):
		return {
			"ok": false,
			"message": "placement reachability point is outside the validation/search region",
			"error_code": "placement_reachability_goal_out_of_region",
			"coords": MapValidator.coord_payload(coords, dimension),
			"goal": MapValidator.coord_payload(goal, dimension),
		}
	var reachable := _reachable_cells_for_input(map_node, input, region, dimension, map_layer)
	if not bool(reachable.get("ok", false)):
		return reachable
	var reachable_cells: Dictionary = reachable.get("reachable", {})
	var goal_key := MapValidator.coord_key(goal)
	if not reachable_cells.has(goal_key):
		return {
			"ok": false,
			"message": "placement reachability point is not reachable from start under the requested movement model",
			"error_code": "placement_unreachable",
			"coords": MapValidator.coord_payload(coords, dimension),
			"goal": MapValidator.coord_payload(goal, dimension),
			"start": input.get("start", {}),
			"movement_model": str(MapValidator.movement_from_input(input, dimension).get("model", "grid")),
		}
	return {"ok": true, "goal": MapValidator.coord_payload(goal, dimension)}


static func _placement_reachability_point(coords: Vector3i, profile: Dictionary, dimension: int) -> Vector3i:
	var point_type := str(profile.get("reachability_point", "anchor")).to_lower()
	var offset_key := "entrance_offset" if point_type == "entrance" else "interaction_offset"
	if point_type in ["interaction", "entrance"]:
		var offset := MapValidator.coord_from_input(profile.get(offset_key, {}), dimension)
		return coords + offset
	return coords


static func _reachable_cells_for_input(map_node: Node, input: Dictionary, region: Dictionary, dimension: int, map_layer: int) -> Dictionary:
	var cache_key := "_placement_reachable_%s_%s_%s_%s_%s_%s" % [
		str(input.get("start", {})),
		str(input.get("movement_model", "grid")),
		str(region.get("x", 0)),
		str(region.get("y", 0)),
		str(region.get("width", 1)),
		str(region.get("height", 1)),
	]
	if input.has(cache_key):
		return input[cache_key]
	var start := MapValidator.coord_from_input(input.get("start", {}), dimension)
	var movement := MapValidator.movement_from_input(input, dimension)
	var filled := _collect_filled_cells(map_node, region, dimension, map_layer)
	if not MapValidator.in_region(start, region):
		var out_of_region := {"ok": false, "message": "placement reachability start is outside region", "error_code": "placement_reachability_start_out_of_region"}
		input[cache_key] = out_of_region
		return out_of_region
	if not MapValidator.is_standable(filled, start, region, movement):
		var not_standable := {"ok": false, "message": "placement reachability start is not standable", "error_code": "placement_reachability_start_not_standable"}
		input[cache_key] = not_standable
		return not_standable
	var reachable := {}
	var queue: Array = [start]
	reachable[MapValidator.coord_key(start)] = true
	var cursor := 0
	while cursor < queue.size():
		var current: Vector3i = queue[cursor]
		cursor += 1
		for next in MapValidator.movement_neighbors(filled, current, region, movement):
			var key := MapValidator.coord_key(next)
			if reachable.has(key):
				continue
			reachable[key] = true
			queue.append(next)
	var result := {"ok": true, "reachable": reachable, "count": reachable.size()}
	input[cache_key] = result
	return result


static func _placement_candidate_cells(input: Dictionary, region: Dictionary, profile: Dictionary, dimension: int) -> Dictionary:
	var surface_type := str(profile.get("surface_type", "ground")).to_lower()
	match surface_type:
		"room_center":
			return _candidate_cells_from_named_sets(input, region, dimension, ["room_centers"], "room_centers")
		"branch_end":
			return _candidate_cells_from_named_sets(input, region, dimension, ["branch_ends"], "branch_ends")
		"path_edge":
			return _path_edge_candidate_cells(input, region, dimension)
		_:
			return {"ok": true, "source": "region", "candidates": _all_region_cells(region, dimension)}


static func _candidate_cells_from_named_sets(input: Dictionary, region: Dictionary, dimension: int, fields: Array, source_name: String) -> Dictionary:
	var candidates: Array = []
	var seen := {}
	for field in fields:
		var value = input.get(field, [])
		if not (value is Array):
			continue
		for coord_value in value:
			var coords := MapValidator.coord_from_input(coord_value, dimension)
			var key := MapValidator.coord_key(coords)
			if seen.has(key) or not MapValidator.in_region(coords, region):
				continue
			seen[key] = true
			candidates.append(coords)
	if candidates.is_empty():
		return {"ok": false, "message": "surface_type requires explicit %s candidates" % source_name, "error_code": "placement_anchor_candidates_required", "source": source_name}
	return {"ok": true, "source": source_name, "candidates": candidates}


static func _path_edge_candidate_cells(input: Dictionary, region: Dictionary, dimension: int) -> Dictionary:
	var path_cells := _protected_cell_set(input, dimension)
	if path_cells.is_empty():
		return {"ok": false, "message": "surface_type=path_edge requires protected/path/route/frontier cells", "error_code": "placement_anchor_candidates_required", "source": "path_edge"}
	var candidates: Array = []
	var seen := {}
	var offsets := [Vector3i(1, 0, 0), Vector3i(-1, 0, 0), Vector3i(0, 1, 0), Vector3i(0, -1, 0)]
	if dimension == 3:
		offsets.append_array([Vector3i(0, 0, 1), Vector3i(0, 0, -1)])
	for key in path_cells.keys():
		var base := _coords_from_key(str(key), dimension)
		for offset in offsets:
			var candidate: Vector3i = base + offset
			var candidate_key := MapValidator.coord_key(candidate)
			if seen.has(candidate_key) or path_cells.has(candidate_key) or not MapValidator.in_region(candidate, region):
				continue
			seen[candidate_key] = true
			candidates.append(candidate)
	if candidates.is_empty():
		return {"ok": false, "message": "no path edge candidate cells were found inside the region", "error_code": "placement_anchor_candidates_empty", "source": "path_edge"}
	return {"ok": true, "source": "path_edge", "candidates": candidates}


static func _all_region_cells(region: Dictionary, dimension: int) -> Array:
	var candidates: Array = []
	for dz in range(int(region["depth"])):
		for dy in range(int(region["height"])):
			for dx in range(int(region["width"])):
				candidates.append(Vector3i(int(region["x"]) + dx, int(region["y"]) + dy, int(region["z"]) + dz))
	return candidates


static func _protected_cell_set(input: Dictionary, dimension: int) -> Dictionary:
	var protected := {}
	for field in ["protected_cells", "path_cells", "route_cells", "frontier_cells"]:
		var value = input.get(field, [])
		if not (value is Array):
			continue
		for coord_value in value:
			var coords := MapValidator.coord_from_input(coord_value, dimension)
			protected[MapValidator.coord_key(coords)] = true
	return protected


static func _score_placement_anchor(coords: Vector3i, profile: Dictionary, protected_cells: Dictionary, target_path: String, input: Dictionary, dimension: int) -> float:
	var score := 100.0
	var min_distance := int(profile.get("min_distance_to_protected", 0))
	if min_distance > 0 and not protected_cells.is_empty():
		var nearest := _nearest_distance_to_cell_set(coords, protected_cells, dimension)
		if nearest < min_distance:
			score -= float((min_distance - nearest) * 25)
	var preferred_distance := int(profile.get("preferred_distance_to_protected", 0))
	if preferred_distance > 0 and not protected_cells.is_empty():
		var nearest_preferred := _nearest_distance_to_cell_set(coords, protected_cells, dimension)
		score += maxf(0.0, 40.0 - float(abs(nearest_preferred - preferred_distance) * 8))
	var same_kind_distance := int(profile.get("min_distance_from_same_kind", 0))
	if same_kind_distance > 0:
		var nearest_same := _nearest_same_kind_distance(coords, profile, target_path, input, dimension)
		if nearest_same < same_kind_distance:
			score -= float((same_kind_distance - nearest_same) * 40)
		else:
			score += minf(30.0, float(nearest_same - same_kind_distance))
	score += _nearest_bonus(coords, _protected_cell_set_from_field(input, "branch_ends", dimension), 35.0, dimension)
	score += _nearest_bonus(coords, _protected_cell_set_from_field(input, "room_centers", dimension), 25.0, dimension)
	score += _nearest_bonus(coords, _protected_cell_set_from_field(input, "reward_cells", dimension), 20.0, dimension)
	return score


static func _nearest_distance_to_cell_set(coords: Vector3i, cells: Dictionary, dimension: int) -> int:
	var nearest := 999999
	for key in cells.keys():
		var other := _coords_from_key(str(key), dimension)
		nearest = mini(nearest, abs(coords.x - other.x) + abs(coords.y - other.y) + abs(coords.z - other.z))
	return nearest


static func _nearest_bonus(coords: Vector3i, cells: Dictionary, max_bonus: float, dimension: int) -> float:
	if cells.is_empty():
		return 0.0
	var nearest := _nearest_distance_to_cell_set(coords, cells, dimension)
	return maxf(0.0, max_bonus - float(nearest * 6))


static func _protected_cell_set_from_field(input: Dictionary, field: String, dimension: int) -> Dictionary:
	var protected := {}
	var value = input.get(field, [])
	if not (value is Array):
		return protected
	for coord_value in value:
		var coords := MapValidator.coord_from_input(coord_value, dimension)
		protected[MapValidator.coord_key(coords)] = true
	return protected


static func _coords_from_key(key: String, dimension: int) -> Vector3i:
	var parts := key.split(",")
	return Vector3i(int(parts[0]), int(parts[1]), int(parts[2]) if dimension == 3 and parts.size() > 2 else 0)


static func _nearest_same_kind_distance(coords: Vector3i, profile: Dictionary, target_path: String, input: Dictionary, dimension: int) -> int:
	var region := {"min_x": -2147483648, "max_x": 2147483647, "min_y": -2147483648, "max_y": 2147483647, "min_z": -2147483648, "max_z": 2147483647}
	var entries := _spatial_entries_in_region(target_path, region, dimension)
	var wanted_kind := str(profile.get("name", ""))
	var wanted_resource := str(input.get("resource", input.get("resource_key", "")))
	var nearest := 999999
	var ignored := _protected_cell_set_from_field(input, "ignore_object_coords", dimension)
	for entry in entries:
		if not _is_object_index_entry(entry):
			continue
		var other := MapValidator.coord_from_input(entry.get("coords", {}), dimension)
		if ignored.has(MapValidator.coord_key(other)):
			continue
		var entry_resource := str(entry.get("resource", entry.get("resource_key", "")))
		var tags = entry.get("tags", [])
		var same := wanted_resource != "" and entry_resource == wanted_resource
		if not same and wanted_kind != "":
			same = str(entry.get("semantic_layer", "")) == wanted_kind
			if tags is Array:
				same = same or (tags as Array).has(wanted_kind)
		if not same:
			continue
		nearest = mini(nearest, abs(coords.x - other.x) + abs(coords.y - other.y) + abs(coords.z - other.z))
	return nearest


static func _best_placement_anchor(
	target: Node,
	target_path: String,
	region: Dictionary,
	dimension: int,
	map_layer: int,
	profile: Dictionary,
	occupied: Dictionary,
	blocked_cells: Dictionary,
	input: Dictionary
) -> Dictionary:
	var protected_cells := _protected_cell_set(input, dimension)
	var candidate_result := _placement_candidate_cells(input, region, profile, dimension)
	if not bool(candidate_result.get("ok", false)):
		return candidate_result
	var best := {}
	var best_score := -999999.0
	for coords in candidate_result.get("candidates", []):
		var check := _validate_single_object_placement(target, target_path, dimension, map_layer, coords, profile, occupied, blocked_cells, {}, input)
		if not bool(check.get("ok", false)):
			continue
		var score := _score_placement_anchor(coords, profile, protected_cells, target_path, input, dimension)
		if best.is_empty() or score > best_score:
			best_score = score
			best = {
				"ok": true,
				"coords": MapValidator.coord_payload(coords, dimension),
				"score": score,
				"surface_source": candidate_result.get("source", "region"),
				"footprint_cells": check.get("footprint_cells", []),
				"support_cells": check.get("support_cells", []),
			}
	if best.is_empty():
		return {"ok": false, "message": "no legal placement anchor found in repair region", "error_code": "placement_anchor_not_found"}
	return best


static func _coord_payloads(cells: Array, dimension: int) -> Array:
	var payloads: Array = []
	for cell in cells:
		payloads.append(MapValidator.coord_payload(cell, dimension))
	return payloads


static func _string_array_from_value(value) -> Array:
	var result: Array = []
	if value is Array:
		for item in value:
			var text := str(item).strip_edges().to_lower()
			if text != "":
				result.append(text)
	elif value is String:
		for item in str(value).split(","):
			var text := str(item).strip_edges().to_lower()
			if text != "":
				result.append(text)
	return result


static func dimension_from_profile(profile_source: Dictionary) -> int:
	if profile_source.has("z") or profile_source.has("depth") or profile_source.has("footprint_depth"):
		return 3
	return 2


static func _coord_dict_from_value(value, dimension: int) -> Dictionary:
	if value is Dictionary:
		return {
			"x": int((value as Dictionary).get("x", 0)),
			"y": int((value as Dictionary).get("y", 0)),
			"z": int((value as Dictionary).get("z", 0)) if dimension == 3 else 0,
		}
	if value is Array:
		var values: Array = value
		return {
			"x": int(values[0]) if values.size() > 0 else 0,
			"y": int(values[1]) if values.size() > 1 else 0,
			"z": int(values[2]) if dimension == 3 and values.size() > 2 else 0,
		}
	return {"x": 0, "y": 0, "z": 0}


static func _cell_has_any_semantic_layer(target_path: String, coords: Vector3i, dimension: int, layers: Array) -> Dictionary:
	if layers.is_empty():
		return {"hit": false}
	var normalized := {}
	for layer in layers:
		normalized[str(layer).to_lower()] = true
	var region := {
		"x": coords.x, "y": coords.y, "z": coords.z,
		"width": 1, "height": 1, "depth": 1,
		"min_x": coords.x, "max_x": coords.x,
		"min_y": coords.y, "max_y": coords.y,
		"min_z": coords.z, "max_z": coords.z,
	}
	for entry in _spatial_entries_in_region(target_path, region, dimension):
		var semantic_layer := str(entry.get("semantic_layer", "")).to_lower()
		if normalized.has(semantic_layer):
			return {"hit": true, "semantic_layer": semantic_layer, "entry": entry}
		var tags = entry.get("tags", [])
		if tags is Array:
			for tag in tags:
				var tag_text := str(tag).to_lower()
				if normalized.has(tag_text):
					return {"hit": true, "tag": tag_text, "entry": entry}
	return {"hit": false}


static func _object_occupancy_from_spatial_index(target_path: String, dimension: int) -> Dictionary:
	var occupied := {}
	var parsed := _read_json_resource(SPATIAL_INDEX_PATH)
	if not bool(parsed.get("exists", false)):
		return occupied
	var data = parsed.get("data", {})
	if not (data is Dictionary):
		return occupied
	var branch = (data as Dictionary).get("3d" if dimension == 3 else "2d", {})
	if not (branch is Dictionary):
		return occupied
	var target_entries = (branch as Dictionary).get(target_path, {})
	if not (target_entries is Dictionary):
		return occupied
	for key in (target_entries as Dictionary).keys():
		var entry = (target_entries as Dictionary)[key]
		if entry is Dictionary and (str(entry.get("kind", "")) == "object" or str(entry.get("scene_path", "")) != ""):
			var coords = (entry as Dictionary).get("coords", {})
			if coords is Dictionary:
				occupied[MapValidator.coord_key(MapValidator.coord_from_input(coords, dimension))] = true
			else:
				occupied[str(key)] = true
	return occupied


static func _blocked_object_cells_from_spatial_index(target_path: String, dimension: int) -> Dictionary:
	var blocked := {}
	var parsed := _read_json_resource(SPATIAL_INDEX_PATH)
	if not bool(parsed.get("exists", false)):
		return blocked
	var data = parsed.get("data", {})
	if not (data is Dictionary):
		return blocked
	var branch = (data as Dictionary).get("3d" if dimension == 3 else "2d", {})
	if not (branch is Dictionary):
		return blocked
	var target_entries = (branch as Dictionary).get(target_path, {})
	if not (target_entries is Dictionary):
		return blocked
	for key in (target_entries as Dictionary).keys():
		var entry = (target_entries as Dictionary)[key]
		if not (entry is Dictionary):
			continue
		var semantic_layer := str(entry.get("semantic_layer", ""))
		var tags = entry.get("tags", [])
		var is_blocked := semantic_layer in ["water", "obstacle", "blocked"]
		if tags is Array:
			is_blocked = is_blocked or (tags as Array).has("water") or (tags as Array).has("blocked") or (tags as Array).has("obstacle")
		if not is_blocked:
			continue
		var coords = (entry as Dictionary).get("coords", {})
		if coords is Dictionary:
			blocked[MapValidator.coord_key(MapValidator.coord_from_input(coords, dimension))] = entry
	return blocked


static func _maybe_update_object_spatial_index(
	input: Dictionary,
	undo_manager: Node,
	target_path: String,
	parent_path: String,
	dimension: int,
	prepared: Array
) -> Dictionary:
	if not bool(input.get("update_spatial_index", true)):
		return {"ok": true, "updated": false}
	var absolute := ProjectSettings.globalize_path(SPATIAL_INDEX_PATH)
	var before_exists := FileAccess.file_exists(absolute)
	var before_text := FileAccess.get_file_as_string(absolute) if before_exists else ""
	var parsed = JSON.parse_string(before_text) if before_text != "" else {}
	var index: Dictionary = parsed if parsed is Dictionary else {}
	var branch_key := "3d" if dimension == 3 else "2d"
	if not index.has(branch_key) or not (index[branch_key] is Dictionary):
		index[branch_key] = {}
	if not index[branch_key].has(target_path) or not (index[branch_key][target_path] is Dictionary):
		index[branch_key][target_path] = {}
	var target_index: Dictionary = index[branch_key][target_path]
	var total_entries := _count_index_entries(index.get("2d", {})) + _count_index_entries(index.get("3d", {}))
	var added := 0
	for prepared_value in prepared:
		var item: Dictionary = prepared_value
		var coords: Vector3i = item["coords"]
		var key := _object_index_key(coords, dimension, str(item.get("scene_path", "")), str(item.get("resource", "")))
		var existed := target_index.has(key)
		if not existed and total_entries >= MAX_SPATIAL_INDEX_ENTRIES:
			return {
				"ok": false,
				"message": "spatial index reached the %d-entry cap; compact it before placing more objects" % MAX_SPATIAL_INDEX_ENTRIES,
				"error_code": "spatial_index_full",
			}
		var spec: Dictionary = item.get("spec", {})
		var indexed_node_name := ""
		if item.get("node", null) is Node:
			var indexed_node: Node = item.get("node")
			indexed_node_name = indexed_node.name
		target_index[key] = {
			"kind": "object",
			"coords": MapValidator.coord_payload(coords, dimension),
			"scene_path": str(item.get("scene_path", "")),
			"resource": str(item.get("resource", "")),
			"resource_key": str(item.get("resource", "")),
			"parent_path": parent_path,
			"map_layer": int(item.get("map_layer", input.get("map_layer", 0))),
			"node_name": indexed_node_name,
			"semantic_layer": str(spec.get("semantic_layer", "object")),
			"tags": spec.get("tags", []),
		}
		for metadata_key in ["visual_group_id", "instance_id", "instance_kind"]:
			if spec.has(metadata_key):
				target_index[key][metadata_key] = str(spec.get(metadata_key, ""))
		if not existed:
			added += 1
			total_entries += 1
	var after_text := JSON.stringify(index, "\t")
	var write_result := _write_json_file(SPATIAL_INDEX_PATH, before_text, after_text, undo_manager)
	if not bool(write_result.get("ok", false)):
		return write_result
	return {"ok": true, "updated": true, "path": SPATIAL_INDEX_PATH, "objects": prepared.size(), "added": added, "total_entries": total_entries}


static func _object_index_key(coords: Vector3i, dimension: int, scene_path: String, resource_key: String) -> String:
	var suffix := resource_key if resource_key != "" else scene_path.get_file().get_basename()
	return "object:%s:%s" % [_index_coord_key(coords, dimension), suffix]


static func _repair_spatial_object_issues(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var root := editor_interface.get_edited_scene_root()
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var region := MapValidator.region_from_input(input, dimension)
	var target_path := str(target_result.get("path", ""))
	var issues: Array = []
	if bool(input.get("repair_overlaps", false)):
		issues.append_array(_object_entries_from_overlap_result(_detect_spatial_overlaps(target_path, region, dimension)))
	if bool(input.get("repair_blocked_objects", false)):
		issues.append_array(_object_entries_from_blocked_result(_detect_objects_on_blocked_cells(target_path, region, dimension)))
	if issues.is_empty():
		return {"ok": true, "changed": false, "message": "No repairable object overlap/blocking issues found"}
	var occupied := _occupied_object_cells_for_repair(target_path, dimension)
	var blocked := _blocked_object_cells_from_spatial_index(target_path, dimension)
	var before_text := _read_text_file(SPATIAL_INDEX_PATH)
	var parsed = JSON.parse_string(before_text) if before_text != "" else {}
	var index: Dictionary = parsed if parsed is Dictionary else {}
	var moved: Array = []
	for entry in issues:
		if not (entry is Dictionary):
			continue
		var object_entry: Dictionary = entry
		var old_coords := MapValidator.coord_from_input(object_entry.get("coords", {}), dimension)
		var node := _resolve_indexed_object_node(root, object_entry)
		if node == null:
			moved.append({"ok": false, "reason": "indexed object has no resolvable node", "entry": object_entry})
			continue
		var new_coords := _nearest_free_object_cell(old_coords, region, dimension, occupied, blocked)
		if new_coords == old_coords:
			moved.append({"ok": false, "reason": "no nearby free cell found", "entry": object_entry})
			continue
		var before_position = null
		if node is Node3D:
			before_position = (node as Node3D).position
		elif node is Node2D:
			before_position = (node as Node2D).position
		_apply_object_position(node, target, new_coords, dimension, node.get_parent())
		var after_position = null
		if node is Node3D:
			after_position = (node as Node3D).position
		elif node is Node2D:
			after_position = (node as Node2D).position
		if undo_manager != null and before_position != null and after_position != null and undo_manager.has_method("record_node_property"):
			undo_manager.record_node_property(node, "position", before_position, after_position)
		_update_object_entry_coords(index, object_entry, old_coords, new_coords, dimension)
		occupied.erase(MapValidator.coord_key(old_coords))
		occupied[MapValidator.coord_key(new_coords)] = true
		moved.append({"ok": true, "node": str(root.get_path_to(node)), "from": MapValidator.coord_payload(old_coords, dimension), "to": MapValidator.coord_payload(new_coords, dimension)})
	var after_text := JSON.stringify(index, "\t")
	var write_result := _write_json_file(SPATIAL_INDEX_PATH, before_text, after_text, undo_manager)
	if not bool(write_result.get("ok", false)):
		return write_result
	return {"ok": true, "changed": true, "target": target_path, "moved": moved, "spatial_index": {"ok": true, "updated": true, "path": SPATIAL_INDEX_PATH}}


static func _object_entries_from_overlap_result(overlap_result: Dictionary) -> Array:
	var entries: Array = []
	for overlap in overlap_result.get("overlaps", []):
		if not (overlap is Dictionary):
			continue
		var at_coord: Array = overlap.get("entries", [])
		var first_seen := false
		for entry in at_coord:
			if entry is Dictionary and _is_object_index_entry(entry):
				if first_seen:
					entries.append(entry)
				first_seen = true
	return entries


static func _object_entries_from_blocked_result(blocked_result: Dictionary) -> Array:
	var entries: Array = []
	for overlap in blocked_result.get("overlaps", []):
		if not (overlap is Dictionary):
			continue
		for entry in overlap.get("objects", []):
			if entry is Dictionary:
				entries.append(entry)
	return entries


static func _repairable_placement_entries(input: Dictionary, target_path: String, region: Dictionary, dimension: int) -> Array:
	var entries := _spatial_entries_in_region(target_path, region, dimension)
	var want_resource := str(input.get("resource", input.get("resource_key", ""))).strip_edges()
	var want_kind := str(input.get("placement_kind", input.get("kind", ""))).strip_edges().to_lower()
	var want_tags := _string_array_from_value(input.get("tags", []))
	var result: Array = []
	for entry in entries:
		if not _is_object_index_entry(entry):
			continue
		if want_resource != "":
			var entry_resource := str(entry.get("resource", entry.get("resource_key", "")))
			if entry_resource != want_resource:
				continue
		if want_kind != "" and _placement_kind_from_entry(entry) != want_kind:
			continue
		if not want_tags.is_empty():
			var entry_tags = entry.get("tags", [])
			var matched_tag := false
			if entry_tags is Array:
				for tag in want_tags:
					if (entry_tags as Array).has(tag):
						matched_tag = true
						break
			if not matched_tag:
				continue
		result.append(entry)
	return result


static func _placement_kind_from_entry(entry: Dictionary) -> String:
	var semantic := str(entry.get("semantic_layer", "")).strip_edges().to_lower()
	if semantic != "" and semantic != "object":
		return semantic
	var tags = entry.get("tags", [])
	if tags is Array and not (tags as Array).is_empty():
		return str((tags as Array)[0]).strip_edges().to_lower()
	var resource := str(entry.get("resource", entry.get("resource_key", ""))).strip_edges().to_lower()
	for known in ["tree", "rock", "bush", "decor", "building", "house", "hut", "npc", "enemy", "chest", "pickup", "save_point", "coin", "flying", "air"]:
		if resource.find(known) >= 0:
			return known
	return "generic"


static func _occupied_object_cells_for_repair(target_path: String, dimension: int) -> Dictionary:
	var region := {"min_x": -2147483648, "max_x": 2147483647, "min_y": -2147483648, "max_y": 2147483647, "min_z": -2147483648, "max_z": 2147483647}
	var entries := _spatial_entries_in_region(target_path, region, dimension)
	var occupied := {}
	for entry in entries:
		if _is_object_index_entry(entry):
			var coords := MapValidator.coord_from_input(entry.get("coords", {}), dimension)
			occupied[MapValidator.coord_key(coords)] = true
	return occupied


static func _resolve_indexed_object_node(root: Node, entry: Dictionary) -> Node:
	var node_path := str(entry.get("node_path", "")).strip_edges()
	if node_path != "":
		var found := root.get_node_or_null(NodePath(node_path))
		if found != null:
			return found
	var parent_path := str(entry.get("parent_path", "")).strip_edges()
	var node_name := str(entry.get("node_name", "")).strip_edges()
	if parent_path == "" or node_name == "":
		return null
	var parent := root if parent_path == "." else root.get_node_or_null(NodePath(parent_path))
	if parent == null:
		return null
	return parent.get_node_or_null(NodePath(node_name))


static func _nearest_free_object_cell(origin: Vector3i, region: Dictionary, dimension: int, occupied: Dictionary, blocked: Dictionary) -> Vector3i:
	var max_radius: int = maxi(int(region.get("width", 1)), int(region.get("height", 1))) + int(region.get("depth", 1))
	for radius in range(1, max_radius + 1):
		for offset in _candidate_offsets(radius, dimension):
			var candidate: Vector3i = origin + offset
			var key := MapValidator.coord_key(candidate)
			if not MapValidator.in_region(candidate, region):
				continue
			if occupied.has(key) or blocked.has(key):
				continue
			return candidate
	return origin


static func _candidate_offsets(radius: int, dimension: int) -> Array:
	var offsets: Array = []
	for dx in range(-radius, radius + 1):
		for dy in range(-radius, radius + 1):
			if abs(dx) + abs(dy) != radius:
				continue
			if dimension == 3:
				for dz in range(-radius, radius + 1):
					if abs(dx) + abs(dy) + abs(dz) == radius:
						offsets.append(Vector3i(dx, dy, dz))
			else:
				offsets.append(Vector3i(dx, dy, 0))
	return offsets


static func _update_object_entry_coords(index: Dictionary, entry: Dictionary, old_coords: Vector3i, new_coords: Vector3i, dimension: int) -> void:
	var branch_key := "3d" if dimension == 3 else "2d"
	var target_path := str(entry.get("_target_path", ""))
	var old_key := str(entry.get("_index_key", ""))
	if target_path == "" or old_key == "" or not index.has(branch_key):
		return
	var branch: Dictionary = index[branch_key]
	if not branch.has(target_path) or not (branch[target_path] is Dictionary):
		return
	var target_index: Dictionary = branch[target_path]
	if not target_index.has(old_key):
		return
	var updated: Dictionary = target_index[old_key].duplicate(true)
	updated["coords"] = MapValidator.coord_payload(new_coords, dimension)
	target_index.erase(old_key)
	target_index[_object_index_key(new_coords, dimension, str(updated.get("scene_path", "")), str(updated.get("resource", "")))] = updated


## 创建/维护资源语义表 res://.ai_agent_service/map_agent/resource_registry.json，
## 把自然语言资源词（grass/wall/river...）映射到真实的 TileSet/MeshLibrary/PackedScene 引用。
## 默认按 key 合并进已有表；replace=true 时整表覆盖。写入走 Undo 批次，可撤销/可预览。
static func write_resource_registry(input: Dictionary, _editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	var entries_value = input.get("entries", {})
	if not (entries_value is Dictionary) or (entries_value as Dictionary).is_empty():
		return {
			"ok": false,
			"message": "entries must be a non-empty object mapping resource keys to their definitions",
			"error_code": "invalid_entries",
		}
	var entries: Dictionary = entries_value
	var replace := bool(input.get("replace", false))
	var before_text := _read_text_file(RESOURCE_REGISTRY_PATH)
	var data: Dictionary = {}
	if before_text != "":
		var parsed = JSON.parse_string(before_text)
		if parsed is Dictionary:
			data = (parsed as Dictionary).duplicate(true)
	if replace:
		data = {}
	for key in entries.keys():
		var entry = entries[key]
		if not (entry is Dictionary):
			return {"ok": false, "message": "entry '%s' must be an object" % str(key), "error_code": "invalid_entry"}
		var contract_check := _normalize_resource_registry_entry(str(key), entry)
		if not bool(contract_check.get("ok", false)):
			return contract_check
		data[str(key)] = contract_check.get("entry", {})
	var after_text := JSON.stringify(data, "\t")
	var write_result := _write_json_file(RESOURCE_REGISTRY_PATH, before_text, after_text, undo_manager)
	if not bool(write_result.get("ok", false)):
		return write_result
	var invalid_existing_keys := _invalid_resource_registry_keys(data)
	return {
		"ok": true,
		"path": RESOURCE_REGISTRY_PATH,
		"keys": data.keys(),
		"written_keys": entries.keys(),
		"invalid_existing_keys": invalid_existing_keys,
		"warning": "registry still has invalid entries; rewrite these keys before using them in edit_map" if not invalid_existing_keys.is_empty() else "",
		"replaced": replace,
	}


static func _invalid_resource_registry_keys(data: Dictionary) -> Array:
	var invalid: Array = []
	for key in data.keys():
		var entry = data.get(key, {})
		if not (entry is Dictionary):
			invalid.append(str(key))
			continue
		var entry_dict: Dictionary = entry
		if not bool(_validate_resource_contract_shape(str(key), entry_dict).get("ok", false)):
			invalid.append(str(key))
			continue
		if entry_dict.has("scene_path"):
			continue
		var mode := str(entry_dict.get("mode", "2d")).strip_edges()
		if mode == "3d":
			if not entry_dict.has("item") and not entry_dict.has("mesh_library_item"):
				invalid.append(str(key))
			continue
		if not entry_dict.has("source_id") or _registry_2d_tile_signature(entry_dict).is_empty():
			invalid.append(str(key))
	return invalid


static func _normalize_resource_registry_entry(key: String, entry_value: Dictionary) -> Dictionary:
	var entry := entry_value.duplicate(true)
	var kind := str(entry.get("kind", "")).strip_edges()
	if kind == "":
		return {"ok": false, "message": "resource '%s' must declare kind" % key, "error_code": "invalid_resource_contract", "resource": key}
	entry["kind"] = kind
	var footprint = entry.get("footprint", {})
	if footprint == null or (footprint is Dictionary and (footprint as Dictionary).is_empty()):
		footprint = {"width": 1, "height": 1}
	if not (footprint is Dictionary):
		return {"ok": false, "message": "resource '%s' footprint must be an object" % key, "error_code": "invalid_resource_contract", "resource": key}
	var footprint_dict: Dictionary = footprint
	var width := max(1, int(footprint_dict.get("width", 1)))
	var height := max(1, int(footprint_dict.get("height", 1)))
	var depth := max(1, int(footprint_dict.get("depth", 1)))
	var normalized_footprint := {"width": width, "height": height}
	if footprint_dict.has("depth"):
		normalized_footprint["depth"] = depth
	entry["footprint"] = normalized_footprint
	entry["required_cells"] = max(1, int(entry.get("required_cells", width * height * depth)))
	if entry.has("visual_group_id") and str(entry.get("visual_group_id", "")).strip_edges() == "":
		return {"ok": false, "message": "resource '%s' visual_group_id must be non-empty when provided" % key, "error_code": "invalid_resource_contract", "resource": key}
	if entry.has("scene_path"):
		return {"ok": true, "entry": entry}
	var mode := str(entry.get("mode", "2d")).strip_edges()
	if mode == "3d":
		if not entry.has("item") and not entry.has("mesh_library_item"):
			return {"ok": false, "message": "3D resource '%s' must declare item or mesh_library_item" % key, "error_code": "invalid_resource_contract", "resource": key}
		return {"ok": true, "entry": entry}
	if not entry.has("source_id") or _registry_2d_tile_signature(entry).is_empty():
		return {"ok": false, "message": "2D resource '%s' must declare source_id and atlas_coords/atlas_x/atlas_y" % key, "error_code": "invalid_resource_contract", "resource": key}
	return {"ok": true, "entry": entry}


## 按 tag / 语义层 / 资源 key / 坐标范围检索空间索引，定位"左上角的树""村庄道路"这类语义对象，
## 支撑局部删除/替换，避免全量重绘。纯读，不需确认。
static func query_spatial_index(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var parsed := _read_json_resource(SPATIAL_INDEX_PATH)
	var dimension := 3 if str(input.get("dimension", "2d")) == "3d" else 2
	var branch_key := "3d" if dimension == 3 else "2d"
	if not bool(parsed.get("exists", false)):
		return {
			"ok": true,
			"dimension": dimension,
			"matches": [],
			"total": 0,
			"note": "spatial index has no data yet; run edit_map with update_spatial_index=true first",
		}
	var data: Dictionary = parsed.get("data", {})
	var branch = data.get(branch_key, {})
	if not (branch is Dictionary):
		return {"ok": true, "dimension": dimension, "matches": [], "total": 0}

	var want_tags: Array = input.get("tags", []) if input.get("tags", []) is Array else []
	var want_resource := str(input.get("resource", input.get("resource_key", ""))).strip_edges()
	var want_layer := str(input.get("semantic_layer", "")).strip_edges()
	var want_visual_group := str(input.get("visual_group_id", input.get("instance_id", ""))).strip_edges()
	var target_filter := str(input.get("target_path", "")).strip_edges()
	var map_layer := int(input.get("map_layer", 0))
	var has_map_layer_filter := input.has("map_layer")
	var has_region := input.has("x") or input.has("y") or input.has("z") \
		or input.has("width") or input.has("height") or input.has("depth")
	var region := _region_bounds(input)
	var limit := max(1, int(input.get("limit", 200)))

	var matches: Array = []
	var stale_entries := 0
	var checked_entries := 0
	var target_cache := {}
	for target_path in branch.keys():
		if target_filter != "" and str(target_path) != target_filter:
			continue
		var cells = branch[target_path]
		if not (cells is Dictionary):
			continue
		for coord_key in cells.keys():
			var entry = cells[coord_key]
			if not (entry is Dictionary):
				continue
			if not _entry_matches(entry, want_tags, want_resource, want_layer, want_visual_group):
				continue
			if has_map_layer_filter and dimension == 2 and int((entry as Dictionary).get("map_layer", -1)) != map_layer:
				continue
			if has_region and not _entry_in_region(entry, region, dimension):
				continue
			var hit := (entry as Dictionary).duplicate(true)
			var stale := _spatial_entry_stale_for_index_hit(editor_interface, target_cache, str(target_path), dimension, map_layer, hit)
			if bool(stale.get("checked", false)):
				checked_entries += 1
				if bool(stale.get("stale", false)):
					stale_entries += 1
					hit["_spatial_index_stale"] = true
					hit["_spatial_index_stale_detail"] = stale
			hit["target_path"] = str(target_path)
			hit["coord_key"] = str(coord_key)
			matches.append(hit)
			if matches.size() >= limit:
				break
		if matches.size() >= limit:
			break
	var result := {
		"ok": true,
		"dimension": dimension,
		"matches": matches,
		"total": matches.size(),
		"truncated": matches.size() >= limit,
		"index_entries": _count_index_entries(data.get("2d", {})) + _count_index_entries(data.get("3d", {})),
		"max_entries": MAX_SPATIAL_INDEX_ENTRIES,
	}
	if checked_entries > 0:
		result["checked_entries"] = checked_entries
		result["stale_entries"] = stale_entries
	if stale_entries > 0:
		result["stale_warning"] = "%d spatial index entries no longer match real source_id/atlas_coords; call describe_map_region and trust the real cells before editing." % stale_entries
	return result


## 把自然语言地图请求解析成结构化意图，并生成只读布局计划/操作草案。
## 该工具不写场景；真正落地仍由 edit_map / ensure_standard_map_layers 等工具执行。
static func plan_map_layout(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var context := describe_map_context({}, editor_interface)
	if not bool(context.get("ok", false)):
		return context
	var intent := MapIntentParser.parse(input, context)
	var plan := MapLayoutPlanner.plan(intent, context)
	var algorithm_input := input.duplicate(true)
	algorithm_input["mode"] = str(intent.get("mode", input.get("mode", "2d")))
	algorithm_input["theme"] = str(intent.get("theme", input.get("theme", "generic")))
	algorithm_input["density"] = str(intent.get("style", {}).get("density", input.get("density", "medium"))) if intent.get("style", {}) is Dictionary else str(input.get("density", "medium"))
	for key in ["x", "y", "z", "width", "height", "depth"]:
		if not algorithm_input.has(key) and plan.get("region", {}) is Dictionary and (plan.get("region", {}) as Dictionary).has(key):
			algorithm_input[key] = (plan.get("region", {}) as Dictionary)[key]
	var algorithm_plan := MapAlgorithms.build_algorithm_plan(algorithm_input, context)
	plan["algorithm_plan"] = algorithm_plan
	return {
		"ok": true,
		"intent": intent,
		"plan": plan,
		"message": "Map intent parsed and layout planned; review missing_resources before editing.",
	}


## Build a reusable, read-only algorithm plan for map generation/editing.
static func plan_map_algorithms(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var context := describe_map_context({}, editor_interface)
	if not bool(context.get("ok", false)):
		return context
	return MapAlgorithms.build_algorithm_plan(input, context)


## Build a platformer-specific critical path, jump graph, tile batches, reward arcs, and leap validation plan.
static func plan_platform_level(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var context := describe_map_context({}, editor_interface)
	if not bool(context.get("ok", false)):
		return context
	var platform_input := input.duplicate(true)
	platform_input["mode"] = "2d"
	_ensure_entry_anchor_from_frontier(platform_input)
	if bool(platform_input.get("connect_from_existing", true)):
		if _has_coord_dict(platform_input.get("entry_anchor", {})):
			platform_input["entry_sample"] = {"ok": true, "entry_anchor": platform_input["entry_anchor"], "source": str(platform_input.get("entry_anchor_source", "entry_anchor"))}
		else:
			var anchor_result := _platform_entry_anchor(platform_input, editor_interface)
			if bool(anchor_result.get("ok", false)):
				platform_input["entry_anchor"] = anchor_result.get("entry_anchor", {})
				platform_input["entry_support"] = anchor_result.get("entry_support", {})
				platform_input["entry_sample"] = anchor_result
			else:
				# 没扫描到左侧已有落脚点：让 composer 把 edit_map_batches 结构性清空，
				# 而不是只靠 prompt 提醒 agent "没有 entry_anchor 就别执行"。
				platform_input["entry_anchor_scan_failed"] = true
				platform_input["entry_sample"] = anchor_result
	return MapPlatformComposer.plan_platform_level(platform_input, context)


## Build a profile-based reachable frontier growth plan for platformer/topdown/dungeon/3d maps.
static func plan_reachable_map_growth(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var context := describe_map_context({}, editor_interface)
	if not bool(context.get("ok", false)):
		return context
	var growth_input := input.duplicate(true)
	var profile := str(growth_input.get("profile", "")).to_lower()
	if growth_input.has("start"):
		var frontier_result := compute_reachable_frontier(growth_input, editor_interface)
		if not bool(frontier_result.get("ok", false)):
			return frontier_result
		growth_input["frontier"] = frontier_result.get("rightmost_frontier", {})
		growth_input["reachable_frontier"] = frontier_result
		if profile in ["platform", "platformer", "side_scroller", "side-scroller"]:
			growth_input["entry_anchor"] = frontier_result.get("rightmost_frontier", {})
	_ensure_entry_anchor_from_frontier(growth_input)
	if profile in ["platform", "platformer", "side_scroller", "side-scroller"] and bool(growth_input.get("connect_from_existing", true)):
		if not _has_coord_dict(growth_input.get("entry_anchor", {})):
			var anchor_result := _platform_entry_anchor(growth_input, editor_interface)
			if bool(anchor_result.get("ok", false)):
				growth_input["entry_anchor"] = anchor_result.get("entry_anchor", {})
				growth_input["frontier"] = anchor_result.get("entry_anchor", {})
				growth_input["entry_sample"] = anchor_result
			else:
				growth_input["entry_anchor_scan_failed"] = true
				growth_input["entry_sample"] = anchor_result
	return MapReachableGrowth.plan_growth(growth_input, context)


static func _ensure_entry_anchor_from_frontier(input: Dictionary) -> void:
	if _has_coord_dict(input.get("entry_anchor", {})):
		return
	var frontier = input.get("frontier", {})
	if frontier is Dictionary and (frontier as Dictionary).get("cell", {}) is Dictionary:
		frontier = (frontier as Dictionary).get("cell", {})
	if not _has_coord_dict(frontier):
		return
	input["entry_anchor"] = {
		"x": int((frontier as Dictionary).get("x", 0)),
		"y": int((frontier as Dictionary).get("y", 0)),
	}
	input["entry_anchor_source"] = "frontier"


static func _has_coord_dict(value) -> bool:
	return value is Dictionary and (value as Dictionary).has("x") and (value as Dictionary).has("y")


## Read the real map and compute all cells reachable from a real player/unit start.
static func compute_reachable_frontier(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := int(input.get("map_layer", 0))
	var region := MapValidator.region_from_input(input, dimension)
	if int(region["width"]) * int(region["height"]) * int(region["depth"]) > MAX_DESCRIBED_CELLS:
		return {
			"ok": false,
			"message": "frontier region exceeds the %d-cell limit; compute frontier in a smaller region" % MAX_DESCRIBED_CELLS,
			"error_code": "region_too_large",
		}
	if not input.has("start"):
		return {"ok": false, "message": "start is required to compute a real reachable frontier", "error_code": "missing_start"}
	var start := MapValidator.coord_from_input(input.get("start", {}), dimension)
	var movement := MapValidator.movement_from_input(input, dimension)
	var filled := _collect_filled_cells(target, region, dimension, map_layer)
	if not MapValidator.in_region(start, region):
		return {"ok": false, "message": "start is outside the frontier region", "error_code": "start_out_of_region"}
	if not MapValidator.is_standable(filled, start, region, movement):
		var failure := MapValidator._foothold_failure(filled, start, region, movement, "start")
		failure.merge({
			"ok": false,
			"message": "start is not standable under the requested movement model",
			"error_code": "start_not_standable",
			"start": MapValidator.coord_payload(start, dimension),
			"movement_model": movement.get("model", "grid"),
		}, true)
		return failure
	var max_returned := max(1, int(input.get("max_returned_cells", 256)))
	var visited := {}
	var queue: Array = [start]
	visited[MapValidator.coord_key(start)] = start
	var cursor := 0
	while cursor < queue.size():
		var current: Vector3i = queue[cursor]
		cursor += 1
		for next in MapValidator.movement_neighbors(filled, current, region, movement):
			var key := MapValidator.coord_key(next)
			if visited.has(key):
				continue
			visited[key] = next
			queue.append(next)
	var reachable_cells: Array = []
	var reachable_footholds: Array = []
	var rightmost := start
	var reachable_vectors: Array = []
	for key in visited.keys():
		var coords: Vector3i = visited[key]
		reachable_vectors.append(coords)
		if coords.x > rightmost.x or (coords.x == rightmost.x and coords.y < rightmost.y):
			rightmost = coords
		if reachable_cells.size() < max_returned:
			reachable_cells.append(MapValidator.coord_payload(coords, dimension))
		if MapValidator.is_standable(filled, coords, region, movement):
			if reachable_footholds.size() < max_returned:
				reachable_footholds.append(MapValidator.coord_payload(coords, dimension))
	var frontier_candidates := _rightmost_frontier_candidates(reachable_vectors, filled, region, movement, dimension, max_returned)
	var first_blocked_gap := _first_blocked_gap(filled, rightmost, region, movement, visited, dimension)
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"region": region,
		"movement_model": movement.get("model", "grid"),
		"start": MapValidator.coord_payload(start, dimension),
		"reachable_count": visited.size(),
		"returned_count": reachable_cells.size(),
		"reachable_cells": reachable_cells,
		"reachable_footholds": reachable_footholds,
		"rightmost_frontier": MapValidator.coord_payload(rightmost, dimension),
		"frontier_candidates": frontier_candidates,
		"first_blocked_gap": first_blocked_gap,
		"note": "Use rightmost_frontier as plan_reachable_map_growth.frontier; it was computed from the real start and real map cells.",
	}


static func _rightmost_frontier_candidates(reachable: Array, filled: Dictionary, region: Dictionary, movement: Dictionary, dimension: int, limit: int) -> Array:
	if reachable.is_empty():
		return []
	var max_x := -2147483648
	for coords_value in reachable:
		var coords: Vector3i = coords_value
		if coords.x > max_x and MapValidator.is_standable(filled, coords, region, movement):
			max_x = coords.x
	var candidates: Array = []
	for coords_value in reachable:
		var coords: Vector3i = coords_value
		if coords.x < max_x - 2:
			continue
		if not MapValidator.is_standable(filled, coords, region, movement):
			continue
		candidates.append(coords)
	candidates.sort_custom(func(a: Vector3i, b: Vector3i) -> bool:
		if a.x == b.x:
			return a.y < b.y
		return a.x > b.x
	)
	var payload: Array = []
	for coords in candidates:
		if payload.size() >= limit:
			break
		payload.append(MapValidator.coord_payload(coords, dimension))
	return payload


static func _first_blocked_gap(filled: Dictionary, rightmost: Vector3i, region: Dictionary, movement: Dictionary, visited: Dictionary, dimension: int) -> Dictionary:
	var probe_limit := max(1, int(movement.get("max_horizontal_gap", movement.get("max_step", 4))))
	for dx in range(1, probe_limit + 1):
		var probe := rightmost + Vector3i(dx, 0, 0)
		if not MapValidator.in_region(probe, region):
			return {"reason": "region_boundary", "after": MapValidator.coord_payload(rightmost, dimension), "probe": MapValidator.coord_payload(probe, dimension)}
		if MapValidator.is_standable(filled, probe, region, movement) and not visited.has(MapValidator.coord_key(probe)):
			return {"reason": "standable_but_unreachable", "after": MapValidator.coord_payload(rightmost, dimension), "probe": MapValidator.coord_payload(probe, dimension)}
	return {}


static func _platform_entry_anchor(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var target_result := _resolve_map_target(input, editor_interface)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	if target.get_class() == "GridMap":
		return {"ok": false, "message": "platform entry anchor requires a 2D TileMap/TileMapLayer", "error_code": "unsupported_map_type"}
	var dimension := 2
	var map_layer := int(input.get("map_layer", 0))
	var region_x := int(input.get("x", 0))
	var sample_width: int = maxi(3, int(input.get("entry_sample_width", 24)))
	var sample_x := int(input.get("entry_sample_x", region_x - sample_width))
	var sample_y := int(input.get("entry_sample_y", input.get("y", 0)))
	var sample_height: int = maxi(4, int(input.get("entry_sample_height", maxi(30, int(input.get("height", 20))))))
	var min_landing_width := maxi(1, int(input.get("min_landing_width", 2)))
	var best_support := Vector3i(-2147483648, 0, 0)
	var valid_supports := {}
	var non_empty_cells := 0
	var blocked_above_cells := 0
	var blocked_above_examples: Array = []
	for y_offset in range(sample_height):
		for x_offset in range(sample_width):
			var coords := Vector3i(sample_x + x_offset, sample_y + y_offset, 0)
			var cell := _read_map_cell(target, coords, dimension, map_layer)
			if _is_empty_cell(target, cell):
				continue
			non_empty_cells += 1
			var above := _read_map_cell(target, coords + Vector3i(0, -1, 0), dimension, map_layer)
			if not _is_empty_cell(target, above):
				blocked_above_cells += 1
				if blocked_above_examples.size() < 8:
					blocked_above_examples.append({"support": {"x": coords.x, "y": coords.y}, "blocked_above": {"x": coords.x, "y": coords.y - 1}})
				continue
			valid_supports["%d:%d" % [coords.x, coords.y]] = coords
	for value in valid_supports.values():
		var support: Vector3i = value
		var has_width := true
		for offset in range(min_landing_width):
			if not valid_supports.has("%d:%d" % [support.x - offset, support.y]):
				has_width = false
				break
		if has_width and (support.x > best_support.x or (support.x == best_support.x and support.y < best_support.y)):
			best_support = support
	if best_support.x == -2147483648:
		var suggested_width: int = sample_width * 2
		var suggested_height: int = sample_height * 2
		return {
			"ok": false,
			"message": "no reachable surface found in the left boundary sample",
			"error_code": "platform_entry_anchor_not_found",
			"sample_rect": {"x": sample_x, "y": sample_y, "width": sample_width, "height": sample_height},
			"non_empty_cells": non_empty_cells,
			"blocked_above_cells": blocked_above_cells,
			"min_landing_width": min_landing_width,
			"blocked_above_examples": blocked_above_examples,
			"suggested_entry_sample": {
				"x": sample_x - sample_width,
				"y": sample_y - int(sample_height / 2),
				"width": suggested_width,
				"height": suggested_height,
			},
			"hint": "If you already know the real rightmost foothold, pass it as frontier or entry_anchor; otherwise retry with suggested_entry_sample_*.",
		}
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"sample_rect": {"x": sample_x, "y": sample_y, "width": sample_width, "height": sample_height},
		"entry_support": {"x": best_support.x, "y": best_support.y},
		"entry_anchor": {"x": best_support.x, "y": best_support.y - 1},
		"min_landing_width": min_landing_width,
		"note": "plan_platform_level must connect the first generated platform from this existing foothold.",
	}


## Deterministically sample naturally spaced map cells for props, resources, enemies, or decor.
static func sample_poisson_points(input: Dictionary, _editor_interface: EditorInterface) -> Dictionary:
	return MapAlgorithms.sample_poisson_points(input)


## Compose reusable map modules into a blueprint/prefab stamping plan.
static func compose_map_blueprint_grammar(input: Dictionary, _editor_interface: EditorInterface) -> Dictionary:
	var result := MapAlgorithms.compose_blueprint_grammar(input)
	var missing_files: Array = []
	for stamp in result.get("stamps", []):
		if not (stamp is Dictionary):
			continue
		var name := MapBlueprints.sanitize_name(str((stamp as Dictionary).get("name", "")))
		if name == "":
			continue
		var path := BLUEPRINTS_DIR + "/" + name + ".json"
		if not FileAccess.file_exists(ProjectSettings.globalize_path(path)):
			missing_files.append({"name": name, "path": path})
	if not missing_files.is_empty():
		result["ok"] = false
		result["missing_blueprints"] = missing_files
	return result


## 压缩或清理空间索引，避免长期使用后整份索引无限增长。
## 可按 dimension/target_path/坐标区域清理，也可只执行 cap 修剪。
static func compact_spatial_index(input: Dictionary, _editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	var parsed := _read_json_resource(SPATIAL_INDEX_PATH)
	if not bool(parsed.get("exists", false)):
		return {"ok": true, "path": SPATIAL_INDEX_PATH, "changed": false, "message": "spatial index does not exist"}
	var before_text := _read_text_file(SPATIAL_INDEX_PATH)
	var data: Dictionary = parsed.get("data", {}).duplicate(true)
	var before_entries := _count_index_entries(data.get("2d", {})) + _count_index_entries(data.get("3d", {}))
	var dimension_filter := str(input.get("dimension", "")).to_lower()
	var target_filter := str(input.get("target_path", "")).strip_edges()
	var clear_all := bool(input.get("clear_all", false))
	var has_region := input.has("x") or input.has("y") or input.has("z") \
		or input.has("width") or input.has("height") or input.has("depth")
	var region := _region_bounds(input)
	var removed := 0
	for branch_key in ["2d", "3d"]:
		if dimension_filter in ["2d", "3d"] and branch_key != dimension_filter:
			continue
		var branch = data.get(branch_key, {})
		if not (branch is Dictionary):
			continue
		for target_path in (branch as Dictionary).keys():
			if target_filter != "" and str(target_path) != target_filter:
				continue
			var entries = branch[target_path]
			if not (entries is Dictionary):
				continue
			var removed_keys: Array = []
			for coord_key in (entries as Dictionary).keys():
				var entry = entries[coord_key]
				if clear_all or (has_region and entry is Dictionary and _entry_in_region(entry, region, 3 if branch_key == "3d" else 2)):
					removed_keys.append(coord_key)
			for key in removed_keys:
				(entries as Dictionary).erase(key)
				removed += 1
			if (entries as Dictionary).is_empty():
				(branch as Dictionary).erase(target_path)
	var cap := max(1, int(input.get("max_entries", MAX_SPATIAL_INDEX_ENTRIES)))
	var pruned := _prune_spatial_index_to_cap(data, cap)
	removed += int(pruned.get("removed", 0))
	var after_entries := _count_index_entries(data.get("2d", {})) + _count_index_entries(data.get("3d", {}))
	if removed == 0 and before_entries == after_entries:
		return {"ok": true, "path": SPATIAL_INDEX_PATH, "changed": false, "entries": after_entries, "max_entries": cap}
	var after_text := JSON.stringify(data, "\t")
	var write_result := _write_json_file(SPATIAL_INDEX_PATH, before_text, after_text, undo_manager)
	if not bool(write_result.get("ok", false)):
		return write_result
	return {
		"ok": true,
		"path": SPATIAL_INDEX_PATH,
		"changed": true,
		"removed": removed,
		"entries_before": before_entries,
		"entries_after": after_entries,
		"max_entries": cap,
	}


## 只读校验一小块地图区域：统计实心/空格，可选做连通性（BFS）检测，返回问题清单。
## 只检测不自动修复——具体怎么补由调用方用 edit_map 决定（自动改场景风险太大）。
static func validate_map_region(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := int(input.get("map_layer", 0))
	var region := MapValidator.region_from_input(input, dimension)
	var allowed_bounds := _bounds_from_input(input, dimension)
	if not _region_within_bounds(region, allowed_bounds):
		return {
			"ok": false,
			"message": "validation region is outside allowed_bounds",
			"error_code": "validation_region_out_of_bounds",
			"region": region,
			"allowed_bounds": allowed_bounds,
		}
	var validate_cells := int(region["width"]) * int(region["height"]) * int(region["depth"])
	if validate_cells > MAX_DESCRIBED_CELLS:
		# 校验区域不能像 describe 那样随便拆——切碎会破坏跨段可达性判定。提示按"路线分段、每段
		# 带自己的 start/goal 校验"的方式缩小，并把支撑行留在区域内，而不是盲目砍宽高。
		return _merge_map_completion_blocker({
			"ok": false,
			"message": "validation region has %d cells, over the %d-cell limit; validate fewer cells per call — split the route into segments and validate each with its own start/goal, keeping each segment's support row inside the region." % [validate_cells, MAX_DESCRIBED_CELLS],
			"error_code": "region_too_large",
			"cells": validate_cells,
			"max_cells": MAX_DESCRIBED_CELLS,
		}, "region_too_large")

	var filled := _collect_filled_cells(target, region, dimension, map_layer)
	var result := {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"region": region,
	}
	var movement := MapValidator.movement_from_input(input, dimension)
	if bool(input.get("check_platform_design", false)) and str(movement.get("model", "grid")) != "leap":
		return _merge_map_completion_blocker({
			"ok": false,
			"message": "platformer map validation must use movement_model='leap'; grid only proves abstract adjacency and cannot validate jumps or gravity.",
			"error_code": "platformer_validation_requires_leap",
		}, "platformer_validation_requires_leap")
	var analysis := MapValidator.validate_region(
		filled,
		region,
		dimension,
		input.get("start", null),
		input.get("goal", null),
		movement,
		str(input.get("path_algorithm", "bfs")),
		input.get("waypoints", null),
		input.get("entrances", null),
		input.get("exits", null)
	)
	result.merge(analysis, true)
	var check_platform_design := bool(input.get("check_platform_design", str(movement.get("model", "grid")) == "leap"))
	if check_platform_design:
		var platform_design := MapValidator.analyze_platform_design(filled, region, dimension, movement, input)
		result["platform_design"] = platform_design
		if not bool(platform_design.get("passed", true)):
			result["passed"] = false
			var design_issues: Array = result.get("issues", [])
			for issue in platform_design.get("issues", []):
				design_issues.append(str(issue))
			result["issues"] = design_issues
			var design_repair: Array = result.get("repair_plan", [])
			for repair in platform_design.get("repair_plan", []):
				design_repair.append(repair)
			result["repair_plan"] = design_repair
	if bool(input.get("check_overlaps", false)) or bool(input.get("check_blocked_objects", false)):
		var overlap_result := _detect_spatial_overlaps(str(target_result.get("path", "")), region, dimension)
		result["overlaps"] = overlap_result.get("overlaps", [])
		if int(overlap_result.get("count", 0)) > 0:
			result["passed"] = false
			var issues: Array = result.get("issues", [])
			issues.append("spatial index contains overlapping entries in the region")
			result["issues"] = issues
			var repair_plan: Array = result.get("repair_plan", [])
			repair_plan.append({
				"type": "overlap_review",
				"action": "move_or_remove_duplicate_object",
				"overlaps": overlap_result.get("overlaps", []),
				"note": "Resolve by moving one object to a nearby free cell with place_map_objects, or deleting the unintended object manually; no scene node is removed automatically.",
			})
			result["repair_plan"] = repair_plan
	if bool(input.get("check_blocked_objects", false)):
		var pressure := _detect_objects_on_blocked_cells(str(target_result.get("path", "")), region, dimension)
		result["blocked_object_overlaps"] = pressure.get("overlaps", [])
		if int(pressure.get("count", 0)) > 0:
			result["passed"] = false
			var pressure_issues: Array = result.get("issues", [])
			pressure_issues.append("one or more objects are placed on water/blocked/obstacle cells")
			result["issues"] = pressure_issues
			var pressure_repair: Array = result.get("repair_plan", [])
			pressure_repair.append({
				"type": "blocked_object_relocate",
				"action": "move_object_to_free_cell",
				"overlaps": pressure.get("overlaps", []),
				"note": "repair_map_region can move indexed object nodes to nearby free cells when repair_blocked_objects=true.",
			})
			result["repair_plan"] = pressure_repair
	# edit_map 编辑后会主动报"毯式图层跟不上了"，validate_map_region 这边同一套逻辑也要跑一遍——
	# 不依赖"刚好是这次编辑造成的"，哪怕落后是上一轮留下的旧问题，校验时一样要能发现。
	var coverage_extent := _layer_used_extent_2d(target, map_layer)
	var coverage_siblings := _sibling_layers_for_coverage(target, map_layer)
	for sibling in coverage_siblings:
		var coverage_sibling_node: Node = (sibling as Dictionary)["node"]
		var coverage_sibling_map_layer := int((sibling as Dictionary)["map_layer"])
		(sibling as Dictionary)["extent"] = _layer_used_extent_2d(coverage_sibling_node, coverage_sibling_map_layer)
		(sibling as Dictionary)["columns"] = _used_columns_2d(coverage_sibling_node, coverage_sibling_map_layer)
	var coverage_target_columns := _used_columns_2d(target, map_layer)
	var coverage_gaps := _compute_coverage_gaps(coverage_extent, coverage_extent, coverage_siblings, coverage_target_columns)
	result["layer_coverage_gaps"] = coverage_gaps
	if not coverage_gaps.is_empty():
		result["passed"] = false
		var coverage_issues: Array = result.get("issues", [])
		coverage_issues.append("one or more full-coverage layers (background/sky/water etc.) fall short of the map's overall extent")
		result["issues"] = coverage_issues
	var route_checked := (input.has("start") and (input.has("goal") or input.has("waypoints"))) or (input.has("entrances") and input.has("exits"))
	var completion_issues: Array = result.get("issues", [])
	if not route_checked:
		completion_issues.append("route_validation_not_checked: pass start plus goal/waypoints or entrances/exits before treating a route/map edit as complete")
	result["issues"] = completion_issues
	result["completion_allowed"] = bool(result.get("passed", false)) and route_checked
	result["blocking_completion"] = not bool(result["completion_allowed"])
	result["validation"] = {
		"passed": bool(result.get("passed", false)),
		"blocking_completion": bool(result["blocking_completion"]),
		"issues": completion_issues,
	}
	return result


## 根据 validate_map_region 的连通性修复计划应用最小 corridor 修复。
## 默认玩法里空格可走时会清空 start->goal 的曼哈顿走廊；平台类/实心可走时可传 fill_* 参数填路。
static func repair_map_region(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	if (bool(input.get("repair_overlaps", false)) or bool(input.get("repair_blocked_objects", false))) and not (input.has("start") and input.has("goal")):
		return _repair_spatial_object_issues(input, editor_interface, undo_manager)
	if not input.has("start") or not input.has("goal"):
		return {"ok": false, "message": "start and goal are required for connectivity repair; pass repair_overlaps/repair_blocked_objects for object repair", "error_code": "invalid_repair_request"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := int(input.get("map_layer", 0))
	var region := MapValidator.region_from_input(input, dimension)
	var repair_cells := int(region["width"]) * int(region["height"]) * int(region["depth"])
	if repair_cells > MAX_DESCRIBED_CELLS:
		return _merge_map_completion_blocker({
			"ok": false,
			"message": "repair region has %d cells, over the %d-cell limit; repair fewer cells per call — repair one route segment at a time with its own start/goal." % [repair_cells, MAX_DESCRIBED_CELLS],
			"error_code": "region_too_large",
			"cells": repair_cells,
			"max_cells": MAX_DESCRIBED_CELLS,
		}, "region_too_large")
	var filled := _collect_filled_cells(target, region, dimension, map_layer)
	var analysis := MapValidator.validate_region(
		filled,
		region,
		dimension,
		input.get("start", null),
		input.get("goal", null),
		MapValidator.movement_from_input(input, dimension),
		str(input.get("path_algorithm", "astar")),
		input.get("waypoints", null),
		input.get("entrances", null),
		input.get("exits", null)
	)
	if bool(analysis.get("passed", false)):
		return {
			"ok": true,
			"changed": false,
			"completion_allowed": true,
			"blocking_completion": false,
			"message": "Region already passes validation",
			"validation": analysis,
		}
	var cells_result := _repair_cells_from_plan(input, target, dimension, map_layer, analysis.get("repair_plan", []))
	if not bool(cells_result.get("ok", false)):
		return cells_result
	var after: Array = cells_result.get("cells", [])
	if after.is_empty():
		return {"ok": false, "message": "No repair cells were produced", "error_code": "empty_repair_plan", "validation": analysis}
	var before: Array = []
	var touched := {}
	for cell in after:
		var key := _cell_key(cell, dimension, map_layer)
		if touched.has(key):
			continue
		before.append(_read_map_cell(target, cell["coords"], dimension, map_layer))
		touched[key] = true
	var index_result := _maybe_update_spatial_index(input, undo_manager, target, str(target_result.get("path", "")), dimension, after)
	if not bool(index_result.get("ok", true)):
		return index_result
	if undo_manager != null:
		undo_manager.record_tile_cells(target, before, after)
	else:
		for cell in after:
			_apply_map_cell(target, cell)
	# 修完立刻在新状态上重跑一次校验，把"应用了修复"和"修复真的生效了"分开：以前不论是否真修好
	# 都返回 ok，模型据此当作已解决继续往下，结果（日志里就发生过）repair 方案本身根本没让它通过。
	var filled_after := _collect_filled_cells(target, region, dimension, map_layer)
	var validation_after := MapValidator.validate_region(
		filled_after,
		region,
		dimension,
		input.get("start", null),
		input.get("goal", null),
		MapValidator.movement_from_input(input, dimension),
		str(input.get("path_algorithm", "astar")),
		input.get("waypoints", null),
		input.get("entrances", null),
		input.get("exits", null)
	)
	var repaired := bool(validation_after.get("passed", false))
	return {
		"ok": true,
		"changed": true,
		"repaired": repaired,
		"completion_allowed": repaired,
		"blocking_completion": not repaired,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"cells": after.size(),
		"validation_before": analysis,
		"validation_after": validation_after,
		"message": (
			"Repair applied and the region now passes validation."
			if repaired else
			"Repair applied but the region STILL fails validation — do not treat it as fixed. Read validation_after.reason/repair_plan (and re-read describe_map_region) and resolve the remaining failure before continuing."
		),
		"spatial_index": index_result,
	}


## 用 FastNoiseLite 在一块区域上采样归一化噪声值（0..1），供 agent 做"密度/自然分布"决策
## （树木、岩石、草地变化等）。纯计算，不读写场景，不需确认。固定 seed 可复现。
static func sample_noise_grid(input: Dictionary, _editor_interface: EditorInterface) -> Dictionary:
	var dimension := 3 if str(input.get("dimension", "2d")) == "3d" else 2
	var width := max(1, int(input.get("width", 1)))
	var height := max(1, int(input.get("height", 1)))
	var depth := max(1, int(input.get("depth", 1))) if dimension == 3 else 1
	if width * height * depth > MAX_NOISE_CELLS:
		return {
			"ok": false,
			"message": "noise grid exceeds the %d-sample limit; request a smaller grid" % MAX_NOISE_CELLS,
			"error_code": "noise_grid_too_large",
		}
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var z := int(input.get("z", 0))

	var noise := FastNoiseLite.new()
	noise.seed = int(input.get("seed", 0))
	noise.frequency = float(input.get("frequency", 0.05))
	noise.noise_type = _noise_type_from_name(str(input.get("noise_type", "simplex")))
	var octaves := int(input.get("octaves", 0))
	if octaves > 0:
		noise.fractal_octaves = octaves

	var rows: Array = []
	if dimension == 3:
		for dz in range(depth):
			var plane: Array = []
			for dy in range(height):
				var row: Array = []
				for dx in range(width):
					row.append(_normalize_noise(noise.get_noise_3d(x + dx, y + dy, z + dz)))
				plane.append(row)
			rows.append(plane)
	else:
		for dy in range(height):
			var row: Array = []
			for dx in range(width):
				row.append(_normalize_noise(noise.get_noise_2d(x + dx, y + dy)))
			rows.append(row)
	return {
		"ok": true,
		"dimension": dimension,
		"origin": {"x": x, "y": y, "z": z},
		"width": width,
		"height": height,
		"depth": depth,
		"seed": noise.seed,
		"frequency": noise.frequency,
		"noise_type": str(input.get("noise_type", "simplex")),
		"values": rows,
		"note": "values are normalized to 0..1; pick a threshold to convert to placement density",
	}


## 把一块现有区域里非空的瓦片/网格存成可复用模板
## res://.ai_agent_service/map_agent/blueprints/<name>.json，记录相对坐标 + 真实资源引用。
static func save_map_blueprint(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var name := MapBlueprints.sanitize_name(str(input.get("name", "")))
	if name == "":
		return {"ok": false, "message": "name is required and must contain letters/digits/_/-", "error_code": "invalid_blueprint_name"}
	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := int(input.get("map_layer", 0))
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var z := int(input.get("z", 0)) if dimension == 3 else 0
	var width := max(1, int(input.get("width", 1)))
	var height := max(1, int(input.get("height", 1)))
	var depth := max(1, int(input.get("depth", 1))) if dimension == 3 else 1
	if width * height * depth > MAX_DESCRIBED_CELLS:
		return {
			"ok": false,
			"message": "blueprint region exceeds the %d-cell limit; capture a smaller region" % MAX_DESCRIBED_CELLS,
			"error_code": "region_too_large",
		}

	var target_path := str(target_result.get("path", ""))
	var blueprint := MapBlueprints.build_blueprint(
		name,
		dimension,
		map_layer,
		Vector3i(x, y, z),
		width,
		height,
		depth,
		input.get("tags", []) if input.get("tags", []) is Array else [],
		func(coords: Vector3i) -> Dictionary:
			var cell := _read_map_cell(target, coords, dimension, map_layer)
			_apply_spatial_metadata_to_cell(target_path, coords, dimension, cell)
			return cell,
		func(cell: Dictionary) -> bool:
			return _is_empty_cell(target, cell)
	)
	var path := BLUEPRINTS_DIR + "/" + name + ".json"
	var before_text := _read_text_file(path)
	var after_text := JSON.stringify(blueprint, "\t")
	var write_result := _write_json_file(path, before_text, after_text, undo_manager)
	if not bool(write_result.get("ok", false)):
		return write_result
	return {"ok": true, "path": path, "name": name, "dimension": dimension, "cell_count": int(blueprint.get("cell_count", 0))}


## 把已保存的模板平移到目标原点重新铺一遍，复用真实资源引用。可选写空间索引。
## 走 Undo 预览批次，可撤销。
static func apply_map_blueprint(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var name := MapBlueprints.sanitize_name(str(input.get("name", "")))
	if name == "":
		return {"ok": false, "message": "name is required", "error_code": "invalid_blueprint_name"}
	var path := BLUEPRINTS_DIR + "/" + name + ".json"
	var parsed := _read_json_resource(path)
	if not bool(parsed.get("exists", false)):
		return {"ok": false, "message": "blueprint not found: " + path, "error_code": "blueprint_not_found"}
	var blueprint: Dictionary = parsed.get("data", {})
	var ops = blueprint.get("ops", [])
	if not (ops is Array) or (ops as Array).is_empty():
		return {"ok": false, "message": "blueprint has no ops", "error_code": "blueprint_empty"}

	var target_result := _resolve_map_target(input, editor_interface, true)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var dimension := 3 if target.get_class() == "GridMap" else 2
	var blueprint_dimension := MapBlueprints.blueprint_dimension(blueprint)
	if blueprint_dimension != dimension:
		return {
			"ok": false,
			"message": "blueprint is %dD but target is %dD" % [blueprint_dimension, dimension],
			"error_code": "blueprint_dimension_mismatch",
		}
	var map_layer := int(input.get("map_layer", 0))
	var origin := Vector3i(
		int(input.get("x", 0)),
		int(input.get("y", 0)),
		int(input.get("z", 0)) if dimension == 3 else 0
	)

	var before: Array = []
	var after: Array = MapBlueprints.build_cells_from_blueprint(blueprint, dimension, map_layer, origin)
	var touched := {}
	if after.size() > MAX_EDITED_CELLS:
		return {"ok": false, "message": "blueprint application exceeds the cell safety limit", "error_code": "map_edit_too_large"}
	for cell in after:
		var key := _cell_key(cell, dimension, map_layer)
		if not touched.has(key):
			before.append(_read_map_cell(target, cell["coords"], dimension, map_layer))
			touched[key] = true

	# 同 edit_map：先写空间索引，索引失败就在动瓦片前返回，避免半截状态。
	var index_result := _maybe_update_spatial_index(input, undo_manager, target, str(target_result.get("path", "")), dimension, after)
	if not bool(index_result.get("ok", true)):
		return index_result
	if undo_manager != null:
		undo_manager.record_tile_cells(target, before, after)
	else:
		for cell in after:
			_apply_map_cell(target, cell)
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"name": name,
		"cells": after.size(),
		"spatial_index": index_result,
	}


## 创建/补齐文档约定的 2D/3D 标准地图节点结构。
static func ensure_standard_map_layers(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	return MapLayerScaffold.ensure_standard_layers(input, editor_interface, undo_manager)


## 读取 res:// 文本文件原始内容，文件不存在时返回空串（给 Undo before_text 用）。
static func _read_text_file(path: String) -> String:
	var absolute := ProjectSettings.globalize_path(path)
	if not FileAccess.file_exists(absolute):
		return ""
	return FileAccess.get_file_as_string(absolute)


## 写入 JSON 文本：优先走 undo_manager 进同一个预览/撤销批次，否则退化为直接写盘。
static func _write_json_file(path: String, before_text: String, after_text: String, undo_manager: Node) -> Dictionary:
	if undo_manager != null and undo_manager.has_method("record_file_write"):
		var before_exists := FileAccess.file_exists(ProjectSettings.globalize_path(path))
		var error: Error = undo_manager.record_file_write(path, before_text, after_text, before_exists)
		if error != OK:
			return {"ok": false, "message": "failed to write " + path, "error_code": "file_write_failed", "error": error}
		return {"ok": true}
	var dir_error := DirAccess.make_dir_recursive_absolute(ProjectSettings.globalize_path(path.get_base_dir()))
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		return {"ok": false, "message": "failed to create directory for " + path, "error_code": "file_write_failed", "error": dir_error}
	var file := FileAccess.open(ProjectSettings.globalize_path(path), FileAccess.WRITE)
	if file == null:
		return {"ok": false, "message": "failed to open " + path, "error_code": "file_write_failed", "error": FileAccess.get_open_error()}
	file.store_string(after_text)
	return {"ok": true}


static func _region_bounds(input: Dictionary) -> Dictionary:
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var z := int(input.get("z", 0))
	var width := max(1, int(input.get("width", 1)))
	var height := max(1, int(input.get("height", 1)))
	var depth := max(1, int(input.get("depth", 1)))
	return {
		"min_x": x, "max_x": x + width - 1,
		"min_y": y, "max_y": y + height - 1,
		"min_z": z, "max_z": z + depth - 1,
	}


static func _bounds_from_input(input: Dictionary, dimension: int) -> Dictionary:
	var raw = input.get("allowed_bounds", {})
	if not (raw is Dictionary):
		return {}
	var bounds: Dictionary = raw
	if not (bounds.has("width") and bounds.has("height")):
		return {}
	var x := int(bounds.get("x", 0))
	var y := int(bounds.get("y", 0))
	var z := int(bounds.get("z", 0)) if dimension == 3 else 0
	var width := max(1, int(bounds.get("width", 1)))
	var height := max(1, int(bounds.get("height", 1)))
	var depth := max(1, int(bounds.get("depth", 1))) if dimension == 3 else 1
	return {
		"x": x, "y": y, "z": z,
		"width": width, "height": height, "depth": depth,
		"min_x": x, "max_x": x + width - 1,
		"min_y": y, "max_y": y + height - 1,
		"min_z": z, "max_z": z + depth - 1,
	}


static func _cell_within_bounds(coords: Vector3i, bounds: Dictionary) -> bool:
	if bounds.is_empty():
		return true
	return coords.x >= int(bounds["min_x"]) and coords.x <= int(bounds["max_x"]) \
		and coords.y >= int(bounds["min_y"]) and coords.y <= int(bounds["max_y"]) \
		and coords.z >= int(bounds["min_z"]) and coords.z <= int(bounds["max_z"])


static func _region_within_bounds(region: Dictionary, bounds: Dictionary) -> bool:
	if bounds.is_empty():
		return true
	return int(region["min_x"]) >= int(bounds["min_x"]) and int(region["max_x"]) <= int(bounds["max_x"]) \
		and int(region["min_y"]) >= int(bounds["min_y"]) and int(region["max_y"]) <= int(bounds["max_y"]) \
		and int(region["min_z"]) >= int(bounds["min_z"]) and int(region["max_z"]) <= int(bounds["max_z"])


static func _detect_spatial_overlaps(target_path: String, region: Dictionary, dimension: int) -> Dictionary:
	var parsed := _read_json_resource(SPATIAL_INDEX_PATH)
	var by_coord := {}
	if not bool(parsed.get("exists", false)):
		return {"count": 0, "overlaps": []}
	var data = parsed.get("data", {})
	if not (data is Dictionary):
		return {"count": 0, "overlaps": []}
	var branch = (data as Dictionary).get("3d" if dimension == 3 else "2d", {})
	if not (branch is Dictionary):
		return {"count": 0, "overlaps": []}
	var targets: Array = [target_path] if target_path != "" else (branch as Dictionary).keys()
	for target in targets:
		var entries = (branch as Dictionary).get(target, {})
		if not (entries is Dictionary):
			continue
		for key in (entries as Dictionary).keys():
			var entry = (entries as Dictionary)[key]
			if not (entry is Dictionary):
				continue
			if not _is_object_index_entry(entry):
				continue
			if not _entry_in_region(entry, region, dimension):
				continue
			var coords = (entry as Dictionary).get("coords", {})
			var coord_key := str(key)
			if coords is Dictionary:
				coord_key = "%d,%d,%d" % [int(coords.get("x", 0)), int(coords.get("y", 0)), int(coords.get("z", 0))]
			if not by_coord.has(coord_key):
				by_coord[coord_key] = []
			(by_coord[coord_key] as Array).append(entry)
	var overlaps: Array = []
	for key in by_coord.keys():
		var entries_at_coord: Array = by_coord[key]
		if entries_at_coord.size() > 1:
			overlaps.append({"coord_key": key, "entries": entries_at_coord})
	return {"count": overlaps.size(), "overlaps": overlaps}


static func _detect_objects_on_blocked_cells(target_path: String, region: Dictionary, dimension: int) -> Dictionary:
	var entries := _spatial_entries_in_region(target_path, region, dimension)
	var blocked_by_coord := {}
	var objects_by_coord := {}
	for entry in entries:
		var coords: Dictionary = entry.get("coords", {})
		var coord_key := MapValidator.coord_key(MapValidator.coord_from_input(coords, dimension))
		if _is_object_index_entry(entry):
			if not objects_by_coord.has(coord_key):
				objects_by_coord[coord_key] = []
			(objects_by_coord[coord_key] as Array).append(entry)
		elif _is_blocked_index_entry(entry):
			blocked_by_coord[coord_key] = entry
	var overlaps: Array = []
	for coord_key in objects_by_coord.keys():
		if blocked_by_coord.has(coord_key):
			overlaps.append({
				"coord_key": coord_key,
				"objects": objects_by_coord[coord_key],
				"blocking_entry": blocked_by_coord[coord_key],
			})
	return {"count": overlaps.size(), "overlaps": overlaps}


static func _spatial_entries_in_region(target_path: String, region: Dictionary, dimension: int) -> Array:
	var parsed := _read_json_resource(SPATIAL_INDEX_PATH)
	var entries_out: Array = []
	if not bool(parsed.get("exists", false)):
		return entries_out
	var data = parsed.get("data", {})
	if not (data is Dictionary):
		return entries_out
	var branch = (data as Dictionary).get("3d" if dimension == 3 else "2d", {})
	if not (branch is Dictionary):
		return entries_out
	var targets: Array = [target_path] if target_path != "" else (branch as Dictionary).keys()
	for target in targets:
		var entries = (branch as Dictionary).get(target, {})
		if not (entries is Dictionary):
			continue
		for key in (entries as Dictionary).keys():
			var entry = (entries as Dictionary)[key]
			if not (entry is Dictionary):
				continue
			if _entry_in_region(entry, region, dimension):
				var copy: Dictionary = (entry as Dictionary).duplicate(true)
				copy["_index_key"] = str(key)
				copy["_target_path"] = str(target)
				entries_out.append(copy)
	return entries_out


static func _is_object_index_entry(entry: Dictionary) -> bool:
	return str(entry.get("kind", "")) == "object" or str(entry.get("scene_path", "")) != ""


static func _is_blocked_index_entry(entry: Dictionary) -> bool:
	var semantic_layer := str(entry.get("semantic_layer", ""))
	var tags = entry.get("tags", [])
	if semantic_layer in ["water", "obstacle", "blocked"]:
		return true
	return tags is Array and ((tags as Array).has("water") or (tags as Array).has("blocked") or (tags as Array).has("obstacle"))


static func _entry_matches(entry: Dictionary, want_tags: Array, want_resource: String, want_layer: String, want_visual_group: String = "") -> bool:
	if want_resource != "":
		var entry_resource := str(entry.get("resource", entry.get("resource_key", "")))
		if entry_resource != want_resource:
			return false
	if want_layer != "" and str(entry.get("semantic_layer", "")) != want_layer:
		return false
	if want_visual_group != "":
		var entry_group := _visual_group_id_from_data(entry)
		if entry_group != want_visual_group:
			return false
	if not want_tags.is_empty():
		var entry_tags = entry.get("tags", [])
		if not (entry_tags is Array):
			return false
		var found := false
		for tag in want_tags:
			if (entry_tags as Array).has(tag):
				found = true
				break
		if not found:
			return false
	return true


static func _entry_in_region(entry: Dictionary, region: Dictionary, dimension: int) -> bool:
	var coords = entry.get("coords", {})
	if not (coords is Dictionary):
		return true
	var cx := int(coords.get("x", 0))
	var cy := int(coords.get("y", 0))
	if cx < int(region["min_x"]) or cx > int(region["max_x"]):
		return false
	if cy < int(region["min_y"]) or cy > int(region["max_y"]):
		return false
	if dimension == 3:
		var cz := int(coords.get("z", 0))
		if cz < int(region["min_z"]) or cz > int(region["max_z"]):
			return false
	return true


static func _coverage_sibling_for_gap(siblings: Array, gap: Dictionary) -> Dictionary:
	var wanted_layer := int(gap.get("map_layer", -1))
	var wanted_label := str(gap.get("layer", ""))
	for sibling_value in siblings:
		var sibling: Dictionary = sibling_value
		if int(sibling.get("map_layer", -2)) == wanted_layer and (wanted_label == "" or str(sibling.get("label", "")) == wanted_label):
			return sibling
	for sibling_value in siblings:
		var sibling: Dictionary = sibling_value
		if int(sibling.get("map_layer", -2)) == wanted_layer:
			return sibling
	for sibling_value in siblings:
		var sibling: Dictionary = sibling_value
		if wanted_label != "" and str(sibling.get("label", "")) == wanted_label:
			return sibling
	return {}


static func _coverage_repair_columns(gap: Dictionary, union_extent: Dictionary, target_columns: Dictionary) -> Array:
	var columns := {}
	var shortfall: Dictionary = gap.get("shortfall_cells", {})
	var current: Dictionary = gap.get("current_extent", {})
	if not shortfall.is_empty() and not current.is_empty():
		if shortfall.has("left"):
			for x in range(int(union_extent["min_x"]), int(current["min_x"])):
				columns[x] = true
		if shortfall.has("right"):
			for x in range(int(current["max_x"]) + 1, int(union_extent["max_x"]) + 1):
				columns[x] = true
		if shortfall.has("top") or shortfall.has("bottom"):
			for key in target_columns.keys():
				columns[int(key)] = true
	var holes = gap.get("interior_holes_x", [])
	if holes is Array:
		for hole_value in holes:
			if not (hole_value is Dictionary):
				continue
			var hole: Dictionary = hole_value
			for x in range(int(hole.get("from", 0)), int(hole.get("to", 0)) + 1):
				columns[x] = true
	var out := columns.keys()
	out.sort()
	return out


static func _normalize_noise(value: float) -> float:
	return clampf((value + 1.0) * 0.5, 0.0, 1.0)


static func _noise_type_from_name(noise_name: String) -> int:
	match noise_name.to_lower():
		"perlin":
			return FastNoiseLite.TYPE_PERLIN
		"value":
			return FastNoiseLite.TYPE_VALUE
		"value_cubic":
			return FastNoiseLite.TYPE_VALUE_CUBIC
		"cellular":
			return FastNoiseLite.TYPE_CELLULAR
		"simplex_smooth":
			return FastNoiseLite.TYPE_SIMPLEX_SMOOTH
		_:
			return FastNoiseLite.TYPE_SIMPLEX

static func fill_rect(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	var selected := describe_selection(editor_interface)
	if not bool(selected.get("ok", false)):
		return selected
	var layer := _selected_tilemap_layer(editor_interface)
	if layer == null or not layer.has_method("set_cell"):
		return {"ok": false, "message": "Selected TileMapLayer cannot set cells"}

	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var width := max(0, int(input.get("width", 0)))
	var height := max(0, int(input.get("height", 0)))
	var source_id := int(input.get("source_id", -1))
	var atlas := Vector2i(int(input.get("atlas_x", -1)), int(input.get("atlas_y", -1)))
	var alt := int(input.get("alternative_tile", 0))

	var before: Array = []
	var after: Array = []
	for yy in range(y, y + height):
		for xx in range(x, x + width):
			var coords := Vector2i(xx, yy)
			before.append(_read_cell(layer, coords))
			after.append({
				"coords": coords,
				"source_id": source_id,
				"atlas_coords": atlas,
				"alternative_tile": alt
			})

	if undo_manager != null:
		undo_manager.record_tile_cells(layer, before, after)
	else:
		for cell in after:
			layer.call("set_cell", cell["coords"], cell["source_id"], cell["atlas_coords"], cell["alternative_tile"])
	return {"ok": true, "target": selected, "cells": after.size()}


static func paint_from_image_grid(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	var selected := describe_selection(editor_interface)
	if not bool(selected.get("ok", false)):
		return selected
	var layer := _selected_tilemap_layer(editor_interface)
	if layer == null or not layer.has_method("set_cell"):
		return {"ok": false, "message": "Selected TileMapLayer cannot set cells"}

	var image_path := PathUtils.to_res_path(str(input.get("image_path", "")))
	if image_path == "":
		return {"ok": false, "message": "image_path is required"}
	var image := Image.new()
	var err := image.load(image_path)
	if err != OK:
		return {"ok": false, "message": "failed to load image", "path": image_path, "error": err}
	var palette: Array = input.get("palette", [])
	if palette.is_empty():
		return {"ok": false, "message": "palette is required"}

	var origin := Vector2i(int(input.get("origin_x", 0)), int(input.get("origin_y", 0)))
	var width = min(image.get_width(), max(1, int(input.get("max_width", image.get_width()))))
	var height = min(image.get_height(), max(1, int(input.get("max_height", image.get_height()))))
	var before: Array = []
	var after: Array = []
	for y in range(height):
		for x in range(width):
			var tile := _nearest_palette_tile(image.get_pixel(x, y), palette)
			if tile.is_empty():
				continue
			var coords := origin + Vector2i(x, y)
			before.append(_read_cell(layer, coords))
			after.append({
				"coords": coords,
				"source_id": int(tile.get("source_id", -1)),
				"atlas_coords": Vector2i(int(tile.get("atlas_x", -1)), int(tile.get("atlas_y", -1))),
				"alternative_tile": int(tile.get("alternative_tile", 0))
			})

	if undo_manager != null:
		undo_manager.record_tile_cells(layer, before, after)
	else:
		for cell in after:
			layer.call("set_cell", cell["coords"], cell["source_id"], cell["atlas_coords"], cell["alternative_tile"])
	return {
		"ok": true,
		"target": selected,
		"image_path": image_path,
		"width": width,
		"height": height,
		"cells": after.size()
	}


static func _selected_tilemap_layer(editor_interface: EditorInterface) -> Node:
	for node in editor_interface.get_selection().get_selected_nodes():
		if node != null and node.get_class() == "TileMapLayer":
			return node
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return null
	var found: Array = []
	_collect_tilemap_layers(root, found)
	if found.size() == 1:
		return found[0]
	return null


static func _read_cell(layer: Node, coords: Vector2i) -> Dictionary:
	var source_id := -1
	var atlas := Vector2i(-1, -1)
	var alt := 0
	if layer.has_method("get_cell_source_id"):
		source_id = int(layer.call("get_cell_source_id", coords))
	if layer.has_method("get_cell_atlas_coords"):
		atlas = layer.call("get_cell_atlas_coords", coords)
	if layer.has_method("get_cell_alternative_tile"):
		alt = int(layer.call("get_cell_alternative_tile", coords))
	return {
		"coords": coords,
		"source_id": source_id,
		"atlas_coords": atlas,
		"alternative_tile": alt
	}


static func _nearest_palette_tile(color: Color, palette: Array) -> Dictionary:
	var best: Dictionary = {}
	var best_score := INF
	for item in palette:
		if not (item is Dictionary):
			continue
		var candidate: Dictionary = item
		var parsed := _parse_hex_color(str(candidate.get("hex", "#000000")))
		var score := pow(color.r - parsed.r, 2.0) + pow(color.g - parsed.g, 2.0) + pow(color.b - parsed.b, 2.0)
		if score < best_score:
			best_score = score
			best = candidate
	return best


static func _parse_hex_color(raw: String) -> Color:
	var hex := raw.strip_edges().trim_prefix("#")
	if hex.length() < 6:
		return Color.BLACK
	var r := _hex_byte(hex.substr(0, 2)) / 255.0
	var g := _hex_byte(hex.substr(2, 2)) / 255.0
	var b := _hex_byte(hex.substr(4, 2)) / 255.0
	return Color(r, g, b, 1.0)


static func _hex_byte(pair: String) -> float:
	var value := 0
	for i in range(min(2, pair.length())):
		var c := pair.unicode_at(i)
		value *= 16
		if c >= 48 and c <= 57:
			value += c - 48
		elif c >= 65 and c <= 70:
			value += c - 55
		elif c >= 97 and c <= 102:
			value += c - 87
	return float(value)
