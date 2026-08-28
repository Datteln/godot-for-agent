@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")

const _MAX_TARGET_PADDING := 256.0
const _MIN_3D_PADDING := 1.0
const _MAX_3D_PADDING := 3.0
static var _viewport_3d_leases: Dictionary = {}


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
	if node is Control:
		(node as Control).position = Vector2(float(position["x"]), float(position["y"]))
		return {}
	return {
		"ok": false,
		"message": (
			"%s has no spatial position property; omit \"position\" here. If you need to place it, " +
			"use set_node_property afterwards with a property this class actually supports (e.g. " +
			"\"offset\" on a 2D collision shape, or a custom transform property)."
		) % node.get_class(),
		"error_code": "position_unsupported",
	}


static func _node_position_payload(node: Node) -> Dictionary:
	if node is Node2D:
		var position_2d := (node as Node2D).position
		return {"x": position_2d.x, "y": position_2d.y}
	if node is Node3D:
		var position_3d := (node as Node3D).position
		return {"x": position_3d.x, "y": position_3d.y, "z": position_3d.z}
	if node is Control:
		var position_control := (node as Control).position
		return {"x": position_control.x, "y": position_control.y}
	return {}


static func _coerce_property_value(current_value: Variant, raw_value: Variant) -> Dictionary:
	var current_type := typeof(current_value)
	var accepts_resource_ref := current_type == TYPE_NIL or current_type == TYPE_OBJECT
	if accepts_resource_ref and raw_value is Dictionary and raw_value.has("_resource_path"):
		var ref_path := PathUtils.to_res_path(str(raw_value.get("_resource_path", "")))
		if ref_path == "" or not PathUtils.is_read_allowed(ref_path) or not FileAccess.file_exists(ref_path):
			return {"ok": false, "message": "resource reference is not readable: " + ref_path, "error_code": "invalid_resource_reference"}
		var loaded = load(ref_path)
		if not (loaded is Resource):
			return {"ok": false, "message": "resource reference is not a Resource: " + ref_path, "error_code": "invalid_resource_reference"}
		return {"ok": true, "value": loaded}
	if current_value is Vector2:
		return _coerce_vector2(raw_value)
	if current_value is Vector2i:
		var vector2_result := _coerce_vector2(raw_value)
		if not bool(vector2_result.get("ok", false)):
			return vector2_result
		var vector2: Vector2 = vector2_result["value"]
		return {"ok": true, "value": Vector2i(roundi(vector2.x), roundi(vector2.y))}
	if current_value is Vector3:
		return _coerce_vector3(raw_value)
	if current_value is Vector3i:
		var vector3_result := _coerce_vector3(raw_value)
		if not bool(vector3_result.get("ok", false)):
			return vector3_result
		var vector3: Vector3 = vector3_result["value"]
		return {"ok": true, "value": Vector3i(roundi(vector3.x), roundi(vector3.y), roundi(vector3.z))}
	if current_value is Color:
		return _coerce_color(raw_value)
	if current_value is NodePath:
		return {"ok": true, "value": NodePath(str(raw_value))}
	if current_value is StringName:
		return {"ok": true, "value": StringName(str(raw_value))}
	match typeof(current_value):
		TYPE_INT:
			return {"ok": true, "value": int(raw_value)}
		TYPE_FLOAT:
			return {"ok": true, "value": float(raw_value)}
		TYPE_BOOL:
			return {"ok": true, "value": bool(raw_value)}
		TYPE_STRING:
			return {"ok": true, "value": str(raw_value)}
	return {"ok": true, "value": raw_value}


static func _coerce_vector2(value: Variant) -> Dictionary:
	if value is Dictionary:
		if not _has_numeric_components(value, ["x", "y"]):
			return {"ok": false, "message": "Vector2 value must include numeric x/y fields", "error_code": "invalid_vector"}
		return {"ok": true, "value": Vector2(float(value["x"]), float(value["y"]))}
	if value is Array and value.size() >= 2:
		if typeof(value[0]) in [TYPE_INT, TYPE_FLOAT] and typeof(value[1]) in [TYPE_INT, TYPE_FLOAT]:
			return {"ok": true, "value": Vector2(float(value[0]), float(value[1]))}
	return {
		"ok": false,
		"message": (
			"Vector2 value must be an object {x,y} or array [x,y], not a JSON-encoded string. " +
			"You passed %s; use a real object/array instead, e.g. {\"x\": 1400, \"y\": -60} or [1400, -60]."
		) % JSON.stringify(value),
		"error_code": "invalid_vector",
	}


static func _coerce_vector3(value: Variant) -> Dictionary:
	if value is Dictionary:
		if not _has_numeric_components(value, ["x", "y"]):
			return {"ok": false, "message": "Vector3 value must include numeric x/y fields", "error_code": "invalid_vector"}
		var z_value = value.get("z", 0.0)
		if typeof(z_value) not in [TYPE_INT, TYPE_FLOAT]:
			return {"ok": false, "message": "Vector3.z must be a number", "error_code": "invalid_vector"}
		return {"ok": true, "value": Vector3(float(value["x"]), float(value["y"]), float(z_value))}
	if value is Array and value.size() >= 3:
		if typeof(value[0]) in [TYPE_INT, TYPE_FLOAT] and typeof(value[1]) in [TYPE_INT, TYPE_FLOAT] and typeof(value[2]) in [TYPE_INT, TYPE_FLOAT]:
			return {"ok": true, "value": Vector3(float(value[0]), float(value[1]), float(value[2]))}
	return {
		"ok": false,
		"message": (
			"Vector3 value must be an object {x,y,z} or array [x,y,z], not a JSON-encoded string. " +
			"You passed %s; use a real object/array instead, e.g. {\"x\": 1, \"y\": 2, \"z\": 0} or [1, 2, 0]."
		) % JSON.stringify(value),
		"error_code": "invalid_vector",
	}


static func _coerce_color(value: Variant) -> Dictionary:
	if value is Dictionary:
		if not _has_numeric_components(value, ["r", "g", "b"]):
			return {"ok": false, "message": "Color value must include numeric r/g/b fields", "error_code": "invalid_color"}
		var a_value = value.get("a", 1.0)
		if typeof(a_value) not in [TYPE_INT, TYPE_FLOAT]:
			return {"ok": false, "message": "Color.a must be a number", "error_code": "invalid_color"}
		return {"ok": true, "value": Color(float(value["r"]), float(value["g"]), float(value["b"]), float(a_value))}
	return {
		"ok": false,
		"message": (
			"Color value must be an object {r,g,b,a?} with components in 0..1, not a hex string or " +
			"JSON-encoded string. You passed %s; use a real object instead, e.g. " +
			"{\"r\": 1.0, \"g\": 0.0, \"b\": 0.0, \"a\": 1.0}."
		) % JSON.stringify(value),
		"error_code": "invalid_color",
	}


static func _has_numeric_components(value: Dictionary, components: Array) -> bool:
	for component in components:
		if not value.has(component) or typeof(value[component]) not in [TYPE_INT, TYPE_FLOAT]:
			return false
	return true


static func _json_safe_value(value: Variant) -> Variant:
	if value is Vector2:
		return {"x": value.x, "y": value.y}
	if value is Vector2i:
		return {"x": value.x, "y": value.y}
	if value is Vector3:
		return {"x": value.x, "y": value.y, "z": value.z}
	if value is Vector3i:
		return {"x": value.x, "y": value.y, "z": value.z}
	if value is Color:
		return {"r": value.r, "g": value.g, "b": value.b, "a": value.a}
	if value is Resource:
		return {"_type": "Resource", "class": value.get_class(), "path": str(value.resource_path)}
	if value is Object:
		return {"_type": "Object", "class": value.get_class()}
	if value is Array:
		var out: Array = []
		for item in value:
			out.append(_json_safe_value(item))
		return out
	if value is Dictionary:
		var out_dict := {}
		for key in value.keys():
			out_dict[str(key)] = _json_safe_value(value[key])
		return out_dict
	return value


static func _variant_matches(actual: Variant, expected: Variant, tolerance: float) -> bool:
	var coerced := _coerce_property_value(actual, expected)
	if bool(coerced.get("ok", false)):
		expected = coerced["value"]
	if actual is Resource and expected is Dictionary and expected.has("_resource_path"):
		return str(actual.resource_path) == PathUtils.to_res_path(str(expected.get("_resource_path", "")))
	if actual is Vector2 and expected is Vector2:
		return actual.distance_to(expected) <= tolerance
	if actual is Vector2i and expected is Vector2i:
		return actual == expected
	if actual is Vector3 and expected is Vector3:
		return actual.distance_to(expected) <= tolerance
	if actual is Vector3i and expected is Vector3i:
		return actual == expected
	if actual is Color and expected is Color:
		return (
			abs(actual.r - expected.r) <= tolerance
			and abs(actual.g - expected.g) <= tolerance
			and abs(actual.b - expected.b) <= tolerance
			and abs(actual.a - expected.a) <= tolerance
		)
	if typeof(actual) in [TYPE_INT, TYPE_FLOAT] and typeof(expected) in [TYPE_INT, TYPE_FLOAT]:
		return abs(float(actual) - float(expected)) <= tolerance
	if actual is Array and expected is Array:
		if actual.size() != expected.size():
			return false
		for index in range(actual.size()):
			if not _variant_matches(actual[index], expected[index], tolerance):
				return false
		return true
	if actual is Dictionary and expected is Dictionary:
		for key in expected.keys():
			if not actual.has(key) or not _variant_matches(actual[key], expected[key], tolerance):
				return false
		return true
	return actual == expected


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


static func validate_scene_state(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available", "error_code": "editor_unavailable"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root", "error_code": "no_scene_root"}
	var raw_checks = input.get("checks", [])
	if not (raw_checks is Array) or raw_checks.is_empty():
		return {"ok": false, "message": "checks must be a non-empty array", "error_code": "invalid_checks"}
	var tolerance := max(0.0, float(input.get("tolerance", 0.001)))
	var results: Array = []
	var failed := 0
	for index in range(raw_checks.size()):
		var raw_check = raw_checks[index]
		var result := _validate_scene_check(root, raw_check, index, tolerance)
		results.append(result)
		if not bool(result.get("ok", false)):
			failed += 1
	return {
		"ok": failed == 0,
		"passed": raw_checks.size() - failed,
		"failed": failed,
		"results": results,
	}


static func _validate_scene_check(root: Node, raw_check: Variant, index: int, tolerance: float) -> Dictionary:
	if not (raw_check is Dictionary):
		return {"ok": false, "index": index, "message": "check must be an object", "error_code": "invalid_check"}
	var check: Dictionary = raw_check
	var path := str(check.get("path", ""))
	var expect_exists := bool(check.get("exists", true))
	var node := root if path in [".", "", str(root.get_path())] else root.get_node_or_null(NodePath(path))
	var failures: Array = []
	var details := {
		"index": index,
		"path": path if path != "" else ".",
		"exists": node != null,
	}
	if node == null:
		if expect_exists:
			failures.append("node not found")
		details["failures"] = failures
		details["ok"] = failures.is_empty()
		return details
	if not expect_exists:
		failures.append("node exists but expected missing")
	details["actual_path"] = _relative_path(root, node)
	details["type"] = node.get_class()

	var expected_type := str(check.get("type", "")).strip_edges()
	if expected_type != "" and not node.is_class(expected_type):
		failures.append("type expected %s but got %s" % [expected_type, node.get_class()])

	var properties = check.get("properties", {})
	var property_details := {}
	if properties is Dictionary:
		for property in properties.keys():
			var property_name := str(property)
			var actual = node.get(property_name)
			var expected = properties[property]
			property_details[property_name] = _json_safe_value(actual)
			if not _variant_matches(actual, expected, tolerance):
				failures.append("property %s expected %s but got %s" % [property_name, JSON.stringify(_json_safe_value(expected)), JSON.stringify(_json_safe_value(actual))])
	details["properties"] = property_details

	var groups = check.get("groups", [])
	if groups is Array:
		for group in groups:
			if not node.is_in_group(str(group)):
				failures.append("missing group %s" % str(group))
	var absent_groups = check.get("not_groups", [])
	if absent_groups is Array:
		for group in absent_groups:
			if node.is_in_group(str(group)):
				failures.append("unexpected group %s" % str(group))

	var signals = check.get("signals", [])
	var signal_details: Array = []
	if signals is Array:
		for signal_check in signals:
			if not (signal_check is Dictionary):
				failures.append("signal check must be an object")
				continue
			var signal_name := str(signal_check.get("signal", ""))
			var target_path := str(signal_check.get("target_path", path))
			var method_name := str(signal_check.get("method", ""))
			var expect_connected := bool(signal_check.get("connected", true))
			var target := root if target_path in [".", "", str(root.get_path())] else root.get_node_or_null(NodePath(target_path))
			var connected := false
			if target != null and signal_name != "" and method_name != "" and node.has_signal(signal_name):
				connected = node.is_connected(signal_name, Callable(target, method_name))
			signal_details.append({
				"signal": signal_name,
				"target_path": target_path,
				"method": method_name,
				"connected": connected,
			})
			if connected != expect_connected:
				failures.append("signal %s -> %s.%s connected=%s expected %s" % [signal_name, target_path, method_name, connected, expect_connected])
	details["signals"] = signal_details
	details["failures"] = failures
	details["ok"] = failures.is_empty()
	return details


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
	var coerced := _coerce_property_value(before, input.get("value"))
	if not bool(coerced.get("ok", false)):
		return coerced
	var after = coerced["value"]
	if undo_manager != null:
		undo_manager.record_node_property(node, property, before, after)
	else:
		node.set(property, after)
	return {"ok": true, "path": _relative_path(root, node), "property": property, "before": _json_safe_value(before), "after": _json_safe_value(after)}


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
	if scene_path == "" or not PathUtils.is_res_path(scene_path) or not (scene_path.get_extension().to_lower() in ["tscn", "scn"]):
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
	node.owner = root
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
	if str(clone.scene_file_path) == "":
		_set_owner_preserving_scene_instances(clone, root)
	else:
		clone.owner = root
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
	if str(root.scene_file_path).strip_edges() == "":
		return {"ok": false, "message": "Current scene has no file path; save it in the editor first, then run save_scene again.", "error_code": "scene_path_required"}
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
	var target_value := input.get("target", {})
	if not (target_value is Dictionary):
		return {"ok": false, "message": "target must be an object", "error_code": "invalid_target"}
	var target: Dictionary = target_value
	if target.is_empty():
		return await _capture_viewport_image(viewport, input, {})
	if mode == "3d":
		return await _capture_3d_target(viewport, target, input, editor_interface)
	return await _capture_2d_target(viewport, target, input, editor_interface)


static func _capture_2d_target(viewport: Viewport, target: Dictionary, input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var resolved := _resolve_2d_target(target, viewport, editor_interface)
	if not bool(resolved.get("ok", false)):
		return resolved
	var prior_transform: Transform2D = viewport.canvas_transform
	var changed_transform := false
	var target_rect: Rect2 = resolved["world_rect"]
	if bool(resolved.get("needs_frame", false)):
		viewport.canvas_transform = _canvas_transform_for_rect(target_rect, viewport.get_visible_rect().size, float(resolved.get("padding", 16.0)))
		changed_transform = true
	var result := await _capture_viewport_image(viewport, input, resolved)
	if changed_transform:
		viewport.canvas_transform = prior_transform
	if bool(result.get("ok", false)):
		result["spatial_facts"] = resolved.get("spatial_facts", {})
		result["requested_target"] = target.duplicate(true)
		result["resolved_target"] = {
			"capture_scope": resolved.get("capture_scope", "current_viewport"),
			"world_rect": _rect2_payload(target_rect),
		}
	return result


static func _capture_3d_target(viewport: Viewport, target: Dictionary, input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if str(target.get("type", "")) != "node_3d":
		return {"ok": false, "message": "3D target.type must be node_3d", "error_code": "invalid_target"}
	var viewport_index := int(target.get("viewport_index", input.get("viewport_index", 0)))
	var lease_key := str(viewport.get_instance_id())
	if _viewport_3d_leases.has(lease_key):
		return {"ok": false, "message": "A 3D framing transaction is already active for this viewport", "error_code": "viewport_busy"}
	var resolved := _resolve_3d_target(target, editor_interface)
	if not bool(resolved.get("ok", false)):
		return resolved
	var camera := viewport.get_camera_3d()
	if camera == null:
		return {"ok": false, "message": "The editor 3D camera is not available through this viewport", "error_code": "target_unavailable"}
	var lease := "%s:%d" % [lease_key, Time.get_ticks_usec()]
	_viewport_3d_leases[lease_key] = lease
	var prior_transform: Transform3D = camera.global_transform
	var prior_projection := int(camera.projection)
	var prior_fov := float(camera.fov)
	var prior_size := float(camera.size)
	var applied_transform := _camera_transform_for_aabb(camera, resolved["world_aabb"], viewport.get_visible_rect().size, target)
	if applied_transform == null:
		_viewport_3d_leases.erase(lease_key)
		return {"ok": false, "message": "Could not calculate a finite editor camera transform", "error_code": "bounds_unavailable"}
	camera.global_transform = applied_transform
	var result := await _capture_viewport_image(viewport, input, resolved)
	var lease_is_current := str(_viewport_3d_leases.get(lease_key, "")) == lease
	var user_changed_view := not camera.global_transform.is_equal_approx(applied_transform)
	if lease_is_current:
		_viewport_3d_leases.erase(lease_key)
		if not user_changed_view:
			camera.projection = prior_projection
			camera.fov = prior_fov
			camera.size = prior_size
			camera.global_transform = prior_transform
	if user_changed_view:
		return {"ok": false, "message": "The editor 3D view changed while target capture was active", "error_code": "editor_view_changed"}
	if bool(result.get("ok", false)):
		var facts: Dictionary = resolved.get("spatial_facts", {})
		facts["camera"] = {
			"coordinate_space": "world_3d",
			"source": "editor_viewport_camera",
			"available": true,
			"viewport_index": viewport_index,
			"transform": _transform3d_payload(applied_transform),
			"projection": prior_projection,
			"fov": prior_fov,
			"size": prior_size,
		}
		result["spatial_facts"] = facts
	return result


static func _capture_viewport_image(viewport: Viewport, input: Dictionary, resolved: Dictionary) -> Dictionary:
	var tree := viewport.get_tree()
	if tree != null:
		await tree.process_frame
		await tree.process_frame
	var image := viewport.get_texture().get_image()
	if image == null:
		return {"ok": false, "message": "Failed to capture viewport image", "error_code": "capture_failed"}
	var crop: Rect2i = resolved.get("crop_rect_px", Rect2i())
	if crop.size.x > 0 and crop.size.y > 0:
		var image_rect := Rect2i(Vector2i.ZERO, image.get_size())
		crop = crop.intersection(image_rect)
		if crop.size.x <= 0 or crop.size.y <= 0:
			return {"ok": false, "message": "Resolved target is outside the captured viewport", "error_code": "target_outside_viewport"}
		image = image.get_region(crop)
	var output := _write_screenshot_image(image, input)
	if bool(output.get("ok", false)):
		output["capture_scope"] = resolved.get("capture_scope", "current_viewport")
		if crop.size.x > 0 and crop.size.y > 0:
			output["viewport_rect_px"] = _rect2i_payload(crop)
	return output


static func _write_screenshot_image(image: Image, input: Dictionary) -> Dictionary:
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
	var hasher := HashingContext.new()
	hasher.start(HashingContext.HASH_SHA256)
	hasher.update(image.get_data())
	return {"ok": true, "path": output_path, "absolute_path": absolute, "width": image.get_width(), "height": image.get_height(), "image_hash": hasher.finish().hex_encode(), "captured_at_unix_ms": Time.get_unix_time_from_system() * 1000.0}


static func _resolve_2d_target(target: Dictionary, viewport: Viewport, editor_interface: EditorInterface) -> Dictionary:
	var target_type := str(target.get("type", ""))
	var viewport_size := viewport.get_visible_rect().size
	if target_type == "viewport_rect":
		var rect_result := _rect2_from_input(target.get("rect", {}))
		if not bool(rect_result.get("ok", false)):
			return rect_result
		var rect: Rect2 = rect_result["rect"]
		return {
			"ok": true, "world_rect": rect, "crop_rect_px": Rect2i(rect.position, rect.size), "capture_scope": "viewport_rect",
			"spatial_facts": {"viewport_rect_px": {"coordinate_space": "viewport_px", "source": "request", "available": true, "value": _rect2_payload(rect)}}
		}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root", "error_code": "scene_unavailable"}
	var scene_path := str(root.scene_file_path)
	var path := str(target.get("path", "")).strip_edges()
	var node := root.get_node_or_null(NodePath(path))
	if node == null:
		return {"ok": false, "message": "Target node not found: " + path, "error_code": "target_missing"}
	var padding := clampf(float(target.get("padding", 16.0)), 0.0, _MAX_TARGET_PADDING)
	var world_rect := Rect2()
	var fact_key := "canvas_rect"
	var facts: Dictionary = {}
	var region: Dictionary = {}
	if target_type == "canvas_item":
		if not (node is CanvasItem):
			return {"ok": false, "message": "canvas_item target must resolve to CanvasItem", "error_code": "invalid_target"}
		var bounds := _canvas_item_world_rect(node)
		if not bool(bounds.get("ok", false)):
			return bounds
		world_rect = bounds["rect"].grow(padding)
		facts[fact_key] = {"coordinate_space": "canvas", "source": "scene_node", "available": true, "value": _rect2_payload(world_rect)}
	elif target_type == "map_region":
		region = _tilemap_world_rect(node, target)
		if not bool(region.get("ok", false)):
			return region
		world_rect = region["rect"].grow(padding)
		facts = region["spatial_facts"]
	else:
		return {"ok": false, "message": "2D target.type must be viewport_rect, canvas_item, or map_region", "error_code": "invalid_target"}
	if world_rect.size.x <= 0.0 or world_rect.size.y <= 0.0:
		return {"ok": false, "message": "Target has no finite 2D bounds", "error_code": "bounds_unavailable"}
	var initial_rect := _world_rect_to_viewport(world_rect, viewport.canvas_transform)
	var needs_frame := not Rect2(Vector2.ZERO, viewport_size).encloses(initial_rect)
	var final_transform := _canvas_transform_for_rect(world_rect, viewport_size, padding) if needs_frame else viewport.canvas_transform
	var crop_rect := _world_rect_to_viewport(world_rect, final_transform).grow(padding)
	if target_type == "map_region":
		var map_rect := _world_rect_to_viewport(region["rect"], final_transform)
		if map_rect.size.x <= 0.0 or map_rect.size.y <= 0.0 or not crop_rect.intersects(map_rect):
			return {"ok": false, "message": "Focused map crop does not intersect the requested map bounds", "error_code": "map_crop_mismatch"}
		facts["focused_capture"] = {"coordinate_space": "viewport_px", "source": "resolved_target", "available": true, "value": "intersects_requested_map_bounds"}
	facts["viewport_rect_px"] = {"coordinate_space": "viewport_px", "source": "resolved_target", "available": true, "value": _rect2_payload(crop_rect)}
	facts["scene_path"] = {"coordinate_space": "scene", "source": "editor", "available": not scene_path.is_empty(), "value": scene_path}
	return {"ok": true, "world_rect": world_rect, "crop_rect_px": Rect2i(Vector2i(crop_rect.position.floor()), Vector2i(crop_rect.size.ceil())), "needs_frame": needs_frame, "padding": padding, "capture_scope": target_type, "spatial_facts": facts}


static func _resolve_3d_target(target: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root", "error_code": "scene_unavailable"}
	var path := str(target.get("path", "")).strip_edges()
	var node := root.get_node_or_null(NodePath(path))
	if node == null:
		return {"ok": false, "message": "Target node not found: " + path, "error_code": "target_missing"}
	if not (node is Node3D):
		return {"ok": false, "message": "node_3d target must resolve to Node3D", "error_code": "invalid_target"}
	var aabb_result := _visible_world_aabb(node)
	if not bool(aabb_result.get("ok", false)):
		return aabb_result
	var target_node: Node3D = node
	var world_aabb: AABB = aabb_result["aabb"]
	var target_path := _relative_path(root, node)
	return {"ok": true, "world_aabb": world_aabb, "capture_scope": "node_3d", "spatial_facts": {"world_aabb": {"coordinate_space": "world_3d", "source": "VisualInstance3D.get_aabb", "available": true, "value": _aabb_payload(world_aabb)}, "target_origin": {"coordinate_space": "world_3d", "source": "Node3D.global_position", "available": true, "value": _vector3_payload(target_node.global_position)}, "target": target_path, "scene_path": str(root.scene_file_path)}}


static func _canvas_item_world_rect(node: Node) -> Dictionary:
	if node is Control:
		var control: Control = node
		return {"ok": true, "rect": control.get_global_rect()}
	if node is Node2D and node.has_method("get_rect"):
		var local_rect: Rect2 = node.call("get_rect")
		var node_2d: Node2D = node
		return {"ok": true, "rect": node_2d.global_transform * local_rect}
	return {"ok": false, "message": "Target CanvasItem has no finite drawable rect", "error_code": "bounds_unavailable"}


static func _tilemap_world_rect(node: Node, target: Dictionary) -> Dictionary:
	if not node.has_method("map_to_local"):
		return {"ok": false, "message": "map_region target must resolve to TileMapLayer or TileMap", "error_code": "invalid_target"}
	if not target.has("map_layer") or not (target.get("map_layer") is int):
		return {"ok": false, "message": "map_region requires an explicit integer map_layer", "error_code": "missing_map_layer"}
	var map_layer := int(target.get("map_layer"))
	if node.has_method("get_layers_count"):
		var layer_count := int(node.call("get_layers_count"))
		if map_layer < 0 or map_layer >= layer_count:
			return {"ok": false, "message": "map_region.map_layer is outside the TileMap layer range", "error_code": "invalid_map_layer"}
	elif map_layer != 0:
		return {"ok": false, "message": "TileMapLayer only accepts map_layer 0", "error_code": "invalid_map_layer"}
	var bounds_value := target.get("cell_bounds", {})
	if not (bounds_value is Dictionary):
		return {"ok": false, "message": "map_region.cell_bounds must be an object", "error_code": "invalid_target"}
	var bounds: Dictionary = bounds_value
	for field in ["x", "y", "width", "height"]:
		if not (bounds.get(field) is int):
			return {"ok": false, "message": "map_region.cell_bounds must contain finite integer x, y, width, and height", "error_code": "invalid_target"}
	var width := int(bounds.get("width", 0))
	var height := int(bounds.get("height", 0))
	if width <= 0 or height <= 0:
		return {"ok": false, "message": "map_region.cell_bounds width and height must be positive", "error_code": "invalid_target"}
	var start := Vector2i(int(bounds.get("x", 0)), int(bounds.get("y", 0)))
	var end := start + Vector2i(width - 1, height - 1)
	var first: Vector2 = node.call("map_to_local", start)
	var last: Vector2 = node.call("map_to_local", end)
	var tile_size := Vector2(1.0, 1.0)
	if "tile_set" in node and node.tile_set != null:
		var size: Vector2i = node.tile_set.tile_size
		tile_size = Vector2(size)
	var local_rect := Rect2(first - tile_size * 0.5, (last - first).abs() + tile_size)
	var world_rect := local_rect
	if node is Node2D:
		world_rect = (node as Node2D).global_transform * local_rect
	return {"ok": true, "rect": world_rect, "spatial_facts": {"map_layer": {"coordinate_space": "map_layer", "source": "target", "available": true, "value": map_layer}, "cell_bounds": {"coordinate_space": "map_cells", "source": "request", "available": true, "value": {"x": start.x, "y": start.y, "width": width, "height": height}}, "map_local_rect": {"coordinate_space": "map_local", "source": "TileMap.map_to_local", "available": true, "value": _rect2_payload(local_rect)}}}


static func _visible_world_aabb(root: Node) -> Dictionary:
	var boxes: Array[AABB] = []
	_collect_visible_aabbs(root, boxes)
	if boxes.is_empty():
		return {"ok": false, "message": "Target Node3D has no visible geometry with finite bounds", "error_code": "target_not_visual"}
	var merged: AABB = boxes[0]
	for box in boxes.slice(1):
		merged = merged.merge(box)
	if not _finite_vector3(merged.position) or not _finite_vector3(merged.end):
		return {"ok": false, "message": "Target world bounds are not finite", "error_code": "bounds_unavailable"}
	return {"ok": true, "aabb": merged}


static func _collect_visible_aabbs(node: Node, boxes: Array[AABB]) -> void:
	if node is VisualInstance3D:
		var visual: VisualInstance3D = node
		if visual.visible:
			var local_box := visual.get_aabb()
			if local_box.size.length() > 0.0:
				boxes.append(_transform_aabb(local_box, visual.global_transform))
	for child in node.get_children():
		if child is Node:
			_collect_visible_aabbs(child, boxes)


static func _transform_aabb(box: AABB, transform: Transform3D) -> AABB:
	var result := AABB(transform * box.position, Vector3.ZERO)
	for x in [0.0, box.size.x]:
		for y in [0.0, box.size.y]:
			for z in [0.0, box.size.z]:
				result = result.expand(transform * (box.position + Vector3(x, y, z)))
	return result


static func _camera_transform_for_aabb(camera: Camera3D, box: AABB, viewport_size: Vector2, target: Dictionary) -> Variant:
	if viewport_size.x <= 0.0 or viewport_size.y <= 0.0:
		return null
	var direction := _view_direction(str(target.get("view_direction", "current")), camera)
	if direction.length_squared() <= 0.000001:
		return null
	var center := box.get_center()
	var radius := maxf(0.01, box.size.length() * 0.5)
	var padding := clampf(float(target.get("padding", 1.2)), _MIN_3D_PADDING, _MAX_3D_PADDING)
	var aspect := viewport_size.x / viewport_size.y
	if int(camera.projection) == Camera3D.PROJECTION_ORTHOGONAL:
		camera.size = maxf(box.size.y, box.size.x / aspect) * padding
		return _look_at_transform(center - direction * maxf(radius * 2.0, 1.0), center)
	var vertical_fov := deg_to_rad(maxf(1.0, float(camera.fov)))
	var horizontal_fov := 2.0 * atan(tan(vertical_fov * 0.5) * aspect)
	var limiting_half_fov := maxf(0.01, minf(vertical_fov, horizontal_fov) * 0.5)
	var distance := radius * padding / sin(limiting_half_fov)
	return _look_at_transform(center - direction * distance, center)


static func _view_direction(value: String, camera: Camera3D) -> Vector3:
	match value:
		"front": return Vector3(0, 0, 1)
		"back": return Vector3(0, 0, -1)
		"left": return Vector3(-1, 0, 0)
		"right": return Vector3(1, 0, 0)
		"top": return Vector3(0, 1, 0)
		"bottom": return Vector3(0, -1, 0)
		"isometric": return Vector3(1, 1, 1).normalized()
		_: return -camera.global_transform.basis.z.normalized()


static func _look_at_transform(position: Vector3, target: Vector3) -> Transform3D:
	var transform := Transform3D(Basis.IDENTITY, position)
	transform = transform.looking_at(target, Vector3.UP)
	return transform


static func _canvas_transform_for_rect(rect: Rect2, viewport_size: Vector2, padding: float) -> Transform2D:
	var available := viewport_size - Vector2.ONE * maxf(0.0, padding * 2.0)
	var zoom := minf(available.x / rect.size.x, available.y / rect.size.y)
	zoom = clampf(zoom, 0.05, 16.0)
	return Transform2D(Vector2(zoom, 0.0), Vector2(0.0, zoom), viewport_size * 0.5 - rect.get_center() * zoom)


static func _world_rect_to_viewport(rect: Rect2, transform: Transform2D) -> Rect2:
	return transform * rect


static func _rect2_from_input(value: Variant) -> Dictionary:
	if not (value is Dictionary):
		return {"ok": false, "message": "rect must be an object with x/y/width/height", "error_code": "invalid_target"}
	var rect: Dictionary = value
	for key in ["x", "y", "width", "height"]:
		if typeof(rect.get(key)) not in [TYPE_INT, TYPE_FLOAT]:
			return {"ok": false, "message": "rect.%s must be numeric" % key, "error_code": "invalid_target"}
	if float(rect["width"]) <= 0.0 or float(rect["height"]) <= 0.0:
		return {"ok": false, "message": "rect width and height must be positive", "error_code": "invalid_target"}
	return {"ok": true, "rect": Rect2(float(rect["x"]), float(rect["y"]), float(rect["width"]), float(rect["height"]))}


static func _finite_vector3(value: Vector3) -> bool:
	return is_finite(value.x) and is_finite(value.y) and is_finite(value.z)


static func _vector3_payload(value: Vector3) -> Dictionary:
	return {"x": value.x, "y": value.y, "z": value.z}


static func _rect2_payload(value: Rect2) -> Dictionary:
	return {"x": value.position.x, "y": value.position.y, "width": value.size.x, "height": value.size.y}


static func _rect2i_payload(value: Rect2i) -> Dictionary:
	return {"x": value.position.x, "y": value.position.y, "width": value.size.x, "height": value.size.y}


static func _aabb_payload(value: AABB) -> Dictionary:
	return {"position": _vector3_payload(value.position), "size": _vector3_payload(value.size)}


static func _transform3d_payload(value: Transform3D) -> Dictionary:
	return {"origin": _vector3_payload(value.origin), "basis": {"x": _vector3_payload(value.basis.x), "y": _vector3_payload(value.basis.y), "z": _vector3_payload(value.basis.z)}}


static func _set_owner_preserving_scene_instances(node: Node, owner: Node) -> void:
	node.owner = owner
	for child in node.get_children():
		if child is Node:
			child.owner = owner
			if str(child.scene_file_path) == "":
				_set_owner_preserving_scene_instances(child, owner)


## 切换编辑器当前打开/编辑的场景。会丢弃目标场景之外的未保存编辑器内编辑状态，
## 因此每次调用都需要用户确认（见 front_tools.py 里的 writes_project/needs_preview）。
static func open_scene(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	if path == "" or not PathUtils.is_res_path(path) or not (path.get_extension().to_lower() in ["tscn", "scn"]):
		return {
			"ok": false,
			"message": "path must be a project-relative .tscn/.scn scene file",
			"error_code": "invalid_path"
		}
	if not FileAccess.file_exists(path):
		return {"ok": false, "message": "scene file not found: " + path, "error_code": "scene_not_found"}
	editor_interface.open_scene_from_path(path)
	var tree := editor_interface.get_base_control().get_tree()
	if tree != null:
		await tree.process_frame
		await tree.process_frame
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
