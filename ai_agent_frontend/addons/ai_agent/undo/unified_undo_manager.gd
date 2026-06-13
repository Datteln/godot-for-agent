@tool
extends Node

var undo_redo: EditorUndoRedoManager
var editor_interface: EditorInterface

var _batch_desc := ""
var _ops: Array[Dictionary] = []
var _active := false


func begin_batch(description: String) -> void:
	abort_batch()
	_batch_desc = description
	_ops.clear()
	_active = true


func record_file_write(path: String, before_text: String, after_text: String) -> void:
	if not _active:
		begin_batch("AI file changes")
	_write_file_text(path, after_text)
	_ops.append({
		"type": "file_write",
		"path": path,
		"before": before_text,
		"after": after_text
	})


func record_binary_file_write(path: String, before_bytes: PackedByteArray, after_bytes: PackedByteArray, before_exists: bool) -> void:
	if not _active:
		begin_batch("AI resource changes")
	_write_file_bytes(path, after_bytes, true)
	_ops.append({
		"type": "binary_file_write",
		"path": path,
		"before": before_bytes,
		"after": after_bytes,
		"before_exists": before_exists
	})


func record_node_added(parent: Node, node: Node, owner: Node) -> void:
	if not _active:
		begin_batch("AI scene changes")
	_ops.append({
		"type": "node_add",
		"parent": parent,
		"node": node,
		"owner": owner
	})


func record_node_property(node: Object, property: String, before_value: Variant, after_value: Variant) -> void:
	if not _active:
		begin_batch("AI scene property changes")
	node.set(property, after_value)
	_ops.append({
		"type": "node_property",
		"node": node,
		"property": property,
		"before": before_value,
		"after": after_value
	})


func record_tile_cells(layer: Node, before_cells: Array, after_cells: Array) -> void:
	if not _active:
		begin_batch("AI tilemap changes")
	_set_tile_cells(layer, after_cells)
	_ops.append({
		"type": "tile_cells",
		"layer": layer,
		"before": before_cells,
		"after": after_cells
	})


func record_node_removed(parent: Node, node: Node, index: int) -> void:
	if not _active:
		begin_batch("AI scene changes")
	var owner := node.owner
	_remove_node(parent, node)
	_ops.append({
		"type": "node_remove",
		"parent": parent,
		"node": node,
		"owner": owner,
		"index": index
	})


func record_node_reparented(node: Node, old_parent: Node, old_index: int, new_parent: Node, owner: Node) -> void:
	if not _active:
		begin_batch("AI scene changes")
	_reparent_node(node, new_parent, owner)
	_ops.append({
		"type": "node_reparent",
		"node": node,
		"old_parent": old_parent,
		"old_index": old_index,
		"new_parent": new_parent,
		"owner": owner
	})


func record_node_renamed(node: Node, before_name: String, after_name: String) -> void:
	if not _active:
		begin_batch("AI scene changes")
	_rename_node(node, after_name)
	_ops.append({
		"type": "node_rename",
		"node": node,
		"before": before_name,
		"after": after_name
	})


func commit_batch() -> void:
	if not _active:
		return
	if undo_redo == null or _ops.is_empty():
		_clear()
		return

	undo_redo.create_action(_batch_desc)
	for op in _ops:
		match op.get("type", ""):
			"file_write":
				undo_redo.add_do_method(self, "_write_file_text", op["path"], op["after"])
				undo_redo.add_undo_method(self, "_write_file_text", op["path"], op["before"])
			"binary_file_write":
				undo_redo.add_do_method(self, "_write_file_bytes", op["path"], op["after"], true)
				undo_redo.add_undo_method(self, "_write_file_bytes", op["path"], op["before"], op["before_exists"])
			"node_add":
				undo_redo.add_do_method(self, "_add_node", op["parent"], op["node"], op["owner"])
				undo_redo.add_undo_method(self, "_remove_node", op["parent"], op["node"])
				undo_redo.add_do_reference(op["node"])
			"node_property":
				undo_redo.add_do_method(op["node"], "set", op["property"], op["after"])
				undo_redo.add_undo_method(op["node"], "set", op["property"], op["before"])
			"tile_cells":
				undo_redo.add_do_method(self, "_set_tile_cells", op["layer"], op["after"])
				undo_redo.add_undo_method(self, "_set_tile_cells", op["layer"], op["before"])
			"node_remove":
				undo_redo.add_do_method(self, "_remove_node", op["parent"], op["node"])
				undo_redo.add_undo_method(self, "_add_node_at", op["parent"], op["node"], op["owner"], op["index"])
				undo_redo.add_undo_reference(op["node"])
			"node_reparent":
				undo_redo.add_do_method(self, "_reparent_node", op["node"], op["new_parent"], op["owner"])
				undo_redo.add_undo_method(self, "_reparent_node_to", op["node"], op["old_parent"], op["old_index"], op["owner"])
			"node_rename":
				undo_redo.add_do_method(self, "_rename_node", op["node"], op["after"])
				undo_redo.add_undo_method(self, "_rename_node", op["node"], op["before"])
	undo_redo.commit_action(false)
	_clear()


func abort_batch() -> void:
	if not _active:
		return
	for index in range(_ops.size() - 1, -1, -1):
		var op: Dictionary = _ops[index]
		match op.get("type", ""):
			"file_write":
				_write_file_text(str(op["path"]), str(op["before"]))
			"binary_file_write":
				_write_file_bytes(str(op["path"]), op["before"], bool(op["before_exists"]))
			"node_add":
				var parent: Object = op["parent"]
				var node: Object = op["node"]
				if is_instance_valid(parent) and is_instance_valid(node):
					_remove_node(parent, node)
				else:
					push_warning("Skipping undo of node_add: parent or node is no longer valid")
			"node_property":
				var node: Object = op["node"]
				if is_instance_valid(node) and node.has_method("set"):
					node.set(op["property"], op["before"])
				else:
					push_warning("Skipping undo of node_property: node is no longer valid")
			"tile_cells":
				var layer: Object = op["layer"]
				if is_instance_valid(layer) and layer.has_method("set_cell"):
					_set_tile_cells(layer, op["before"])
				else:
					push_warning("Skipping undo of tile_cells: layer is no longer valid")
			"node_remove":
				var parent: Object = op["parent"]
				var node: Object = op["node"]
				if is_instance_valid(parent) and is_instance_valid(node):
					_add_node_at(parent, node, op["owner"], op["index"])
				else:
					push_warning("Skipping undo of node_remove: parent or node is no longer valid")
			"node_reparent":
				var node: Object = op["node"]
				var old_parent: Object = op["old_parent"]
				if is_instance_valid(node) and is_instance_valid(old_parent):
					_reparent_node_to(node, old_parent, op["old_index"], op["owner"])
				else:
					push_warning("Skipping undo of node_reparent: node or old parent is no longer valid")
			"node_rename":
				var node: Object = op["node"]
				if is_instance_valid(node):
					_rename_node(node, op["before"])
				else:
					push_warning("Skipping undo of node_rename: node is no longer valid")
	_clear()


func _clear() -> void:
	_batch_desc = ""
	_ops.clear()
	_active = false


func _write_file_text(path: String, text: String) -> void:
	var absolute := ProjectSettings.globalize_path(path)
	var dir_path := absolute.get_base_dir()
	DirAccess.make_dir_recursive_absolute(dir_path)
	var file := FileAccess.open(absolute, FileAccess.WRITE)
	if file == null:
		push_error("Failed to write file: " + path)
		return
	file.store_string(text)
	file.close()
	if ResourceLoader.exists(path):
		ResourceLoader.load(path, "", ResourceLoader.CACHE_MODE_REPLACE)


func _write_file_bytes(path: String, bytes: PackedByteArray, exists: bool) -> void:
	var absolute := ProjectSettings.globalize_path(path)
	if not exists:
		if FileAccess.file_exists(absolute):
			DirAccess.remove_absolute(absolute)
		return
	DirAccess.make_dir_recursive_absolute(absolute.get_base_dir())
	var file := FileAccess.open(absolute, FileAccess.WRITE)
	if file == null:
		push_error("Failed to write file: " + path)
		return
	file.store_buffer(bytes)
	file.close()


func _add_node(parent: Node, node: Node, owner: Node) -> void:
	if parent == null or node == null:
		return
	if node.get_parent() != null:
		return
	parent.add_child(node)
	node.owner = owner


func _remove_node(parent: Node, node: Node) -> void:
	if parent == null or node == null:
		return
	if node.get_parent() == parent:
		parent.remove_child(node)


func _add_node_at(parent: Node, node: Node, owner: Node, index: int) -> void:
	if parent == null or node == null:
		return
	if node.get_parent() != null:
		return
	parent.add_child(node)
	node.owner = owner
	if index >= 0 and index < parent.get_child_count():
		parent.move_child(node, index)


func _reparent_node(node: Node, new_parent: Node, owner: Node) -> void:
	if node == null or new_parent == null:
		return
	var old_parent := node.get_parent()
	if old_parent == new_parent:
		return
	if old_parent != null:
		old_parent.remove_child(node)
	new_parent.add_child(node)
	node.owner = owner


func _reparent_node_to(node: Node, parent: Node, index: int, owner: Node) -> void:
	_reparent_node(node, parent, owner)
	if parent != null and node != null and index >= 0 and index < parent.get_child_count():
		parent.move_child(node, index)


func _rename_node(node: Node, new_name: String) -> void:
	if node == null:
		return
	node.name = new_name


func _set_tile_cells(layer: Node, cells: Array) -> void:
	if layer == null or not layer.has_method("set_cell"):
		return
	for cell in cells:
		if not (cell is Dictionary):
			continue
		layer.call(
			"set_cell",
			cell.get("coords", Vector2i.ZERO),
			int(cell.get("source_id", -1)),
			cell.get("atlas_coords", Vector2i(-1, -1)),
			int(cell.get("alternative_tile", 0))
		)
