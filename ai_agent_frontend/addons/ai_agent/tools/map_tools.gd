@tool
extends RefCounted

const MAX_DESCRIBED_CELLS := 400
## 2D 区域读取允许自动分块的上限（任务 5.2）：超过该总单元数不再分块，
## 直接返回带约束的 `region_too_large`。3D GridMap 不参与自动分块。
const MAX_PARTITIONED_CELLS := 3200


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
	# 3D GridMap 暂不支持安全分块：保持严格 400 单元上限与结构化错误（任务 5.2）。
	if dimension == 3 and total_cells > MAX_DESCRIBED_CELLS:
		return _region_too_large_error(total_cells)
	# 2D 区域超出单块上限时做有界分块；超出可分块总上限仍返回结构化错误。
	if dimension == 2 and total_cells > MAX_PARTITIONED_CELLS:
		return _region_too_large_error(total_cells)

	var cells: Array = []
	for z_offset in range(depth):
		for y_offset in range(height):
			for x_offset in range(width):
				var coords := origin + Vector3i(x_offset, y_offset, z_offset)
				cells.append(_describe_safe_cell(_read_map_cell(target, coords, dimension, map_layer), dimension))

	var result := {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"cells": cells,
	}
	if dimension == 2 and total_cells > MAX_DESCRIBED_CELLS:
		# 语义保持的分块：cells 仍按全局行优先顺序一次性读取，分块只标注边界，
		# 调用方可凭 partitions 与 total_cells 核对拼回完整区域（任务 5.2）。
		result["partitioned"] = true
		result["total_cells"] = total_cells
		result["max_cells_per_partition"] = MAX_DESCRIBED_CELLS
		result["partitions"] = _partition_region_2d(origin.x, origin.y, width, height)
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
