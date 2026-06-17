@tool
extends RefCounted


## 节点路径相对于"被编辑场景的根节点"而非 `node.get_path()` 的 SceneTree 绝对路径。
## 在编辑器里运行时，被编辑场景是挂在编辑器自身视口树很深的位置下的，
## `node.get_path()` 会把整条 `/root/@EditorNode@.../@SubViewport@.../` 编辑器内部
## 路径都吐出来——又长又会随编辑器布局变化，不适合展示给用户，也不该塞进模型上下文。
static func _relative_path(root: Node, node: Node) -> String:
	return str(root.get_path_to(node))


static func read_scene_tree(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {}
	return _node_to_dict(root, root, 0, 6)


static func read_runtime_state(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var max_depth := clamp(int(input.get("max_depth", 4)), 1, 8)
	var result := {
		"ok": true,
		"engine_version": Engine.get_version_info(),
		"edited_scene": {},
		"selected_nodes": [],
		"editor_hint": Engine.is_editor_hint(),
		"note": "Editor plugins can only read bounded editor/runtime facts exposed by Godot; no debugger control is performed."
	}
	if editor_interface == null:
		return result
	var root := editor_interface.get_edited_scene_root()
	if root != null:
		result["edited_scene"] = _node_to_dict(root, root, 0, max_depth)
	for node in editor_interface.get_selection().get_selected_nodes():
		if node is Node:
			result["selected_nodes"].append({
				"name": node.name,
				"path": _relative_path(root, node) if root != null else str(node.get_path()),
				"type": node.get_class(),
				"visible": node.visible if node is CanvasItem else null,
				"process_mode": int(node.process_mode)
			})
	return result


static func add_node(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}

	var parent_path := str(input.get("parent_path", "."))
	var parent: Node = root if parent_path in [".", "", str(root.get_path())] else root.get_node_or_null(NodePath(parent_path))
	if parent == null:
		return {"ok": false, "message": "Parent not found: " + parent_path}

	var type_name := str(input.get("type", "Node"))
	var instance = ClassDB.instantiate(type_name)
	if not (instance is Node):
		return {"ok": false, "message": "Cannot instantiate node type: " + type_name}
	var node: Node = instance
	node.name = str(input.get("name", type_name))
	parent.add_child(node)
	node.owner = root
	if undo_manager != null:
		undo_manager.record_node_added(parent, node, root)
	return {"ok": true, "path": _relative_path(root, node), "type": type_name}


static func set_node_property(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var node := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if node == null:
		return {"ok": false, "message": "Node not found"}
	var property := str(input.get("property", ""))
	var before = node.get(property)
	var after = input.get("value")
	if undo_manager != null:
		undo_manager.record_node_property(node, property, before, after)
	else:
		node.set(property, after)
	return {"ok": true, "path": _relative_path(root, node), "property": property}


static func delete_node(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var path := str(input.get("path", ""))
	var node := root.get_node_or_null(NodePath(path))
	if node == null:
		return {"ok": false, "message": "Node not found: " + path}
	if node == root:
		return {"ok": false, "message": "Cannot delete the scene root"}
	var parent := node.get_parent()
	if parent == null:
		return {"ok": false, "message": "Node has no parent"}
	var index := node.get_index()
	if undo_manager != null:
		undo_manager.record_node_removed(parent, node, index)
	else:
		parent.remove_child(node)
	return {"ok": true, "path": path}


static func reparent_node(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var path := str(input.get("path", ""))
	var node := root.get_node_or_null(NodePath(path))
	if node == null:
		return {"ok": false, "message": "Node not found: " + path}
	if node == root:
		return {"ok": false, "message": "Cannot reparent the scene root"}
	var new_parent_path := str(input.get("new_parent_path", "."))
	var new_parent: Node = root if new_parent_path in [".", "", str(root.get_path())] else root.get_node_or_null(NodePath(new_parent_path))
	if new_parent == null:
		return {"ok": false, "message": "New parent not found: " + new_parent_path}
	if new_parent == node or node.is_ancestor_of(new_parent):
		return {"ok": false, "message": "Cannot reparent a node under its own descendant"}
	var old_parent := node.get_parent()
	var old_index := node.get_index()
	if undo_manager != null:
		undo_manager.record_node_reparented(node, old_parent, old_index, new_parent, root)
	else:
		old_parent.remove_child(node)
		new_parent.add_child(node)
		node.owner = root
	return {"ok": true, "path": _relative_path(root, node)}


static func rename_node(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var path := str(input.get("path", ""))
	var node := root.get_node_or_null(NodePath(path))
	if node == null:
		return {"ok": false, "message": "Node not found: " + path}
	if node == root:
		return {"ok": false, "message": "Cannot rename the scene root"}
	var new_name := str(input.get("name", input.get("new_name", "")))
	if new_name.strip_edges() == "":
		return {"ok": false, "message": "name is required"}
	var before_name := str(node.name)
	if undo_manager != null:
		undo_manager.record_node_renamed(node, before_name, new_name)
	else:
		node.name = new_name
	return {"ok": true, "path": _relative_path(root, node), "before_name": before_name, "after_name": new_name}


static func _node_to_dict(root: Node, node: Node, depth: int, max_depth: int) -> Dictionary:
	var children: Array = []
	if depth < max_depth:
		for child in node.get_children():
			if child is Node:
				children.append(_node_to_dict(root, child, depth + 1, max_depth))
	return {
		"name": node.name,
		"path": _relative_path(root, node),
		"type": node.get_class(),
		"children": children
	}
