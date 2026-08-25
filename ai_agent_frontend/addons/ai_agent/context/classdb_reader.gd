@tool
extends RefCounted

## 即使以 "_" 开头也保留的常用生命周期/虚函数方法名。
const METHOD_WHITELIST := [
	"_ready", "_process", "_physics_process", "_input", "_unhandled_input",
	"_enter_tree", "_exit_tree", "_draw", "_init"
]
const MAX_REQUESTED_ITEMS := 12
const DEFAULT_SEARCH_LIMIT := 8
const MAX_RESULT_BYTES := 12 * 1024


static func get_class_info(target_class: String) -> Dictionary:
	return query_class_info({"class_name": target_class})


## 仅返回显式、有界的 ClassDB 查询结果，绝不默认枚举完整类文档。
static func query_class_info(input: Dictionary) -> Dictionary:
	var target_class := str(input.get("class_name", input.get("name", ""))).strip_edges()
	var mode := str(input.get("mode", "overview")).strip_edges().to_lower()
	if target_class.is_empty():
		return {"ok": false, "error_code": "class_docs_invalid_request", "message": "class_name is required"}
	if not ["overview", "search", "members", "constants"].has(mode):
		return _too_large(target_class, "Use overview, search, members, or constants.")
	var requested: Array = input.get("members", []) if mode == "members" else input.get("constants", []) if mode == "constants" else []
	if requested.size() > MAX_REQUESTED_ITEMS:
		return _too_large(target_class, "Request at most %d exact names per query." % MAX_REQUESTED_ITEMS)
	if mode == "search" and str(input.get("query", "")).strip_edges().is_empty():
		return _too_large(target_class, "Provide query words, then request exact member names.")
	var result := _query_classdb(target_class, mode, input)
	if result.is_empty():
		result = _query_script_class(target_class, mode, input)
	if result.is_empty():
		return {"ok": false, "error_code": "class_docs_not_found", "class_name": target_class}
	if JSON.stringify(result).to_utf8_buffer().size() > MAX_RESULT_BYTES:
		return _too_large(target_class, "Narrow the query to fewer exact names.")
	return result


static func get_multi(class_names: Array) -> Array:
	var result: Array = []
	for item in class_names:
		result.append(get_class_info(str(item)))
	return result


static func _query_classdb(target_class: String, mode: String, input: Dictionary) -> Dictionary:
	if not ClassDB.class_exists(target_class):
		return {}
	var result := {"ok": true, "source": "ClassDB", "class_name": target_class, "mode": mode, "parent": ClassDB.get_parent_class(target_class)}
	if mode == "overview":
		result["capabilities"] = ["search", "members", "constants"]
		return result
	if mode == "constants":
		var constants: Dictionary = {}
		for constant_name in input.get("constants", []):
			var name := str(constant_name)
			if ClassDB.class_has_integer_constant(target_class, name):
				constants[name] = ClassDB.class_get_integer_constant(target_class, name)
		result["constants"] = constants
		return result
	var methods := _classdb_methods(target_class)
	if mode == "members":
		var exact: Array = []
		for requested in input.get("members", []):
			for method in methods:
				if str(method.get("name", "")) == str(requested):
					exact.append(method)
					break
		result["members"] = exact
		return result
	var query := str(input.get("query", "")).to_lower()
	var limit := clampi(int(input.get("limit", DEFAULT_SEARCH_LIMIT)), 1, MAX_REQUESTED_ITEMS)
	var matches: Array = []
	for method in methods:
		if str(method.get("name", "")).to_lower().contains(query):
			matches.append(method)
			if matches.size() >= limit:
				break
	result["members"] = matches
	return result


static func _classdb_methods(target_class: String) -> Array:
	var methods: Array = []
	for method in ClassDB.class_get_method_list(target_class, false):
		var method_name := str(method.get("name", ""))
		if method_name.begins_with("_") and not METHOD_WHITELIST.has(method_name):
			continue
		methods.append(_convert_method(method))
	return methods


static func _query_script_class(target_class: String, mode: String, input: Dictionary) -> Dictionary:
	var info := _script_class_info(target_class)
	if info.is_empty():
		return {}
	var result := {"ok": true, "source": "script_class", "class_name": target_class, "mode": mode, "parent": str(info.get("base", ""))}
	if mode == "overview":
		result["capabilities"] = ["search", "members"]
		return result
	var methods: Array = info.get("methods", [])
	var selected: Array = []
	if mode == "search":
		var query := str(input.get("query", "")).to_lower()
		for method in methods:
			if str(method.get("name", "")).to_lower().contains(query) and selected.size() < clampi(int(input.get("limit", DEFAULT_SEARCH_LIMIT)), 1, MAX_REQUESTED_ITEMS):
				selected.append(_convert_method(method))
	elif mode == "members":
		for wanted in input.get("members", []):
			for method in methods:
				if str(method.get("name", "")) == str(wanted):
					selected.append(_convert_method(method))
					break
	result["members"] = selected
	return result


static func _too_large(target_class: String, hint: String) -> Dictionary:
	return {"ok": false, "error_code": "class_docs_query_too_large", "class_name": target_class, "narrowing_hint": hint}


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
