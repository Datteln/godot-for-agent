@tool
extends RefCounted

## 即使以 "_" 开头也保留的常用生命周期/虚函数方法名。
const METHOD_WHITELIST := [
	"_ready", "_process", "_physics_process", "_input", "_unhandled_input",
	"_enter_tree", "_exit_tree", "_draw", "_init"
]


static func get_class_info(target_class: String) -> Dictionary:
	if target_class.strip_edges().is_empty():
		return {"source": "unknown", "class_name": target_class}

	if ClassDB.class_exists(target_class):
		return _classdb_info(target_class)

	var script_info := _script_class_info(target_class)
	if not script_info.is_empty():
		return script_info

	return {"source": "unknown", "class_name": target_class}


static func get_multi(class_names: Array) -> Array:
	var result: Array = []
	for item in class_names:
		result.append(get_class_info(str(item)))
	return result


static func _classdb_info(target_class: String) -> Dictionary:
	var methods: Array = []
	for method in ClassDB.class_get_method_list(target_class, false):
		var method_name := str(method.get("name", ""))
		if method_name.begins_with("_") and not METHOD_WHITELIST.has(method_name):
			continue
		methods.append(_convert_method(method))

	var properties: Array = []
	for property in ClassDB.class_get_property_list(target_class, false):
		properties.append(_convert_property(property))

	var signals: Array = []
	for item in ClassDB.class_get_signal_list(target_class, false):
		signals.append(_convert_method(item))

	var constants: Dictionary = {}
	for constant_name in ClassDB.class_get_integer_constant_list(target_class, false):
		constants[constant_name] = ClassDB.class_get_integer_constant(target_class, constant_name)

	return {
		"source": "ClassDB",
		"class_name": target_class,
		"parent": ClassDB.get_parent_class(target_class),
		"methods": methods,
		"properties": properties,
		"signals": signals,
		"constants": constants
	}


## 将 property-info 字典中的 Variant.Type 整型 "type" 转换为可读类型名。
static func _convert_property(property: Dictionary) -> Dictionary:
	var result := property.duplicate()
	result["type"] = type_string(int(property.get("type", TYPE_NIL)))
	return result


## 转换方法/信号字典中的返回值与参数列表的类型字段。
static func _convert_method(method: Dictionary) -> Dictionary:
	var result := method.duplicate()
	if result.has("return") and result["return"] is Dictionary:
		result["return"] = _convert_property(result["return"])
	if result.has("args"):
		var args: Array = []
		for arg in result["args"]:
			if arg is Dictionary:
				args.append(_convert_property(arg))
			else:
				args.append(arg)
		result["args"] = args
	return result


static func _script_class_info(target_class: String) -> Dictionary:
	for item in ProjectSettings.get_global_class_list():
		if str(item.get("class", "")) != target_class:
			continue
		var path := str(item.get("path", ""))
		var script := load(path)
		if script == null:
			return {
				"source": "script_class",
				"class_name": target_class,
				"path": path,
				"load_error": true
			}
		return {
			"source": "script_class",
			"class_name": target_class,
			"path": path,
			"base": item.get("base", ""),
			"methods": script.get_script_method_list(),
			"properties": script.get_script_property_list(),
			"signals": script.get_script_signal_list()
		}
	return {}
