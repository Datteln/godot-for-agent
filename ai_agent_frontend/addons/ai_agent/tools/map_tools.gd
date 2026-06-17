@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")


static func describe_selection(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	for node in editor_interface.get_selection().get_selected_nodes():
		if node != null and node.get_class() == "TileMapLayer":
			var path := str(root.get_path_to(node)) if root != null else str(node.get_path())
			return {"ok": true, "path": path, "type": "TileMapLayer"}
	return {"ok": false, "message": "Select a TileMapLayer first"}


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
