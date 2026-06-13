@tool
extends Node

var _states: Dictionary = {}


func snapshot(path: String, known_full_read: bool = false) -> Dictionary:
	var state := _make_state(path, known_full_read)
	_states[path] = state
	return state


func get_state(path: String) -> Dictionary:
	return _states.get(path, {})


func is_stale(path: String) -> bool:
	if not _states.has(path):
		return false
	var old_state: Dictionary = _states[path]
	var current := _make_state(path, bool(old_state.get("known_full_read", false)))
	return old_state.get("hash", "") != current.get("hash", "") or old_state.get("mtime_ns", 0) != current.get("mtime_ns", 0)


func _make_state(path: String, known_full_read: bool) -> Dictionary:
	var absolute := ProjectSettings.globalize_path(path)
	var exists := FileAccess.file_exists(absolute)
	var content := ""
	if exists:
		content = FileAccess.get_file_as_string(absolute)
	return {
		"path": path,
		"exists": exists,
		"hash": content.sha256_text(),
		"mtime_ns": FileAccess.get_modified_time(absolute) * 1000000000,
		"known_full_read": known_full_read
	}
