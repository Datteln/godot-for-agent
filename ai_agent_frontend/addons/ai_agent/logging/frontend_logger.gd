@tool
extends RefCounted

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")

const LEVELS := {
	"debug": 10,
	"info": 20,
	"warn": 30,
	"error": 40,
	"off": 999
}

const REDACTED_KEYS := {
	"api_key": true,
	"authorization": true,
	"llm_api_key": true,
	"token": true
}
const MAX_DATA_CHARS := 4000
const MAX_LOG_FILE_BYTES := 10 * 1024 * 1024


static func debug(editor_interface: EditorInterface, component: String, message: String, data: Dictionary = {}) -> void:
	write(editor_interface, "debug", component, message, data)


static func info(editor_interface: EditorInterface, component: String, message: String, data: Dictionary = {}) -> void:
	write(editor_interface, "info", component, message, data)


static func warn(editor_interface: EditorInterface, component: String, message: String, data: Dictionary = {}) -> void:
	write(editor_interface, "warn", component, message, data)


static func error(editor_interface: EditorInterface, component: String, message: String, data: Dictionary = {}) -> void:
	write(editor_interface, "error", component, message, data)


static func write(
	editor_interface: EditorInterface,
	level: String,
	component: String,
	message: String,
	data: Dictionary = {}
) -> void:
	if not _should_log(editor_interface, level):
		return
	var line := _format_line(level, component, message, _redact_dictionary(data))
	match level:
		"warn":
			push_warning(line)
		"error":
			push_error(line)
		_:
			print(line)
	if _log_to_file(editor_interface):
		_append_to_file(editor_interface, line)


static func _should_log(editor_interface: EditorInterface, level: String) -> bool:
	var configured := "info"
	if editor_interface != null:
		configured = str(ConfigMigrations.get_value(editor_interface, "ai_agent/log_level"))
	return int(LEVELS.get(level, 20)) >= int(LEVELS.get(configured, 20))


static func _log_to_file(editor_interface: EditorInterface) -> bool:
	return editor_interface != null and bool(ConfigMigrations.get_value(editor_interface, "ai_agent/log_to_file"))


static func _format_line(level: String, component: String, message: String, data: Dictionary) -> String:
	var timestamp := Time.get_datetime_string_from_system(false, true)
	var suffix := ""
	if not data.is_empty():
		var data_text := JSON.stringify(data)
		if data_text.length() > MAX_DATA_CHARS:
			data_text = data_text.left(MAX_DATA_CHARS) + "...(truncated)"
		suffix = " " + data_text
	return "[%s] [%s] [AI Agent:%s] %s%s" % [timestamp, level.to_upper(), component, message, suffix]


static func _append_to_file(editor_interface: EditorInterface, line: String) -> void:
	var path := str(ConfigMigrations.get_value(editor_interface, "ai_agent/log_file_path")).strip_edges()
	if path.is_empty():
		return
	path = _absolute_log_path(path)
	var directory_path := path.get_base_dir()
	var directory_error := DirAccess.make_dir_recursive_absolute(directory_path)
	if directory_error != OK and directory_error != ERR_ALREADY_EXISTS:
		push_warning("[AI Agent:Logger] Failed to create log directory: " + directory_path)
		return
	path = _available_log_path(path, line.to_utf8_buffer().size() + 1)
	var file := FileAccess.open(path, FileAccess.READ_WRITE)
	if file == null:
		file = FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		push_warning("[AI Agent:Logger] Failed to open log file: " + path)
		return
	file.seek_end()
	file.store_line(line)
	file.close()


static func _absolute_log_path(path: String) -> String:
	if path.is_absolute_path():
		return path
	if path.begins_with("res://") or path.begins_with("user://"):
		return ProjectSettings.globalize_path(path)
	return ProjectSettings.globalize_path("res://" + path.trim_prefix("/"))


static func _available_log_path(base_path: String, incoming_bytes: int) -> String:
	var index := 0
	while true:
		var candidate := base_path if index == 0 else _rotated_log_path(base_path, index)
		if not FileAccess.file_exists(candidate) or _file_size(candidate) + incoming_bytes <= MAX_LOG_FILE_BYTES:
			return candidate
		index += 1
	return base_path


static func _rotated_log_path(base_path: String, index: int) -> String:
	var extension := base_path.get_extension()
	if extension.is_empty():
		return "%s.%d" % [base_path, index]
	return "%s.%d.%s" % [base_path.trim_suffix("." + extension), index, extension]


static func _file_size(path: String) -> int:
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		return 0
	var size := file.get_length()
	file.close()
	return size


static func _redact_dictionary(data: Dictionary) -> Dictionary:
	var result := {}
	for key in data.keys():
		var key_text := str(key)
		var lower_key := key_text.to_lower()
		if REDACTED_KEYS.has(lower_key) or lower_key.contains("api_key") or lower_key.contains("token"):
			result[key_text] = "<redacted>"
			continue
		var value = data[key]
		if value is Dictionary:
			result[key_text] = _redact_dictionary(value)
		elif value is Array:
			result[key_text] = _redact_array(value)
		else:
			result[key_text] = value
	return result


static func _redact_array(items: Array) -> Array:
	var result := []
	for item in items:
		if item is Dictionary:
			result.append(_redact_dictionary(item))
		elif item is Array:
			result.append(_redact_array(item))
		else:
			result.append(item)
	return result
