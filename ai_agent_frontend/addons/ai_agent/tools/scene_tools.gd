@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")
const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")

## 视觉叶子节点 -> 它必须有内容才画得出来的那个资源属性。Sprite2D 没有 texture、MeshInstance3D
## 没有 mesh 时渲染完全不可见——add_node 建出来只是个空节点，工具返回 ok 看起来"成功了"，画面
## 上却什么都没有。这张表既用于 add_node 的建节点拦截，也用于截图时核对真实渲染状态。
const VISUAL_LEAF_RESOURCE_PROPERTY := {
	"Sprite2D": "texture",
	"Sprite3D": "texture",
	"AnimatedSprite2D": "sprite_frames",
	"AnimatedSprite3D": "sprite_frames",
	"MeshInstance3D": "mesh",
}


## 节点路径相对于"被编辑场景的根节点"而非 `node.get_path()` 的 SceneTree 绝对路径。
## 在编辑器里运行时，被编辑场景是挂在编辑器自身视口树很深的位置下的，
## `node.get_path()` 会把整条 `/root/@EditorNode@.../@SubViewport@.../` 编辑器内部
## 路径都吐出来——又长又会随编辑器布局变化，不适合展示给用户，也不该塞进模型上下文。
static func _relative_path(root: Node, node: Node) -> String:
	return str(root.get_path_to(node))


## 将工具协议中的本地坐标转换成 Godot 的 Vector2/Vector3，并拒绝不支持空间坐标的节点。
static func _apply_optional_position(node: Node, input: Dictionary, parent: Node, root: Node) -> Dictionary:
	if input.has("map_cell") and input.has("position"):
		return {"ok": false, "message": "map_cell and position are mutually exclusive", "error_code": "ambiguous_position"}
	if input.has("map_cell"):
		var map_path := str(input.get("target_path", "")).strip_edges()
		var map := root.get_node_or_null(NodePath(map_path)) if map_path != "" else _first_tilemap_2d(root)
		if map == null or not map.has_method("map_to_local") or not (map is Node2D):
			return {"ok": false, "message": "target_path must resolve to a 2D TileMap/TileMapLayer for map_cell placement", "error_code": "map_target_required"}
		var cell_value = _recover_json_encoded(input.get("map_cell"))
		if not cell_value is Dictionary:
			return {"ok": false, "message": "map_cell must be an object with integer x/y fields", "error_code": "invalid_map_cell"}
		var cell: Dictionary = cell_value
		if typeof(cell.get("x", null)) != TYPE_INT or typeof(cell.get("y", null)) != TYPE_INT:
			return {"ok": false, "message": "map_cell.x and map_cell.y must be integers", "error_code": "invalid_map_cell"}
		var local: Vector2 = map.call("map_to_local", Vector2i(int(cell["x"]), int(cell["y"])))
		var world: Vector2 = (map as Node2D).to_global(local)
		if not parent is Node2D:
			return {"ok": false, "message": "map_cell placement requires a Node2D parent", "error_code": "parent_type_mismatch"}
		(node as Node2D).position = (parent as Node2D).to_local(world)
		return {}
	if not input.has("position"):
		return {}
	var position_value = _recover_json_encoded(input.get("position"))
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


static func _first_tilemap_2d(node: Node) -> Node:
	var cls := node.get_class()
	if cls == "TileMapLayer" or cls == "TileMap":
		return node
	for child in node.get_children():
		if child is Node:
			var found := _first_tilemap_2d(child)
			if found != null:
				return found
	return null


## 把一个刚摆好的 2D 节点换算成它落在地图的哪一格，并把地图当前瓦片范围一并带出来。模型/用户
## 据此能立刻看出"这棵树其实落在第 6 列（关卡开头），不在我要扩展的 51..103 区间"——工具拿不到
## "本次任务的目标区间"，没法据此硬判错，但把真实落点格子摆到台面上就能戳穿这类放错位置。
## 只有当落点离地图瓦片范围远到一个地图自身尺寸的余量之外（基本是飘在空中的野坐标）才标 off_map
## 让调用方硬拒绝；地表上方放装饰、边缘外延一两格都属正常，不拦。
static func _placement_reference(root: Node, node: Node) -> Dictionary:
	if not (node is Node2D):
		return {}
	var map := _first_tilemap_2d(root)
	if map == null or not (map is Node2D):
		return {}
	var tile_set = map.get("tile_set")
	if tile_set == null:
		return {}
	var used: Rect2i = map.call("get_used_rect")
	if used.size.x <= 0 or used.size.y <= 0:
		return {}
	var tile_size: Vector2i = tile_set.tile_size
	if tile_size.x <= 0 or tile_size.y <= 0:
		return {}
	var local: Vector2 = (map as Node2D).to_local((node as Node2D).global_position)
	var mapped := map.call("local_to_map", local)
	var col := int(mapped.x)
	var row := int(mapped.y)
	var min_x := used.position.x
	var min_y := used.position.y
	var max_x := used.position.x + used.size.x - 1
	var max_y := used.position.y + used.size.y - 1
	var reference := {
		"map": _relative_path(root, map),
		"placed_at_tile": {"x": col, "y": row},
		"map_tile_bounds": {"min_x": min_x, "min_y": min_y, "max_x": max_x, "max_y": max_y},
	}
	var margin_x := maxi(8, used.size.x)
	var margin_y := maxi(8, used.size.y)
	if col < min_x - margin_x or col > max_x + margin_x or row < min_y - margin_y or row > max_y + margin_y:
		reference["off_map"] = true
	return reference


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


## 有些模型会把 {x,y}/{r,g,b,a} 这类嵌套对象再 JSON 编码成字符串传过来；
## 能解析出 Dictionary/Array 就当真值用，解析不出来原样返回，留给下面的类型检查去报错。
static func _recover_json_encoded(value: Variant) -> Variant:
	if not (value is String):
		return value
	var parsed: Variant = JSON.parse_string(value as String)
	if parsed is Dictionary or parsed is Array:
		return parsed
	return value


static func _coerce_vector2(value: Variant) -> Dictionary:
	value = _recover_json_encoded(value)
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
	value = _recover_json_encoded(value)
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
	value = _recover_json_encoded(value)
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


## 视觉叶子节点（Sprite2D 等）必须随 add_node 提供它的内容资源（参数 texture，res:// 路径），
## 否则在工具层直接拒绝——一个没有 texture 的 Sprite2D 是隐形的，建出来等于没建。提示改用
## instance_scene 实例化已经带美术的预制 .tscn，或在同一次调用里把资源路径传进来。
static func _apply_visual_resource(node: Node, type_name: String, input: Dictionary) -> Dictionary:
	if not VISUAL_LEAF_RESOURCE_PROPERTY.has(type_name):
		return {}
	var property := str(VISUAL_LEAF_RESOURCE_PROPERTY[type_name])
	var resource_arg := str(input.get("texture", "")).strip_edges()
	if resource_arg == "":
		return {
			"ok": false,
			"message": (
				"%s renders nothing without a %s resource; an empty %s node is invisible. Pass " +
				"\"texture\" (a res:// resource path) in this same add_node call, or use instance_scene " +
				"to instantiate a prefab .tscn that already carries its art."
			) % [type_name, property, type_name],
			"error_code": "visual_node_missing_resource",
		}
	if not ResourceLoader.exists(resource_arg):
		return {"ok": false, "message": "texture resource not found: " + resource_arg, "error_code": "resource_not_found"}
	var res := ResourceLoader.load(resource_arg)
	if res == null:
		return {"ok": false, "message": "failed to load texture resource: " + resource_arg, "error_code": "resource_load_failed"}
	node.set(property, res)
	if node.get(property) == null:
		return {"ok": false, "message": "resource at %s did not apply to %s.%s; wrong resource type?" % [resource_arg, type_name, property], "error_code": "resource_type_mismatch"}
	return {}


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
	var position_error := _apply_optional_position(node, input, parent, root)
	if not position_error.is_empty():
		return position_error
	var visual_error := _apply_visual_resource(node, type_name, input)
	if not visual_error.is_empty():
		return visual_error
	parent.add_child(node)
	node.owner = root
	var placement := _placement_reference(root, node)
	if bool(placement.get("off_map", false)):
		parent.remove_child(node)
		node.free()
		return {
			"ok": false,
			"message": "node would land at tile %s, far outside the map's tile bounds %s — likely a miscomputed coordinate (recompute pixel = node_position + tile*tile_size). Nothing was added." % [str(placement.get("placed_at_tile")), str(placement.get("map_tile_bounds"))],
			"error_code": "position_off_map",
			"placement": placement,
		}
	if undo_manager != null:
		undo_manager.record_node_added(parent, node, root)
	var result := {
		"ok": true,
		"path": _relative_path(root, node),
		"type": type_name,
		"position": _node_position_payload(node),
	}
	if not placement.is_empty():
		result["placement"] = placement
	return result


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
	node.owner = root
	var placement := _placement_reference(root, node)
	if bool(placement.get("off_map", false)):
		parent.remove_child(node)
		node.free()
		return {
			"ok": false,
			"message": "instance would land at tile %s, far outside the map's tile bounds %s — likely a miscomputed coordinate (recompute pixel = node_position + tile*tile_size). Nothing was added." % [str(placement.get("placed_at_tile")), str(placement.get("map_tile_bounds"))],
			"error_code": "position_off_map",
			"placement": placement,
		}
	if undo_manager != null:
		undo_manager.record_node_added(parent, node, root)
	var result := {
		"ok": true,
		"path": _relative_path(root, node),
		"scene_path": scene_path,
		"position": _node_position_payload(node),
	}
	if input.has("map_cell") and node is Node2D:
		var requested_cell = input.get("map_cell", {})
		var global_position := (node as Node2D).global_position
		result["requested_map_cell"] = requested_cell
		result["world_position"] = {"x": global_position.x, "y": global_position.y}
	if not placement.is_empty():
		result["placement"] = placement
	return result


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
	var position_error := _apply_optional_position(clone, input, parent, root)
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
		var result_2d := {
			"ok": true,
			"path": _relative_path(root, region2d),
			"type": "NavigationRegion2D",
			"outline_count": baked_polygon.get_outline_count()
		}
		if baked_polygon.get_outline_count() == 0:
			result_2d["warning"] = "NavigationRegion2D baked an empty NavigationPolygon; keep the scene change but fall back to structural validation/path checks."
			result_2d["fallback"] = {
				"tool": "validate_map_region",
				"path_algorithm": "astar",
				"reason": "empty_navigation_polygon",
			}
		return result_2d
	if node is NavigationRegion3D:
		var region3d: NavigationRegion3D = node
		var before_mesh: Resource = region3d.navigation_mesh
		var baked_mesh := NavigationMesh.new()
		region3d.navigation_mesh = baked_mesh
		region3d.bake_navigation_mesh(false)
		if undo_manager != null:
			undo_manager.record_node_property(region3d, "navigation_mesh", before_mesh, baked_mesh)
		var result_3d := {
			"ok": true,
			"path": _relative_path(root, region3d),
			"type": "NavigationRegion3D",
			"vertex_count": baked_mesh.get_vertices().size()
		}
		if baked_mesh.get_vertices().is_empty():
			result_3d["warning"] = "NavigationRegion3D baked an empty NavigationMesh; keep the scene change but fall back to structural validation/path checks."
			result_3d["fallback"] = {
				"tool": "validate_map_region",
				"path_algorithm": "astar",
				"reason": "empty_navigation_mesh",
			}
		return result_3d
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
## 可选 focus_node_path（任意 Node2D/Node3D 的场景内路径）或 focus_region+target_path
## （地图格子坐标区域，复用 map_tools 的 target 解析）让相机/2D 画布在截图前自动对准目标，
## 不再依赖用户手动把编辑器视口滚动到对的位置。
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

	var focus_result := _resolve_focus_points(input, editor_interface)
	if not bool(focus_result.get("ok", false)):
		return focus_result
	var focus_applied := {}
	var focus_points: Array = focus_result.get("points", [])
	if not focus_points.is_empty():
		var margin := maxf(1.0, float(input.get("focus_margin", 1.3)))
		var apply_result := _apply_camera_focus(viewport, mode, focus_points, margin)
		if not bool(apply_result.get("ok", false)):
			return apply_result
		focus_applied = apply_result

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
	var result := {"ok": true, "path": output_path, "absolute_path": absolute, "width": image.get_width(), "height": image.get_height()}
	if not focus_applied.is_empty():
		result["focus"] = focus_applied
	var scene_root := editor_interface.get_edited_scene_root()
	if scene_root != null:
		var render_state := _collect_render_state(scene_root)
		result["rendered_nodes"] = render_state.get("rendered", [])
		result["nodes_missing_visual_resource"] = render_state.get("missing", [])
	return result


## 截图是像素，模型容易"看着自己的操作记录说做过了"而不真正核对画面。这里随截图一并返回
## 场景里实际带可见资源、会画出像素的节点（rendered），以及"是视觉节点却没有资源、因此根本
## 画不出来"的节点（missing）。后者正好戳穿"add 了 Sprite2D 但没贴图、看似添加其实隐形"的假完成。
static func _collect_render_state(root: Node) -> Dictionary:
	var rendered: Array = []
	var missing: Array = []
	_walk_render_state(root, root, rendered, missing)
	return {"rendered": rendered, "missing": missing}


static func _walk_render_state(root: Node, node: Node, rendered: Array, missing: Array) -> void:
	if VISUAL_LEAF_RESOURCE_PROPERTY.has(node.get_class()):
		var property := str(VISUAL_LEAF_RESOURCE_PROPERTY[node.get_class()])
		var has_resource := node.get(property) != null
		var is_visible := true
		if node is CanvasItem:
			is_visible = (node as CanvasItem).visible
		elif node is Node3D:
			is_visible = (node as Node3D).visible
		var entry := {"path": _relative_path(root, node), "type": node.get_class()}
		if has_resource and is_visible:
			rendered.append(entry)
		elif not has_resource:
			missing.append(entry)
	for child in node.get_children():
		if child is Node:
			_walk_render_state(root, child, rendered, missing)


## 解析 focus_node_path（场景内任意 Node2D/Node3D 路径）或 focus_region+target_path
## （地图格子坐标区域，配合 map_tools._resolve_map_target 解析地图节点并用其
## map_to_local/to_global 转出真实世界坐标，而不是手算 tile_size/cell_size 乘法）。
## 两者都没传时返回空 points，调用方按"不需要对焦"处理。
static func _resolve_focus_points(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var focus_node_path := str(input.get("focus_node_path", "")).strip_edges()
	var region_value = input.get("focus_region", null)
	if focus_node_path == "" and not (region_value is Dictionary):
		return {"ok": true, "points": []}

	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No scene is currently being edited", "error_code": "no_edited_scene"}

	if focus_node_path != "":
		var node := root if focus_node_path == "." else root.get_node_or_null(NodePath(focus_node_path))
		if node == null:
			return {"ok": false, "message": "focus_node_path not found: " + focus_node_path, "error_code": "focus_node_not_found"}
		if node is Node2D:
			return {"ok": true, "points": [(node as Node2D).global_position]}
		elif node is Node3D:
			return {"ok": true, "points": [(node as Node3D).global_position]}
		return {"ok": false, "message": "focus_node_path must point to a Node2D or Node3D", "error_code": "unsupported_focus_node"}

	var target_result := MapTools._resolve_map_target(input, editor_interface)
	if not bool(target_result.get("ok", false)):
		return target_result
	var map_node: Node = target_result["node"]
	var dimension := 3 if map_node.get_class() == "GridMap" else 2
	var region: Dictionary = region_value
	var x := int(region.get("x", 0))
	var y := int(region.get("y", 0))
	var width := maxi(1, int(region.get("width", 1)))
	var height := maxi(1, int(region.get("height", 1)))
	var points: Array = []
	if dimension == 3:
		var z := int(region.get("z", 0))
		var depth := maxi(1, int(region.get("depth", 1)))
		var corners3: Array[Vector3i] = [
			Vector3i(x, y, z),
			Vector3i(x + width - 1, y, z),
			Vector3i(x, y + height - 1, z),
			Vector3i(x, y, z + depth - 1),
			Vector3i(x + width - 1, y + height - 1, z + depth - 1),
		]
		for corner in corners3:
			var local3: Vector3 = map_node.call("map_to_local", corner)
			points.append((map_node as Node3D).to_global(local3))
	else:
		var corners2: Array[Vector2i] = [
			Vector2i(x, y),
			Vector2i(x + width - 1, y),
			Vector2i(x, y + height - 1),
			Vector2i(x + width - 1, y + height - 1),
		]
		for corner in corners2:
			var local2: Vector2 = map_node.call("map_to_local", corner)
			points.append((map_node as Node2D).to_global(local2))
	return {"ok": true, "points": points}


## 2D 直接改写 viewport 的 global_canvas_transform（编辑器画布缩放/平移用的就是这个属性），
## 3D 沿相机当前朝向后退到能把目标包进视野的距离，再 look_at 目标中心——
## 不依赖任何"模拟按键触发 Frame Selected"这种内部实现细节，全部走公开 API。
static func _apply_camera_focus(viewport: Viewport, mode: String, points: Array, margin: float) -> Dictionary:
	if points.is_empty():
		return {"ok": true, "applied": false}
	if mode == "3d":
		var camera := viewport.get_camera_3d()
		if camera == null:
			return {"ok": false, "message": "No active Camera3D in the requested viewport", "error_code": "no_active_camera"}
		var min_p: Vector3 = points[0]
		var max_p: Vector3 = points[0]
		for p in points:
			min_p = min_p.min(p)
			max_p = max_p.max(p)
		var center := (min_p + max_p) * 0.5
		var radius := maxf((max_p - min_p).length() * 0.5, 0.5)
		var fov_deg := camera.fov if camera.projection == Camera3D.PROJECTION_PERSPECTIVE else 50.0
		var fov_rad := deg_to_rad(fov_deg)
		var distance := (radius * margin) / maxf(0.001, tan(fov_rad * 0.5))
		distance = maxf(distance, camera.near * 4.0)
		var back := camera.global_transform.basis.z
		if back.length() < 0.001:
			back = Vector3(0, 0, 1)
		back = back.normalized()
		camera.global_position = center + back * distance
		camera.look_at(center, Vector3.UP)
		return {"ok": true, "applied": true, "center": {"x": center.x, "y": center.y, "z": center.z}, "distance": distance}
	else:
		var min_p2: Vector2 = points[0]
		var max_p2: Vector2 = points[0]
		for p in points:
			min_p2 = min_p2.min(p)
			max_p2 = max_p2.max(p)
		var center2 := (min_p2 + max_p2) * 0.5
		var size2 := max_p2 - min_p2
		size2 = Vector2(maxf(size2.x, 16.0), maxf(size2.y, 16.0))
		var viewport_size := Vector2(viewport.size)
		if viewport_size.x <= 0.0 or viewport_size.y <= 0.0:
			return {"ok": false, "message": "Viewport has no size", "error_code": "viewport_no_size"}
		var zoom := minf(viewport_size.x / (size2.x * margin), viewport_size.y / (size2.y * margin))
		zoom = clampf(zoom, 0.02, 16.0)
		var transform := Transform2D(0.0, Vector2.ZERO).scaled(Vector2(zoom, zoom))
		transform.origin = viewport_size * 0.5 - center2 * zoom
		viewport.global_canvas_transform = transform
		return {"ok": true, "applied": true, "center": {"x": center2.x, "y": center2.y}, "zoom": zoom}


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
	if path == "" or not (path.get_extension().to_lower() in ["tscn", "scn"]):
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
