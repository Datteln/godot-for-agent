@tool
extends Node

const DiagnosticsCollector = preload("res://addons/ai_agent/context/diagnostics_collector.gd")
const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")

const MAX_PROJECT_FILES := 200
const PROJECT_FILE_MAX_DEPTH := 4
const PROJECT_FILE_EXTENSIONS := ["gd", "cs", "tscn", "tres"]

var editor_interface: EditorInterface


func collect(domain_hint: String = "any") -> Dictionary:
	var enable_diagnostics := true
	if editor_interface != null:
		enable_diagnostics = bool(ConfigMigrations.get_value(editor_interface, "ai_agent/enable_lsp_diagnostics"))
	var diagnostics: Array = []
	if enable_diagnostics:
		diagnostics = DiagnosticsCollector.collect(editor_interface)
	return {
		"selection": _collect_selection(),
		"scene_tree": _collect_scene_tree(),
		"tile_catalog": _collect_tile_catalog(),
		"project_files": _collect_project_files(),
		"debugger_errors": _collect_debugger_errors(diagnostics),
		"diagnostics": diagnostics,
		"dotnet_enabled": _dotnet_enabled(),
		"domain_hint": domain_hint
	}


func _collect_project_files() -> Array:
	var files: Array = []
	_scan_project_dir("res://", files, 0)
	return files


func _scan_project_dir(path: String, out: Array, depth: int) -> void:
	if depth > PROJECT_FILE_MAX_DEPTH or out.size() >= MAX_PROJECT_FILES:
		return
	var dir := DirAccess.open(path)
	if dir == null:
		return
	dir.list_dir_begin()
	while true:
		var name := dir.get_next()
		if name == "":
			break
		if name.begins_with(".") or name == "addons":
			continue
		var full := path.path_join(name)
		if dir.current_is_dir():
			_scan_project_dir(full, out, depth + 1)
		elif _is_project_file(name):
			out.append(full)
			if out.size() >= MAX_PROJECT_FILES:
				break
	dir.list_dir_end()


func _is_project_file(name: String) -> bool:
	var lower := name.to_lower()
	for ext in PROJECT_FILE_EXTENSIONS:
		if lower.ends_with("." + ext):
			return true
	return false


func _collect_debugger_errors(diagnostics: Array) -> Array:
	var result: Array = []
	for item in diagnostics:
		if item is Dictionary and str(item.get("severity", "")) == "error":
			result.append({
				"type": "error",
				"message": str(item.get("message", "")),
				"file": str(item.get("path", "")),
				"line": int(item.get("line", 0))
			})
	return result


func _collect_selection() -> Dictionary:
	if editor_interface == null:
		return {}
	var selection := editor_interface.get_selection()
	var nodes := selection.get_selected_nodes()
	var result: Array = []
	for node in nodes:
		if node is Node:
			result.append({
				"name": node.name,
				"path": str(node.get_path()),
				"type": node.get_class(),
				"script": _script_path(node)
			})
	return {"nodes": result}


func _collect_scene_tree() -> Dictionary:
	if editor_interface == null:
		return {}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {}
	return _node_to_dict(root, 0, 4)


func _node_to_dict(node: Node, depth: int, max_depth: int) -> Dictionary:
	var children: Array = []
	if depth < max_depth:
		for child in node.get_children():
			if child is Node:
				children.append(_node_to_dict(child, depth + 1, max_depth))
	return {
		"name": node.name,
		"path": str(node.get_path()),
		"type": node.get_class(),
		"script": _script_path(node),
		"children": children
	}


func _collect_tile_catalog() -> Array:
	if editor_interface == null:
		return []
	for node in editor_interface.get_selection().get_selected_nodes():
		if node != null and node.get_class() == "TileMapLayer":
			return _tile_catalog_from_layer(node)
	return []


func _tile_catalog_from_layer(layer: Node) -> Array:
	if not layer.has_method("get_tile_set"):
		return []
	var tile_set = layer.call("get_tile_set")
	if tile_set == null:
		return []
	var result: Array = []
	if tile_set.has_method("get_source_count"):
		for source_index in range(tile_set.call("get_source_count")):
			var source_id = tile_set.call("get_source_id", source_index)
			result.append({"source_id": source_id})
	return result


func _script_path(node: Node) -> String:
	var script = node.get_script()
	if script != null and script is Resource:
		return script.resource_path
	return ""


func _dotnet_enabled() -> bool:
	var root := ProjectSettings.globalize_path("res://")
	var dir := DirAccess.open(root)
	if dir == null:
		return false
	dir.list_dir_begin()
	while true:
		var name := dir.get_next()
		if name == "":
			break
		if not dir.current_is_dir() and name.ends_with(".csproj"):
			dir.list_dir_end()
			return true
	dir.list_dir_end()
	return false
