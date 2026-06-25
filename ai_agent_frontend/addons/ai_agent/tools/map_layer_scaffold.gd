@tool
extends RefCounted


const STANDARD_2D_LAYERS := [
	{"name": "GroundLayer", "type": "TileMapLayer", "role": "ground", "tags": ["ground", "walkable"]},
	{"name": "WaterLayer", "type": "TileMapLayer", "role": "water", "tags": ["water", "blocked"]},
	{"name": "RoadLayer", "type": "TileMapLayer", "role": "road", "tags": ["road", "walkable"]},
	{"name": "ObstacleLayer", "type": "TileMapLayer", "role": "obstacle", "tags": ["obstacle", "solid"]},
	{"name": "DecorLayer", "type": "TileMapLayer", "role": "decor", "tags": ["decor"]},
	{"name": "ObjectLayer", "type": "Node2D", "role": "object", "tags": ["object"]},
]

const STANDARD_3D_LAYERS := [
	{"name": "GridMap", "type": "GridMap", "role": "grid", "tags": ["grid"]},
	{"name": "PropsRoot", "type": "Node3D", "role": "props", "tags": ["props"]},
	{"name": "LightsRoot", "type": "Node3D", "role": "lights", "tags": ["lights"]},
	{"name": "InteractRoot", "type": "Node3D", "role": "interact", "tags": ["interact"]},
]


static func ensure_standard_layers(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No scene is currently being edited", "error_code": "no_edited_scene"}
	var parent_path := str(input.get("parent_path", ".")).strip_edges()
	var parent: Node = root if parent_path in ["", ".", str(root.get_path())] else root.get_node_or_null(NodePath(parent_path))
	if parent == null:
		return {"ok": false, "message": "Parent not found: " + parent_path, "error_code": "parent_not_found"}
	var dimension := 3 if str(input.get("mode", "2d")).to_lower() == "3d" else 2
	var reference := _resolve_reference_map(root, parent, input, dimension)
	var specs := STANDARD_3D_LAYERS if dimension == 3 else STANDARD_2D_LAYERS
	var created: Array = []
	var existing: Array = []
	for spec in specs:
		var child := parent.get_node_or_null(NodePath(str(spec["name"])))
		if child != null:
			existing.append(_layer_result(root, child, spec, false))
			continue
		var node := _instantiate_layer_node(spec, reference)
		if node == null:
			return {"ok": false, "message": "Cannot instantiate " + str(spec["type"]), "error_code": "unsupported_layer_type"}
		parent.add_child(node)
		node.owner = root
		_apply_layer_metadata(node, spec)
		if undo_manager != null and undo_manager.has_method("record_node_added"):
			undo_manager.record_node_added(parent, node, root)
		created.append(_layer_result(root, node, spec, true))
	return {
		"ok": true,
		"parent_path": str(root.get_path_to(parent)) if parent != root else ".",
		"dimension": dimension,
		"created": created,
		"existing": existing,
		"standard_layers": specs,
	}


static func _instantiate_layer_node(spec: Dictionary, reference: Node) -> Node:
	var type_name := str(spec["type"])
	var instance = ClassDB.instantiate(type_name)
	if not (instance is Node):
		return null
	var node: Node = instance
	node.name = str(spec["name"])
	if node.get_class() == "TileMapLayer" and reference != null and "tile_set" in reference:
		node.set("tile_set", reference.get("tile_set"))
	elif node.get_class() == "GridMap" and reference != null and "mesh_library" in reference:
		node.set("mesh_library", reference.get("mesh_library"))
	return node


static func _resolve_reference_map(root: Node, parent: Node, input: Dictionary, dimension: int) -> Node:
	var reference_path := str(input.get("reference_path", "")).strip_edges()
	if reference_path != "":
		var found := root.get_node_or_null(NodePath(reference_path))
		if found != null:
			return found
	var maps: Array = []
	_collect_reference_maps(parent, maps, dimension)
	return maps[0] if maps.size() > 0 else null


static func _collect_reference_maps(node: Node, out: Array, dimension: int) -> void:
	if dimension == 3 and node.get_class() == "GridMap":
		out.append(node)
	elif dimension == 2 and node.get_class() == "TileMapLayer":
		out.append(node)
	for child in node.get_children():
		_collect_reference_maps(child, out, dimension)


static func _apply_layer_metadata(node: Node, spec: Dictionary) -> void:
	node.set_meta("map_agent_role", str(spec.get("role", "")))
	node.set_meta("map_agent_tags", spec.get("tags", []))


static func _layer_result(root: Node, node: Node, spec: Dictionary, was_created: bool) -> Dictionary:
	return {
		"path": str(root.get_path_to(node)),
		"name": node.name,
		"type": node.get_class(),
		"role": str(spec.get("role", "")),
		"created": was_created,
	}
