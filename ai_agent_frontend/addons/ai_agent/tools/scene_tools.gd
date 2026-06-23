@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")


## 节点路径相对于"被编辑场景的根节点"而非 `node.get_path()` 的 SceneTree 绝对路径。
## 在编辑器里运行时，被编辑场景是挂在编辑器自身视口树很深的位置下的，
## `node.get_path()` 会把整条 `/root/@EditorNode@.../@SubViewport@.../` 编辑器内部
## 路径都吐出来——又长又会随编辑器布局变化，不适合展示给用户，也不该塞进模型上下文。
static func _relative_path(root: Node, node: Node) -> String:
	return str(root.get_path_to(node))


## 将工具协议中的本地坐标转换成 Godot 的 Vector2/Vector3，并拒绝不支持空间坐标的节点。
static func _apply_optional_position(node: Node, input: Dictionary) -> Dictionary:
	if not input.has("position"):
		return {}
	var position_value = input.get("position")
	if not position_value is Dictionary:
		return {"ok": false, "message": "position must be an object with numeric x/y[/z] fields", "error_code": "invalid_position"}
	var position: Dictionary = position_value
	for component in ["x", "y"]:
		if not position.has(component) or typeof(position[component]) not in [TYPE_INT, TYPE_FLOAT]:
			return {"ok": false, "message": "position.%s must be a number" % component, "error_code": "invalid_position"}
	if node is Node2D:
		(node as Node2D).position = Vector2(float(position["x"]), float(position["y"]))
		return {}
	if node is Node3D:
		var z_value = position.get("z", 0.0)
		if typeof(z_value) not in [TYPE_INT, TYPE_FLOAT]:
			return {"ok": false, "message": "position.z must be a number", "error_code": "invalid_position"}
		(node as Node3D).position = Vector3(float(position["x"]), float(position["y"]), float(z_value))
		return {}
	return {
		"ok": false,
		"message": "position is only supported for Node2D or Node3D roots; got " + node.get_class(),
		"error_code": "position_unsupported",
	}


static func _node_position_payload(node: Node) -> Dictionary:
	if node is Node2D:
		var position_2d := (node as Node2D).position
		return {"x": position_2d.x, "y": position_2d.y}
	if node is Node3D:
		var position_3d := (node as Node3D).position
		return {"x": position_3d.x, "y": position_3d.y, "z": position_3d.z}
	return {}


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
	var position_error := _apply_optional_position(node, input)
	if not position_error.is_empty():
		return position_error
	parent.add_child(node)
	node.owner = root
	if undo_manager != null:
		undo_manager.record_node_added(parent, node, root)
	return {
		"ok": true,
		"path": _relative_path(root, node),
		"type": type_name,
		"position": _node_position_payload(node),
	}


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


static func instance_scene(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var parent_path := str(input.get("parent_path", "."))
	var parent: Node = root if parent_path in [".", "", str(root.get_path())] else root.get_node_or_null(NodePath(parent_path))
	if parent == null:
		return {"ok": false, "message": "Parent not found: " + parent_path}
	var scene_path := PathUtils.to_res_path(str(input.get("scene_path", "")))
	if scene_path == "" or not (scene_path.get_extension().to_lower() in ["tscn", "scn"]):
		return {
			"ok": false,
			"message": "scene_path must be a project-relative .tscn/.scn file",
			"error_code": "invalid_path"
		}
	if not FileAccess.file_exists(scene_path):
		return {"ok": false, "message": "scene file not found: " + scene_path, "error_code": "scene_not_found"}
	if scene_path == str(root.scene_file_path):
		return {"ok": false, "message": "Cannot instance the currently edited scene into itself", "error_code": "self_instance"}
	var packed = load(scene_path)
	if not (packed is PackedScene):
		return {"ok": false, "message": "Failed to load as PackedScene: " + scene_path, "error_code": "load_failed"}
	var instance := (packed as PackedScene).instantiate()
	if instance == null:
		return {"ok": false, "message": "Failed to instantiate scene: " + scene_path, "error_code": "instantiate_failed"}
	var node: Node = instance
	if input.has("name"):
		node.name = str(input.get("name"))
	var position_error := _apply_optional_position(node, input)
	if not position_error.is_empty():
		return position_error
	parent.add_child(node)
	_set_owner_recursive(node, root)
	if undo_manager != null:
		undo_manager.record_node_added(parent, node, root)
	return {
		"ok": true,
		"path": _relative_path(root, node),
		"scene_path": scene_path,
		"position": _node_position_payload(node),
	}


static func duplicate_node(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var path := str(input.get("path", ""))
	var node := root.get_node_or_null(NodePath(path))
	if node == null:
		return {"ok": false, "message": "Node not found: " + path}
	var parent := node.get_parent()
	if parent == null:
		return {"ok": false, "message": "Node has no parent"}
	## 15 = DUPLICATE_SIGNALS|DUPLICATE_GROUPS|DUPLICATE_SCRIPTS|DUPLICATE_USE_INSTANCING（Node.duplicate() 默认值）。
	var clone := node.duplicate(15)
	if clone == null:
		return {"ok": false, "message": "Failed to duplicate node: " + path, "error_code": "duplicate_failed"}
	if input.has("name"):
		clone.name = str(input.get("name"))
	var position_error := _apply_optional_position(clone, input)
	if not position_error.is_empty():
		return position_error
	parent.add_child(clone)
	_set_owner_recursive(clone, root)
	if undo_manager != null:
		undo_manager.record_node_added(parent, clone, root)
	return {
		"ok": true,
		"path": _relative_path(root, clone),
		"position": _node_position_payload(clone),
	}


static func connect_signal(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var source := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if source == null:
		return {"ok": false, "message": "Source node not found: " + str(input.get("path", ""))}
	var signal_name := str(input.get("signal", ""))
	if signal_name == "" or not source.has_signal(signal_name):
		return {"ok": false, "message": "Source node has no signal: " + signal_name, "error_code": "signal_not_found"}
	var target := root.get_node_or_null(NodePath(str(input.get("target_path", ""))))
	if target == null:
		return {"ok": false, "message": "Target node not found: " + str(input.get("target_path", ""))}
	var method_name := str(input.get("method", ""))
	if method_name == "" or not target.has_method(method_name):
		return {"ok": false, "message": "Target node has no method: " + method_name, "error_code": "method_not_found"}
	var callable := Callable(target, method_name)
	if source.is_connected(signal_name, callable):
		return {"ok": false, "message": "Already connected", "error_code": "already_connected"}
	if undo_manager != null:
		undo_manager.record_signal_connected(source, signal_name, target, method_name)
	else:
		source.connect(signal_name, callable, CONNECT_PERSIST)
	return {
		"ok": true,
		"path": _relative_path(root, source),
		"signal": signal_name,
		"target_path": _relative_path(root, target),
		"method": method_name
	}


static func disconnect_signal(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var source := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if source == null:
		return {"ok": false, "message": "Source node not found: " + str(input.get("path", ""))}
	var target := root.get_node_or_null(NodePath(str(input.get("target_path", ""))))
	if target == null:
		return {"ok": false, "message": "Target node not found: " + str(input.get("target_path", ""))}
	var signal_name := str(input.get("signal", ""))
	var method_name := str(input.get("method", ""))
	var callable := Callable(target, method_name)
	if not source.is_connected(signal_name, callable):
		return {"ok": false, "message": "Not connected", "error_code": "not_connected"}
	if undo_manager != null:
		undo_manager.record_signal_disconnected(source, signal_name, target, method_name)
	else:
		source.disconnect(signal_name, callable)
	return {
		"ok": true,
		"path": _relative_path(root, source),
		"signal": signal_name,
		"target_path": _relative_path(root, target),
		"method": method_name
	}


static func add_to_group(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var node := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if node == null:
		return {"ok": false, "message": "Node not found: " + str(input.get("path", ""))}
	var group := str(input.get("group", "")).strip_edges()
	if group == "":
		return {"ok": false, "message": "group is required", "error_code": "group_required"}
	if node.is_in_group(group):
		return {"ok": false, "message": "Node is already in group: " + group, "error_code": "already_in_group"}
	if undo_manager != null:
		undo_manager.record_group_added(node, group)
	else:
		node.add_to_group(group, true)
	return {"ok": true, "path": _relative_path(root, node), "group": group}


static func remove_from_group(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var node := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if node == null:
		return {"ok": false, "message": "Node not found: " + str(input.get("path", ""))}
	var group := str(input.get("group", "")).strip_edges()
	if group == "" or not node.is_in_group(group):
		return {"ok": false, "message": "Node is not in group: " + group, "error_code": "not_in_group"}
	if undo_manager != null:
		undo_manager.record_group_removed(node, group)
	else:
		node.remove_from_group(group)
	return {"ok": true, "path": _relative_path(root, node), "group": group}


static func list_node_groups(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var node := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if node == null:
		return {"ok": false, "message": "Node not found: " + str(input.get("path", ""))}
	var groups: Array = []
	for group in node.get_groups():
		var group_name := str(group)
		if not group_name.begins_with("_"):
			groups.append(group_name)
	return {"ok": true, "path": _relative_path(root, node), "groups": groups}


## 扫描整棵被编辑场景树，按分组名汇总所有节点；与 list_node_groups（查单个节点属于哪些
## 分组）方向相反，用于"这个项目里到底用了哪些分组、分别挂在谁身上"这类问题。
static func list_groups(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var groups := {}
	_collect_groups(root, root, groups)
	var result: Array = []
	for group_name in groups.keys():
		result.append({"group": group_name, "node_paths": groups[group_name]})
	result.sort_custom(func(a: Dictionary, b: Dictionary): return str(a["group"]) < str(b["group"]))
	return {"ok": true, "groups": result}


static func _collect_groups(root: Node, node: Node, groups: Dictionary) -> void:
	for group in node.get_groups():
		var group_name := str(group)
		if group_name.begins_with("_"):
			continue
		if not groups.has(group_name):
			groups[group_name] = []
		groups[group_name].append(_relative_path(root, node))
	for child in node.get_children():
		if child is Node:
			_collect_groups(root, child, groups)


static func get_current_scene_path(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": true, "path": ""}
	return {"ok": true, "path": str(root.scene_file_path), "root_name": str(root.name), "root_type": root.get_class()}


static func list_node_signals(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var node := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if node == null:
		return {"ok": false, "message": "Node not found: " + str(input.get("path", ""))}
	var signals: Array = []
	for entry in node.get_signal_list():
		var args: Array = entry.get("args", [])
		signals.append({
			"name": str(entry.get("name", "")),
			"args": args.map(func(a): return str(a.get("name", "")))
		})
	return {"ok": true, "path": _relative_path(root, node), "signals": signals}


static func list_node_methods(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var node := root.get_node_or_null(NodePath(str(input.get("path", ""))))
	if node == null:
		return {"ok": false, "message": "Node not found: " + str(input.get("path", ""))}
	var methods: Array = []
	for entry in node.get_method_list():
		var method_name := str(entry.get("name", ""))
		if method_name.begins_with("_"):
			continue
		var args: Array = entry.get("args", [])
		methods.append({
			"name": method_name,
			"args": args.map(func(a): return str(a.get("name", "")))
		})
	return {"ok": true, "path": _relative_path(root, node), "methods": methods}


## 给 NavigationRegion2D/3D 烘焙导航网格。每次烘焙都换一个新的
## NavigationPolygon/NavigationMesh 资源实例（而不是在原对象上原地改数据），
## 这样才能配合 record_node_property 做正常的整体替换式 Undo/Redo。
static func bake_navigation_mesh(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var path := str(input.get("path", ""))
	var node := root.get_node_or_null(NodePath(path))
	if node == null:
		return {"ok": false, "message": "Node not found: " + path}

	if node is NavigationRegion2D:
		var region2d: NavigationRegion2D = node
		var before_polygon: Resource = region2d.navigation_polygon
		var baked_polygon := NavigationPolygon.new()
		region2d.navigation_polygon = baked_polygon
		region2d.bake_navigation_polygon(false)
		if undo_manager != null:
			undo_manager.record_node_property(region2d, "navigation_polygon", before_polygon, baked_polygon)
		return {
			"ok": true,
			"path": _relative_path(root, region2d),
			"type": "NavigationRegion2D",
			"outline_count": baked_polygon.get_outline_count()
		}
	if node is NavigationRegion3D:
		var region3d: NavigationRegion3D = node
		var before_mesh: Resource = region3d.navigation_mesh
		var baked_mesh := NavigationMesh.new()
		region3d.navigation_mesh = baked_mesh
		region3d.bake_navigation_mesh(false)
		if undo_manager != null:
			undo_manager.record_node_property(region3d, "navigation_mesh", before_mesh, baked_mesh)
		return {
			"ok": true,
			"path": _relative_path(root, region3d),
			"type": "NavigationRegion3D",
			"vertex_count": baked_mesh.get_vertices().size()
		}
	return {
		"ok": false,
		"message": "Node is not a NavigationRegion2D/NavigationRegion3D: " + path,
		"error_code": "invalid_node_type"
	}


static func save_scene(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var err := editor_interface.save_scene()
	if err != OK:
		return {"ok": false, "message": "Failed to save scene (error %d)" % err, "error_code": "save_failed"}
	return {"ok": true, "path": str(root.scene_file_path)}


static func list_open_scenes(editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var current_root := editor_interface.get_edited_scene_root()
	var current_path := str(current_root.scene_file_path) if current_root != null else ""
	var open_scenes: Array = []
	for path in editor_interface.get_open_scenes():
		open_scenes.append(str(path))
	return {"ok": true, "current_scene": current_path, "open_scenes": open_scenes}


## 截取编辑器当前 2D/3D 视口画面并存为 PNG，让 agent 能"看到"地图/UI/动画的实际效果。
static func capture_viewport_screenshot(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var mode := str(input.get("mode", "2d")).to_lower()
	var viewport: Viewport = null
	if mode == "3d":
		viewport = editor_interface.get_editor_viewport_3d(int(input.get("viewport_index", 0)))
	else:
		viewport = editor_interface.get_editor_viewport_2d()
	if viewport == null:
		return {"ok": false, "message": "Requested editor viewport is not available", "error_code": "viewport_unavailable"}

	var tree := editor_interface.get_base_control().get_tree()
	if tree != null:
		await tree.process_frame
		await tree.process_frame

	var image := viewport.get_texture().get_image()
	if image == null:
		return {"ok": false, "message": "Failed to capture viewport image", "error_code": "capture_failed"}

	var output_arg := str(input.get("output_path", "")).strip_edges()
	var output_path := ""
	var absolute := ""
	if output_arg == "":
		output_path = "user://ai_agent_screenshots/%d.png" % Time.get_ticks_usec()
		absolute = ProjectSettings.globalize_path(output_path)
	else:
		output_path = PathUtils.to_res_path(output_arg)
		if output_path == "":
			return {"ok": false, "message": "output_path must be a project-relative path", "error_code": "invalid_path"}
		if not PathUtils.is_write_allowed(output_path):
			return {"ok": false, "message": "output_path is not writable: " + output_path, "error_code": "path_denied"}
		absolute = ProjectSettings.globalize_path(output_path)

	DirAccess.make_dir_recursive_absolute(absolute.get_base_dir())
	var err := image.save_png(absolute)
	if err != OK:
		return {"ok": false, "message": "Failed to save screenshot (error %d)" % err, "error_code": "save_failed"}
	return {"ok": true, "path": output_path, "width": image.get_width(), "height": image.get_height()}


static func _set_owner_recursive(node: Node, owner: Node) -> void:
	node.owner = owner
	for child in node.get_children():
		if child is Node:
			_set_owner_recursive(child, owner)


## 切换编辑器当前打开/编辑的场景。会丢弃目标场景之外的未保存编辑器内编辑状态，
## 因此每次调用都需要用户确认（见 front_tools.py 里的 writes_project/needs_preview）。
static func open_scene(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	if path == "" or not (path.get_extension().to_lower() in ["tscn", "scn"]):
		return {
			"ok": false,
			"message": "path must be a project-relative .tscn/.scn scene file",
			"error_code": "invalid_path"
		}
	if not FileAccess.file_exists(path):
		return {"ok": false, "message": "scene file not found: " + path, "error_code": "scene_not_found"}
	editor_interface.open_scene_from_path(path)
	var root := editor_interface.get_edited_scene_root()
	if root == null or str(root.scene_file_path) != path:
		return {"ok": false, "message": "failed to open scene: " + path, "error_code": "open_failed"}
	return {"ok": true, "path": path, "root_name": str(root.name), "root_type": root.get_class()}


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
