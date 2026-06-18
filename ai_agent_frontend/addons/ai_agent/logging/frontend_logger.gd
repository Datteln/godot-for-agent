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
	var file := FileAccess.open(path, FileAccess.READ_WRITE)
	if file == null:
		file = FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		push_warning("[AI Agent:Logger] Failed to open log file: " + path)
		return
	file.seek_end()
	file.store_line(line)


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
