@tool
extends RefCounted


static func build_blueprint(
	name: String,
	dimension: int,
	map_layer: int,
	origin: Vector3i,
	width: int,
	height: int,
	depth: int,
	tags: Array,
	read_cell: Callable,
	is_empty_cell: Callable
) -> Dictionary:
	var ops: Array = []
	for dz in range(depth):
		for dy in range(height):
			for dx in range(width):
				var coords := origin + Vector3i(dx, dy, dz)
				var cell: Dictionary = read_cell.call(coords)
				if bool(is_empty_cell.call(cell)):
					continue
				var op := _cell_to_blueprint_op(cell, dimension, map_layer, dx, dy, dz)
				ops.append(op)
	return {
		"name": name,
		"dimension": dimension,
		"mode": "3d" if dimension == 3 else "2d",
		"size": {"width": width, "height": height, "depth": depth} if dimension == 3 else {"width": width, "height": height},
		"cell_count": ops.size(),
		"ops": ops,
		"tags": tags,
	}


static func build_cells_from_blueprint(blueprint: Dictionary, dimension: int, map_layer: int, origin: Vector3i) -> Array:
	var cells: Array = []
	var ops = blueprint.get("ops", [])
	if not (ops is Array):
		return cells
	for op_value in ops:
		if not (op_value is Dictionary):
			continue
		var op: Dictionary = op_value
		var coords := origin + Vector3i(
			int(op.get("dx", 0)),
			int(op.get("dy", 0)),
			int(op.get("dz", 0)) if dimension == 3 else 0
		)
		var cell := {"coords": coords}
		if dimension == 3:
			cell["item"] = int(op.get("item", -1))
			cell["orientation"] = int(op.get("orientation", 0))
		else:
			cell["map_layer"] = map_layer
			cell["source_id"] = int(op.get("source_id", -1))
			cell["atlas_coords"] = Vector2i(int(op.get("atlas_x", -1)), int(op.get("atlas_y", -1)))
			cell["alternative_tile"] = int(op.get("alternative_tile", 0))
		_copy_metadata(op, cell)
		cells.append(cell)
	return cells


static func blueprint_dimension(blueprint: Dictionary) -> int:
	if blueprint.has("dimension"):
		return int(blueprint.get("dimension", 2))
	return 3 if str(blueprint.get("mode", "2d")) == "3d" else 2


static func sanitize_name(raw: String) -> String:
	var trimmed := raw.strip_edges()
	var result := ""
	for i in range(trimmed.length()):
		var c := trimmed[i]
		if (c >= "a" and c <= "z") or (c >= "A" and c <= "Z") or (c >= "0" and c <= "9") or c == "_" or c == "-":
			result += c
	return result


static func _cell_to_blueprint_op(
	cell: Dictionary,
	dimension: int,
	map_layer: int,
	dx: int,
	dy: int,
	dz: int
) -> Dictionary:
	var op := {"dx": dx, "dy": dy}
	if dimension == 3:
		op["dz"] = dz
		op["item"] = int(cell.get("item", -1))
		op["orientation"] = int(cell.get("orientation", 0))
	else:
		var atlas: Vector2i = cell.get("atlas_coords", Vector2i(-1, -1))
		op["map_layer"] = int(cell.get("map_layer", map_layer))
		op["source_id"] = int(cell.get("source_id", -1))
		op["atlas_x"] = atlas.x
		op["atlas_y"] = atlas.y
		op["alternative_tile"] = int(cell.get("alternative_tile", 0))
	_copy_metadata(cell, op)
	return op


static func _copy_metadata(source: Dictionary, target: Dictionary) -> void:
	for key in ["resource", "resource_key", "semantic_layer", "tags", "cost"]:
		if source.has(key):
			target[key] = source[key]
