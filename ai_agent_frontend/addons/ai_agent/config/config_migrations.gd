@tool
extends RefCounted

const DEFAULTS := {
	"ai_agent/service_url": "http://127.0.0.1:8765",
	"ai_agent/auto_start_service": false,
	"ai_agent/python_executable": "",
	"ai_agent/service_module_dir": "",
	"ai_agent/session_id": "default",
	"ai_agent/permission_mode": "default",
	"ai_agent/effort": "standard",
	"ai_agent/output_style": "default",
	"ai_agent/enable_event_stream": true,
	"ai_agent/event_poll_interval_sec": 1.0,
	"ai_agent/enable_lsp_diagnostics": true,
	"ai_agent/show_recovery_prompt": true,
	"ai_agent/trusted_project_extensions": false,
	"ai_agent/test_executable": "",
	"ai_agent/test_args": "",
	"ai_agent/test_output_log": "",
	"ai_agent/headless_executable": "",
	"ai_agent/headless_args": "",
	"ai_agent/headless_output_log": "",
	"ai_agent/runner_timeout_ms": 120000
}


static func apply_defaults(editor_interface: EditorInterface) -> void:
	var settings := editor_interface.get_editor_settings()
	for key in DEFAULTS.keys():
		if not settings.has_setting(key):
			settings.set_setting(key, DEFAULTS[key])
		_add_property_info(settings, key, DEFAULTS[key])


static func get_value(editor_interface: EditorInterface, key: String) -> Variant:
	var settings := editor_interface.get_editor_settings()
	if not settings.has_setting(key) and DEFAULTS.has(key):
		settings.set_setting(key, DEFAULTS[key])
	return settings.get_setting(key)


static func set_value(editor_interface: EditorInterface, key: String, value: Variant) -> void:
	var settings := editor_interface.get_editor_settings()
	settings.set_setting(key, value)
	_add_property_info(settings, key, value)


static func _add_property_info(settings: EditorSettings, key: String, value: Variant) -> void:
	var value_type := typeof(value)
	if value_type == TYPE_NIL:
		value_type = TYPE_STRING
	settings.add_property_info({
		"name": key,
		"type": value_type,
		"hint": PROPERTY_HINT_NONE,
		"hint_string": ""
	})
