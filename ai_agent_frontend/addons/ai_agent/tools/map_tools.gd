@tool
extends RefCounted

const MAX_DESCRIBED_CELLS := 400
## 单次观察最多扫描的 cell 数；超出时仅观察一个有界前缀并明确标记截断。
const MAX_OBSERVED_CELLS := 3200
## 紧凑行程摘要同样有独立上限，避免每个 cell 都不同时撑爆模型上下文。
const MAX_SUMMARY_RUNS := 256


static func describe_selection(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var root := editor_interface.get_edited_scene_root()
	for node in editor_interface.get_selection().get_selected_nodes():
		if node != null and _is_map_node(node):
			var path := str(root.get_path_to(node)) if root != null else str(node.get_path())
			return _describe_selected_map_node(node, path, false)

	if root == null:
		return _unsupported_selection_result([])
	var found: Array = []
	_collect_map_nodes(root, found)
	if found.size() == 1:
		var node: Node = found[0]
		return _describe_selected_map_node(node, str(root.get_path_to(node)), true)
	if found.size() > 1:
		var paths: Array[String] = []
		for n in found:
			paths.append(str(root.get_path_to(n)))
		return _unsupported_selection_result(paths, "Multiple compatible map nodes found; select one")
	return _unsupported_selection_result([])


static func _describe_selected_map_node(node: Node, path: String, auto_detected: bool) -> Dictionary:
	var node_type := node.get_class()
	var dimension := 3 if node_type == "GridMap" else 2
	var bounds_fields: Array[String] = ["x", "y", "z", "width", "height", "depth"] if dimension == 3 else ["x", "y", "width", "height"]
	var requires_map_layer := node_type in ["TileMapLayer", "TileMap"]
	var result := {
		"ok": true,
		"path": path,
		"type": node_type,
		"dimension": dimension,
		"region_bounds_fields": bounds_fields,
		"map_layer_applicable": requires_map_layer,
		"next_step": {
			"describe_map_region": {"target_path": path, "bounds_fields": bounds_fields},
			"capture_viewport_screenshot": {"mode": "3d" if dimension == 3 else "2d", "target_type": "map_region", "bounds_fields": bounds_fields},
		},
	}
	if requires_map_layer:
		result["next_step"]["describe_map_region"]["map_layer"] = "select explicitly for TileMap; TileMapLayer uses 0"
		result["next_step"]["capture_viewport_screenshot"]["map_layer"] = "required (TileMapLayer uses 0)"
	else:
		result["next_step"]["describe_map_region"]["map_layer"] = "not applicable"
		result["next_step"]["capture_viewport_screenshot"]["map_layer"] = "not applicable"
	if auto_detected:
		result["auto_detected"] = true
	return result


static func _unsupported_selection_result(candidates: Array[String], message := "Select a TileMapLayer, TileMap, or GridMap first") -> Dictionary:
	return {
		"ok": false,
		"message": message,
		"error_code": "unsupported_selection",
		"supported_types": ["TileMapLayer", "TileMap", "GridMap"],
		"candidates": candidates,
	}


## 只读地查询一小块现有地图区域的真实瓦片/网格数据，外加地图节点自身的坐标系数。
## 用于在扩建/延伸地形前先弄清楚现有内容到底长什么样、世界坐标怎么换算，而不是
## 靠 tile_catalog 里"有哪些瓦片可用"自己瞎拼，或者假设 origin/tile_size 是常量。
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
	var total_cells: int = width * height * depth
	var observed_extent := _bounded_observed_extent(width, height, depth, dimension)
	var observed_width := int(observed_extent["width"])
	var observed_height := int(observed_extent["height"])
	var observed_depth := int(observed_extent["depth"])
	var cells: Array = []
	var row_runs: Array = []
	var summary_truncated := false
	for z_offset in range(observed_depth):
		for y_offset in range(observed_height):
			var current_run := {}
			for x_offset in range(observed_width):
				var coords := origin + Vector3i(x_offset, y_offset, z_offset)
				var raw_cell := _read_map_cell(target, coords, dimension, map_layer)
				if cells.size() < MAX_DESCRIBED_CELLS:
					cells.append(_describe_safe_cell(raw_cell, dimension))
				var run_key := _cell_run_key(raw_cell, dimension)
				if current_run.is_empty() or str(current_run.get("key", "")) != run_key:
					if not current_run.is_empty():
						summary_truncated = _append_row_run(row_runs, current_run) or summary_truncated
					current_run = {
						"key": run_key,
						"cell": raw_cell,
						"x_start": coords.x,
						"x_end": coords.x,
						"y": coords.y,
						"z": coords.z,
					}
				else:
					current_run["x_end"] = coords.x
			if not current_run.is_empty():
				summary_truncated = _append_row_run(row_runs, current_run) or summary_truncated

	var result := {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"cells": cells,
		"row_runs": row_runs,
		"requested_cells": total_cells,
		"observed_cells": observed_width * observed_height * observed_depth,
		"detail_limit": MAX_DESCRIBED_CELLS,
		"summary_run_limit": MAX_SUMMARY_RUNS,
		"requested_bounds": _bounds_dictionary(origin, width, height, depth, dimension),
		"observed_bounds": _bounds_dictionary(origin, observed_width, observed_height, observed_depth, dimension),
	}
	var observation_truncated := total_cells > int(result["observed_cells"])
	var detail_truncated := total_cells > MAX_DESCRIBED_CELLS
	result["truncated"] = observation_truncated or detail_truncated or summary_truncated
	if bool(result["truncated"]):
		result["next_query"] = _next_query_hint(
			origin,
			width,
			height,
			depth,
			observed_width,
			observed_height,
			observed_depth,
			dimension,
			map_layer
		)
	if target is Node2D:
		var position_2d := (target as Node2D).position
		result["node_position"] = {"x": position_2d.x, "y": position_2d.y}
	elif target is Node3D:
		var position_3d := (target as Node3D).position
		result["node_position"] = {"x": position_3d.x, "y": position_3d.y, "z": position_3d.z}
	if dimension == 3 and "cell_size" in target:
		var cell_size: Vector3 = target.cell_size
		result["cell_size"] = {"x": cell_size.x, "y": cell_size.y, "z": cell_size.z}
	elif dimension == 2 and "tile_set" in target and target.tile_set != null:
		var tile_size: Vector2i = target.tile_set.tile_size
		result["tile_size"] = {"x": tile_size.x, "y": tile_size.y}
	if target.get_class() == "TileMap":
		result["layers"] = _describe_tilemap_layers(target)
	return result


## 将请求范围收敛到可扫描预算，始终返回起点连续的已观察矩形/体积。
static func _bounded_observed_extent(width: int, height: int, depth: int, dimension: int) -> Dictionary:
	if width * height * depth <= MAX_OBSERVED_CELLS:
		return {"width": width, "height": height, "depth": depth}
	if dimension == 3:
		var bounded_depth := mini(depth, maxi(1, MAX_OBSERVED_CELLS / (width * height)))
		return {"width": width, "height": height, "depth": bounded_depth}
	if width <= MAX_OBSERVED_CELLS:
		return {"width": width, "height": mini(height, maxi(1, MAX_OBSERVED_CELLS / width)), "depth": 1}
	return {"width": MAX_OBSERVED_CELLS, "height": 1, "depth": 1}


static func _bounds_dictionary(origin: Vector3i, width: int, height: int, depth: int, dimension: int) -> Dictionary:
	var result := {
		"x": origin.x,
		"y": origin.y,
		"width": width,
		"height": height,
	}
	if dimension == 3:
		result["z"] = origin.z
		result["depth"] = depth
	return result


static func _next_query_hint(
	origin: Vector3i,
	requested_width: int,
	requested_height: int,
	requested_depth: int,
	observed_width: int,
	observed_height: int,
	observed_depth: int,
	dimension: int,
	map_layer: int
) -> Dictionary:
	var hint := {
		"x": origin.x,
		"y": origin.y,
		"width": observed_width,
		"height": observed_height,
	}
	if observed_width < requested_width:
		hint["x"] = origin.x + observed_width
	elif observed_height < requested_height:
		hint["y"] = origin.y + observed_height
	elif dimension == 3 and observed_depth < requested_depth:
		hint["z"] = origin.z + observed_depth
	else:
		# 仅详细 cell/摘要超限时，建议针对同一范围继续聚焦查询。
		hint["focus"] = "narrow the requested bounds around the needed boundary"
	if dimension == 3:
		hint["z"] = int(hint.get("z", origin.z))
		hint["depth"] = observed_depth
	else:
		hint["map_layer"] = map_layer
	return hint


static func _cell_run_key(cell: Dictionary, dimension: int) -> String:
	if dimension == 3:
		return "%d:%d" % [int(cell.get("item", -1)), int(cell.get("orientation", -1))]
	var atlas: Vector2i = cell.get("atlas_coords", Vector2i(-1, -1))
	return "%d:%d:%d:%d" % [int(cell.get("source_id", -1)), atlas.x, atlas.y, int(cell.get("alternative_tile", -1))]


static func _append_row_run(row_runs: Array, current_run: Dictionary) -> bool:
	if row_runs.size() >= MAX_SUMMARY_RUNS:
		return true
	var cell: Dictionary = current_run["cell"]
	var summary := _describe_safe_cell(cell, 3 if cell.has("item") else 2)
	summary.erase("coords")
	summary["x_start"] = current_run["x_start"]
	summary["x_end"] = current_run["x_end"]
	summary["y"] = current_run["y"]
	if cell.has("item"):
		summary["z"] = current_run["z"]
	row_runs.append(summary)
	return false


## 结构化 `region_too_large` 错误（任务 5.1）：携带上限与可安全缩小的约束，
## 让模型无需猜测就能把请求改到可接受范围。
static func _region_too_large_error(total_cells: int) -> Dictionary:
	var side_limit := int(floor(sqrt(float(MAX_DESCRIBED_CELLS))))
	return {
		"ok": false,
		"message": "requested region of %d cells exceeds the %d-cell read limit; reduce width*height to at most %d (e.g. width<=%d and height<=%d) or split into adjacent regions" % [
			total_cells, MAX_DESCRIBED_CELLS, MAX_DESCRIBED_CELLS, side_limit, side_limit
		],
		"error_code": "region_too_large",
		"max_cells": MAX_DESCRIBED_CELLS,
		"requested_cells": total_cells,
		"constraint": {
			"max_total_cells": MAX_DESCRIBED_CELLS,
			"safe_width": side_limit,
			"safe_height": side_limit,
		},
	}


## 把 2D 区域切分为若干“单元数不超过上限”的矩形（任务 5.2）。
##
## 分块只用于标注与限制单次读取规模：实际单元仍按全局行优先顺序读取，
## 因此各分块按返回顺序拼接即可无重叠、无遗漏地还原完整区域。
static func _partition_region_2d(origin_x: int, origin_y: int, width: int, height: int) -> Array:
	var partitions: Array = []
	var y := origin_y
	while y < origin_y + height:
		var max_rows := maxi(1, MAX_DESCRIBED_CELLS / width)
		var rows := mini(max_rows, origin_y + height - y)
		var x := origin_x
		while x < origin_x + width:
			var cols_allowed := maxi(1, MAX_DESCRIBED_CELLS / rows)
			var cols := mini(cols_allowed, origin_x + width - x)
			partitions.append({
				"x": x,
				"y": y,
				"width": cols,
				"height": rows,
				"cells": cols * rows,
			})
			x += cols
		y += rows
	return partitions


## 一个 legacy TileMap 节点可能同时挂多个图层（比如 "Background"/"Mid"），
## 各图层互相独立、互不遮挡判定；不能假设 map_layer=0 就是承载碰撞的前景层。
## 调用方应该看这份列表自己选对 map_layer，而不是不传 map_layer 时悄悄默认成 0。
static func _describe_tilemap_layers(target: Node) -> Array:
	var layers: Array = []
	var count: int = target.get_layers_count()
	for layer_index in range(count):
		layers.append({
			"index": layer_index,
			"name": str(target.get_layer_name(layer_index)),
			"enabled": bool(target.is_layer_enabled(layer_index)),
		})
	return layers


## 把 `_read_map_cell` 里的 Vector2i/Vector3i 折算成 JSON 可序列化的 `{x,y[,z]}`。
static func _describe_safe_cell(cell: Dictionary, dimension: int) -> Dictionary:
	var safe := cell.duplicate()
	var coords: Vector3i = safe.get("coords", Vector3i.ZERO)
	safe["coords"] = {"x": coords.x, "y": coords.y, "z": coords.z} if dimension == 3 else {"x": coords.x, "y": coords.y}
	if safe.has("atlas_coords"):
		var atlas: Vector2i = safe["atlas_coords"]
		safe["atlas_coords"] = {"x": atlas.x, "y": atlas.y}
	return safe


static func _resolve_map_target(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No scene is currently being edited", "error_code": "no_edited_scene"}
	var requested_path := str(input.get("target_path", "")).strip_edges()
	if requested_path != "":
		var requested := root if requested_path == "." else root.get_node_or_null(NodePath(requested_path))
		if requested == null:
			return {"ok": false, "message": "Map node was not found: " + requested_path, "error_code": "map_not_found"}
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


static func _collect_map_nodes(node: Node, out: Array) -> void:
	if _is_map_node(node):
		out.append(node)
	for child in node.get_children():
		_collect_map_nodes(child, out)


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
