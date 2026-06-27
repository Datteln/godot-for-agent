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
const MAX_DESCRIBED_CELLS := 400
const MAX_NOISE_CELLS := 4096
## 空间索引整份读出/整份重写，条目数上限防止它随使用无限膨胀、拖慢每次 edit_map。
## 到顶后仍允许更新/删除已有坐标，只拒绝新增坐标，并在结果里给出 warning。
const MAX_SPATIAL_INDEX_ENTRIES := 20000
## 地图 agent 运行期生成的数据统一落在项目下的 res://.ai_agent_service/map_agent 里，
## 不再写进 addons（避免污染插件目录，也方便整目录清理）。读取时仍兼容旧的 addons 路径，
## 以免历史项目里已有的语义表/索引被孤立。
const MAP_DATA_DIR := "res://.ai_agent_service/map_agent"
const RESOURCE_REGISTRY_WRITE_PATH := "res://.ai_agent_service/map_agent/resource_registry.json"
const RESOURCE_REGISTRY_PATHS := [
	"res://.ai_agent_service/map_agent/resource_registry.json",
	"res://addons/map_agent/data/resource_registry.json",
	"res://addons/ai_agent/data/resource_registry.json",
]
const SPATIAL_INDEX_PATH := "res://.ai_agent_service/map_agent/spatial_index.json"
const BLUEPRINTS_DIR := "res://.ai_agent_service/map_agent/blueprints"


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

	var registry := _read_first_json_resource(RESOURCE_REGISTRY_PATHS)
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
	var target_result := _resolve_map_target(input, editor_interface)
	if not bool(target_result.get("ok", false)):
		return target_result
	var target: Node = target_result["node"]
	var operations: Array = input.get("operations", [])
	if operations.is_empty():
		return {"ok": false, "message": "operations must not be empty", "error_code": "invalid_operations"}
	if operations.size() > 128:
		return {"ok": false, "message": "at most 128 operations are allowed", "error_code": "map_edit_too_large"}

	var dimension := 3 if target.get_class() == "GridMap" else 2
	var map_layer := int(input.get("map_layer", 0))
	var allowed_bounds := _bounds_from_input(input, dimension)
	# 编辑前先记录"毯式图层"（背景/天空这类本来就铺满全图的图层）的现状，
	# 编辑后跟新的整体范围一比，就能发现哪些图层没跟着扩——不用 agent 自己记得去查，
	# edit_map 自己的返回结果里就会带出来。GridMap(3D)/无同组图层时这里自然是空操作。
	var coverage_edited_extent_before := _layer_used_extent_2d(target, map_layer)
	var coverage_siblings := _sibling_layers_for_coverage(target, map_layer)
	for sibling in coverage_siblings:
		(sibling as Dictionary)["extent"] = _layer_used_extent_2d((sibling as Dictionary)["node"], int((sibling as Dictionary)["map_layer"]))
	var before: Array = []
	var after: Array = []
	var touched := {}
	var pending_cells := {}
	for operation_value in operations:
		if not (operation_value is Dictionary):
			return {"ok": false, "message": "each operation must be an object", "error_code": "invalid_operation"}
		var operation: Dictionary = operation_value
		_apply_registry_fallback_to_operation(operation, dimension)
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
	var coverage_gaps := _compute_coverage_gaps(coverage_edited_extent_before, coverage_edited_extent_after, coverage_siblings)
	var result := {
		"ok": true,
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
	if not coverage_gaps.is_empty():
		result["coverage_gap_warning"] = "One or more full-coverage layers (background/sky/water etc.) now fall short of this map's overall extent. Extend them to match before treating this edit as finished."
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
## （每个元素带它们当前的 "extent"，不受这次调用影响）。
static func _compute_coverage_gaps(extent_before: Dictionary, extent_after: Dictionary, siblings: Array) -> Array:
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
		if not shortfall.is_empty():
			gaps.append({
				"layer": sibling.get("label", ""),
				"map_layer": sibling.get("map_layer", 0),
				"current_extent": {
					"min_x": sibling_extent["min_x"],
					"max_x": sibling_extent["max_x"],
					"min_y": sibling_extent["min_y"],
					"max_y": sibling_extent["max_y"],
				},
				"shortfall_cells": shortfall,
			})
	return gaps


## 使用 TileSet terrain connect API 绘制一组 2D terrain cell，让道路/水域边缘自动衔接。
static func paint_terrain_connect(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var target_result := _resolve_map_target(input, editor_interface)
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
	var target_result := _resolve_map_target(input, editor_interface)
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
	var registry := _read_first_json_resource(RESOURCE_REGISTRY_PATHS)
	var registry_data: Dictionary = registry.get("data", {}) if registry.get("data", {}) is Dictionary else {}
	var occupied := _object_occupancy_from_spatial_index(str(target_result.get("path", "")), dimension)
	var blocked_cells := _blocked_object_cells_from_spatial_index(str(target_result.get("path", "")), dimension)
	var planned := {}
	var prepared: Array = []
	for object_value in objects_value:
		if not (object_value is Dictionary):
			return {"ok": false, "message": "each object must be an object", "error_code": "invalid_object"}
		var object_spec: Dictionary = object_value
		var coords := Vector3i(
			int(object_spec.get("x", 0)),
			int(object_spec.get("y", 0)),
			int(object_spec.get("z", 0)) if dimension == 3 else 0
		)
		if not _cell_within_bounds(coords, allowed_bounds):
			return {
				"ok": false,
				"message": "object placement would write outside allowed_bounds",
				"error_code": "map_object_out_of_bounds",
				"coords": MapValidator.coord_payload(coords, dimension),
				"allowed_bounds": allowed_bounds,
			}
		var coord_key := MapValidator.coord_key(coords)
		if not bool(input.get("allow_overlap", false)) and (occupied.has(coord_key) or planned.has(coord_key)):
			return {
				"ok": false,
				"message": "object placement overlaps an existing or planned object",
				"error_code": "map_object_overlap",
				"coords": MapValidator.coord_payload(coords, dimension),
			}
		if not bool(input.get("allow_on_blocked", false)) and blocked_cells.has(coord_key):
			return {
				"ok": false,
				"message": "object placement is on a blocked/water/obstacle cell",
				"error_code": "map_object_blocked_cell",
				"coords": MapValidator.coord_payload(coords, dimension),
				"blocking_entry": blocked_cells[coord_key],
			}
		var resource_key := str(object_spec.get("resource", object_spec.get("resource_key", ""))).strip_edges()
		var resource_def: Dictionary = _registry_entry_with_fallback(registry_data, resource_key, str(object_spec.get("fallback_resource", "")))
		if resource_def.has("_resolved_resource"):
			resource_key = str(resource_def.get("_resolved_resource", resource_key))
		var scene_path := PathUtils.to_res_path(str(object_spec.get("scene_path", resource_def.get("scene_path", ""))))
		if scene_path == "" or not (scene_path.ends_with(".tscn") or scene_path.ends_with(".scn")):
			return {"ok": false, "message": "object requires a .tscn/.scn scene_path or resource registry entry", "error_code": "missing_scene_path", "resource": resource_key}
		if not FileAccess.file_exists(scene_path):
			return {"ok": false, "message": "scene file not found: " + scene_path, "error_code": "scene_not_found"}
		var packed = load(scene_path)
		if not (packed is PackedScene):
			return {"ok": false, "message": "Failed to load as PackedScene: " + scene_path, "error_code": "load_failed"}
		var instance := (packed as PackedScene).instantiate()
		if not (instance is Node):
			return {"ok": false, "message": "PackedScene did not instantiate a Node: " + scene_path, "error_code": "instantiate_failed"}
		var node: Node = instance
		if dimension == 2 and not (node is Node2D):
			return {"ok": false, "message": "2D map object must instantiate a Node2D scene: " + scene_path, "error_code": "object_type_mismatch"}
		if dimension == 3 and not (node is Node3D):
			return {"ok": false, "message": "3D map object must instantiate a Node3D scene: " + scene_path, "error_code": "object_type_mismatch"}
		node.name = _object_instance_name(object_spec, resource_key, scene_path)
		_apply_object_position(node, map_node, coords, dimension)
		_apply_object_metadata(node, object_spec, resource_key, scene_path, coords, dimension)
		prepared.append({"node": node, "coords": coords, "scene_path": scene_path, "resource": resource_key, "spec": object_spec})
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
		"paths": paths,
		"spatial_index": index_result,
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
	if width * height * depth > MAX_DESCRIBED_CELLS:
		return {
			"ok": false,
			"message": "requested region exceeds the %d-cell read limit; query a smaller region" % MAX_DESCRIBED_CELLS,
			"error_code": "region_too_large",
		}

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
			# 这一层瓦片实际铺到哪——背景/天空这类"毯式"图层扩图时要不要跟着扩，
			# 直接看这个范围跟其它层比是不是已经跟不上，不用再去猜。
			"used_bounds": _used_bounds_2d(target.call("get_used_cells", layer_index)),
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
	for key in ["resource", "resource_key", "semantic_layer", "tags", "cost"]:
		if operation.has(key):
			cell[key] = operation[key]


static func _apply_registry_fallback_to_operation(operation: Dictionary, dimension: int) -> void:
	var resource_entry := _registry_entry_for_resource_input(operation)
	if resource_entry.is_empty():
		return
	if resource_entry.has("_resolved_resource") and not operation.has("resource"):
		operation["resource"] = str(resource_entry.get("_resolved_resource", ""))
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


static func _registry_entry_for_resource_input(input: Dictionary) -> Dictionary:
	var registry := _read_first_json_resource(RESOURCE_REGISTRY_PATHS)
	var registry_data: Dictionary = registry.get("data", {}) if registry.get("data", {}) is Dictionary else {}
	var resource_key := str(input.get("resource", input.get("resource_key", ""))).strip_edges()
	var fallback_key := str(input.get("fallback_resource", input.get("fallback_resource_key", ""))).strip_edges()
	return _registry_entry_with_fallback(registry_data, resource_key, fallback_key)


static func _registry_entry_with_fallback(registry_data: Dictionary, resource_key: String, fallback_key: String) -> Dictionary:
	if resource_key != "" and registry_data.get(resource_key, {}) is Dictionary:
		var primary: Dictionary = (registry_data.get(resource_key, {}) as Dictionary).duplicate(true)
		primary["_resolved_resource"] = resource_key
		return primary
	if fallback_key != "" and registry_data.get(fallback_key, {}) is Dictionary:
		var fallback: Dictionary = (registry_data.get(fallback_key, {}) as Dictionary).duplicate(true)
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


static func _read_first_json_resource(paths: Array) -> Dictionary:
	for path in paths:
		var parsed := _read_json_resource(str(path))
		if bool(parsed.get("exists", false)):
			return parsed
	return {"exists": false, "paths_checked": paths, "data": {}}


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
	for value in after_cells:
		if not (value is Dictionary):
			continue
		var cell: Dictionary = value
		var key := _index_coord_key(cell.get("coords", Vector3i.ZERO), dimension)
		if _is_empty_cell(target, cell):
			if target_index.has(key):
				target_index.erase(key)
				total_entries -= 1
				removed += 1
		elif target_index.has(key):
			# 原地更新已有坐标，不增加体量。
			target_index[key] = _describe_safe_cell(cell, dimension)
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
	return result


static func _index_coord_key(coords: Vector3i, dimension: int) -> String:
	return "%d,%d,%d" % [coords.x, coords.y, coords.z] if dimension == 3 else "%d,%d" % [coords.x, coords.y]


static func _is_empty_cell(target: Node, cell: Dictionary) -> bool:
	if target.get_class() == "GridMap":
		return int(cell.get("item", -1)) == -1
	return int(cell.get("source_id", -1)) == -1


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
	var entry = (target_entries as Dictionary).get(_index_coord_key(coords, dimension), {})
	if not (entry is Dictionary):
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


static func _apply_object_position(node: Node, map_node: Node, coords: Vector3i, dimension: int) -> void:
	if dimension == 3 and node is Node3D:
		var cell_size := Vector3.ONE
		if "cell_size" in map_node:
			cell_size = map_node.get("cell_size")
		var base_3d := (map_node as Node3D).position if map_node is Node3D else Vector3.ZERO
		(node as Node3D).position = base_3d + Vector3(coords.x * cell_size.x, coords.y * cell_size.y, coords.z * cell_size.z)
	elif dimension == 2 and node is Node2D:
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
			"node_name": indexed_node_name,
			"semantic_layer": str(spec.get("semantic_layer", "object")),
			"tags": spec.get("tags", []),
		}
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
	var target_result := _resolve_map_target(input, editor_interface)
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
		_apply_object_position(node, target, new_coords, dimension)
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
	# before_text 取写入路径自身的当前内容，保证 Undo 能按字节还原。首次写入且非覆盖时，
	# 尝试从旧 addons 路径迁移已有语义表，避免历史内容被孤立。
	var before_text := _read_text_file(RESOURCE_REGISTRY_WRITE_PATH)
	var data: Dictionary = {}
	if before_text != "":
		var parsed = JSON.parse_string(before_text)
		if parsed is Dictionary:
			data = (parsed as Dictionary).duplicate(true)
	elif not replace:
		var legacy := _read_first_json_resource(RESOURCE_REGISTRY_PATHS)
		if legacy.get("data", {}) is Dictionary:
			data = (legacy.get("data", {}) as Dictionary).duplicate(true)
	if replace:
		data = {}
	for key in entries.keys():
		var entry = entries[key]
		if not (entry is Dictionary):
			return {"ok": false, "message": "entry '%s' must be an object" % str(key), "error_code": "invalid_entry"}
		data[str(key)] = entry
	var after_text := JSON.stringify(data, "\t")
	var write_result := _write_json_file(RESOURCE_REGISTRY_WRITE_PATH, before_text, after_text, undo_manager)
	if not bool(write_result.get("ok", false)):
		return write_result
	return {
		"ok": true,
		"path": RESOURCE_REGISTRY_WRITE_PATH,
		"keys": data.keys(),
		"written_keys": entries.keys(),
		"replaced": replace,
	}


## 按 tag / 语义层 / 资源 key / 坐标范围检索空间索引，定位"左上角的树""村庄道路"这类语义对象，
## 支撑局部删除/替换，避免全量重绘。纯读，不需确认。
static func query_spatial_index(input: Dictionary, _editor_interface: EditorInterface) -> Dictionary:
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
	var target_filter := str(input.get("target_path", "")).strip_edges()
	var has_region := input.has("x") or input.has("y") or input.has("z") \
		or input.has("width") or input.has("height") or input.has("depth")
	var region := _region_bounds(input)
	var limit := max(1, int(input.get("limit", 200)))

	var matches: Array = []
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
			if not _entry_matches(entry, want_tags, want_resource, want_layer):
				continue
			if has_region and not _entry_in_region(entry, region, dimension):
				continue
			var hit := (entry as Dictionary).duplicate(true)
			hit["target_path"] = str(target_path)
			hit["coord_key"] = str(coord_key)
			matches.append(hit)
			if matches.size() >= limit:
				break
		if matches.size() >= limit:
			break
	return {
		"ok": true,
		"dimension": dimension,
		"matches": matches,
		"total": matches.size(),
		"truncated": matches.size() >= limit,
		"index_entries": _count_index_entries(data.get("2d", {})) + _count_index_entries(data.get("3d", {})),
		"max_entries": MAX_SPATIAL_INDEX_ENTRIES,
	}


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
	if bool(platform_input.get("connect_from_existing", true)):
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
	if profile in ["platform", "platformer", "side_scroller", "side-scroller"] and bool(growth_input.get("connect_from_existing", true)):
		if not growth_input.has("frontier"):
			var anchor_result := _platform_entry_anchor(growth_input, editor_interface)
			if bool(anchor_result.get("ok", false)):
				growth_input["entry_anchor"] = anchor_result.get("entry_anchor", {})
				growth_input["frontier"] = anchor_result.get("entry_anchor", {})
				growth_input["entry_sample"] = anchor_result
			else:
				growth_input["entry_anchor_scan_failed"] = true
				growth_input["entry_sample"] = anchor_result
	return MapReachableGrowth.plan_growth(growth_input, context)


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
		return {
			"ok": false,
			"message": "start is not standable under the requested movement model",
			"error_code": "start_not_standable",
			"start": MapValidator.coord_payload(start, dimension),
			"movement_model": movement.get("model", "grid"),
		}
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
	var sample_width := max(3, int(input.get("entry_sample_width", 12)))
	var sample_x := int(input.get("entry_sample_x", region_x - sample_width))
	var sample_y := int(input.get("entry_sample_y", input.get("y", 0)))
	var sample_height := max(4, int(input.get("entry_sample_height", input.get("height", 20))))
	var best_support := Vector3i(-2147483648, 0, 0)
	for y_offset in range(sample_height):
		for x_offset in range(sample_width):
			var coords := Vector3i(sample_x + x_offset, sample_y + y_offset, 0)
			var cell := _read_map_cell(target, coords, dimension, map_layer)
			if _is_empty_cell(target, cell):
				continue
			var above := _read_map_cell(target, coords + Vector3i(0, -1, 0), dimension, map_layer)
			if not _is_empty_cell(target, above):
				continue
			if coords.x > best_support.x or (coords.x == best_support.x and coords.y < best_support.y):
				best_support = coords
	if best_support.x == -2147483648:
		return {
			"ok": false,
			"message": "no reachable surface found in the left boundary sample",
			"error_code": "platform_entry_anchor_not_found",
			"sample_rect": {"x": sample_x, "y": sample_y, "width": sample_width, "height": sample_height},
		}
	return {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"map_layer": map_layer if target.get_class() == "TileMap" else null,
		"sample_rect": {"x": sample_x, "y": sample_y, "width": sample_width, "height": sample_height},
		"entry_support": {"x": best_support.x, "y": best_support.y},
		"entry_anchor": {"x": best_support.x, "y": best_support.y - 1},
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
	var target_result := _resolve_map_target(input, editor_interface)
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
	if int(region["width"]) * int(region["height"]) * int(region["depth"]) > MAX_DESCRIBED_CELLS:
		return {
			"ok": false,
			"message": "validation region exceeds the %d-cell limit; validate a smaller region" % MAX_DESCRIBED_CELLS,
			"error_code": "region_too_large",
		}

	var filled := _collect_filled_cells(target, region, dimension, map_layer)
	var result := {
		"ok": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"region": region,
	}
	var analysis := MapValidator.validate_region(
		filled,
		region,
		dimension,
		input.get("start", null),
		input.get("goal", null),
		MapValidator.movement_from_input(input, dimension),
		str(input.get("path_algorithm", "bfs")),
		input.get("waypoints", null),
		input.get("entrances", null),
		input.get("exits", null)
	)
	result.merge(analysis, true)
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
		(sibling as Dictionary)["extent"] = _layer_used_extent_2d((sibling as Dictionary)["node"], int((sibling as Dictionary)["map_layer"]))
	var coverage_gaps := _compute_coverage_gaps(coverage_extent, coverage_extent, coverage_siblings)
	result["layer_coverage_gaps"] = coverage_gaps
	if not coverage_gaps.is_empty():
		result["passed"] = false
		var coverage_issues: Array = result.get("issues", [])
		coverage_issues.append("one or more full-coverage layers (background/sky/water etc.) fall short of the map's overall extent")
		result["issues"] = coverage_issues
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
			"message": "repair region exceeds the %d-cell limit; repair a smaller region" % MAX_DESCRIBED_CELLS,
			"error_code": "region_too_large",
		}
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
		return {"ok": true, "changed": false, "message": "Region already passes validation", "validation": analysis}
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
	return {
		"ok": true,
		"changed": true,
		"target": str(target_result.get("path", "")),
		"type": target.get_class(),
		"dimension": dimension,
		"cells": after.size(),
		"validation_before": analysis,
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
	var target_result := _resolve_map_target(input, editor_interface)
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

	var target_result := _resolve_map_target(input, editor_interface)
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


static func _entry_matches(entry: Dictionary, want_tags: Array, want_resource: String, want_layer: String) -> bool:
	if want_resource != "":
		var entry_resource := str(entry.get("resource", entry.get("resource_key", "")))
		if entry_resource != want_resource:
			return false
	if want_layer != "" and str(entry.get("semantic_layer", "")) != want_layer:
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
