@tool
extends RefCounted

const MAX_ITEMS := 60
const MAX_LOG_CHARS := 20000
const MAX_LOG_LINES := 200


static func collect(editor_interface: EditorInterface) -> Array:
	var items: Array = []
	items.append_array(_collect_open_script_state(editor_interface))
	items.append_array(_collect_log_dir(ProjectSettings.globalize_path("user://logs")))
	items.append_array(_collect_log_dir(ProjectSettings.globalize_path("user://")))
	items.append_array(_collect_log_dir(ProjectSettings.globalize_path("res://.godot/editor")))
	return _dedupe(items).slice(0, MAX_ITEMS)


static func _collect_open_script_state(editor_interface: EditorInterface) -> Array:
	var result: Array = []
	if editor_interface == null:
		return result
	var script_editor = editor_interface.get_script_editor()
	if script_editor == null or not script_editor.has_method("get_open_scripts"):
		return result
	for script in script_editor.call("get_open_scripts"):
		if script is Script:
			var path := str(script.resource_path)
			if path == "":
				result.append({
					"source": "script_editor",
					"severity": "warning",
					"path": "",
					"line": 0,
					"message": "Open script has no resource_path; save it before asking for file edits."
				})
	return result


static func _collect_log_dir(path: String) -> Array:
	var result: Array = []
	var dir := DirAccess.open(path)
	if dir == null:
		return result
	dir.list_dir_begin()
	while true:
		var name := dir.get_next()
		if name == "":
			break
		if dir.current_is_dir():
			continue
		var lower := name.to_lower()
		if lower.ends_with(".log") or lower.ends_with(".txt"):
			result.append_array(_collect_log_file(path.path_join(name)))
	dir.list_dir_end()
	return result


static func _collect_log_file(path: String) -> Array:
	var result: Array = []
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return result
	var text := file.get_as_text()
	if text.length() > MAX_LOG_CHARS:
		text = text.substr(text.length() - MAX_LOG_CHARS)
	var lines := text.split("\n")
	var start = max(0, lines.size() - MAX_LOG_LINES)
	for index in range(start, lines.size()):
		var line := str(lines[index]).strip_edges()
		var lower := line.to_lower()
		if lower.find("script error") >= 0 or lower.find("error:") >= 0 or lower.find("warning:") >= 0 or lower.find("failed") >= 0:
			result.append({
				"source": "godot_log",
				"severity": _severity_from_line(lower),
				"path": path,
				"line": index + 1,
				"message": line
			})
	return result


static func _severity_from_line(lower: String) -> String:
	if lower.find("error") >= 0 or lower.find("failed") >= 0:
		return "error"
	if lower.find("warning") >= 0:
		return "warning"
	return "info"


static func _dedupe(items: Array) -> Array:
	var seen := {}
	var result: Array = []
	for item in items:
		if not (item is Dictionary):
			continue
		var key := "%s:%s:%s" % [item.get("source", ""), item.get("path", ""), item.get("message", "")]
		if seen.has(key):
			continue
		seen[key] = true
		result.append(item)
	return result
