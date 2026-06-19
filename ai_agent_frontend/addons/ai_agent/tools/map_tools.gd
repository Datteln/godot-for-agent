@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")

const MAX_EDITED_CELLS := 100000


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
	var before: Array = []
	var after: Array = []
	var touched := {}
	var pending_cells := {}
	for operation_value in operations:
		if not (operation_value is Dictionary):
			return {"ok": false, "message": "each operation must be an object", "error_code": "invalid_operation"}
		var operation: Dictionary = operation_value
		var built := _build_map_operation(target, dimension, map_layer, operation, pending_cells)
		if not bool(built.get("ok", false)):
			return built
		for cell_value in built.get("cells", []):
			var cell: Dictionary = cell_value
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
		"operations": operations.size(),
		"cells": after.size(),
		"message": "Map edited through Godot native APIs; serialized map data was not modified directly."
	}


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
				cells.append(cell)
	return {"ok": true, "cells": cells}


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
