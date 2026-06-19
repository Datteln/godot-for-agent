@tool
extends RefCounted

const DEFAULTS := {
	"ai_agent/service_url": "http://127.0.0.1:8765",
	"ai_agent/auto_start_service": false,
	"ai_agent/python_executable": "",
	"ai_agent/service_module_dir": "",
	"ai_agent/session_id": "default",
	"ai_agent/ui_language": "zh",
	"ai_agent/permission_mode": "default",
	"ai_agent/effort": "standard",
	"ai_agent/output_style": "default",
	"ai_agent/llm_base_url": "https://api.openai.com/v1",
	"ai_agent/llm_api_key": "",
	"ai_agent/llm_model": "gpt-4o-mini",
	"ai_agent/llm_quick_model": "",
	"ai_agent/llm_standard_model": "",
	"ai_agent/llm_deep_model": "",
	"ai_agent/llm_verify_model": "",
	"ai_agent/llm_advisor_model": "",
	"ai_agent/llm_fallback_model": "",
	"ai_agent/llm_request_timeout_s": 60.0,
	"ai_agent/embedding_provider": "disabled",
	"ai_agent/embedding_model": "text-embedding-3-small",
	"ai_agent/embedding_endpoint": "https://api.openai.com/v1",
	"ai_agent/embedding_api_key": "",
	"ai_agent/embedding_timeout_s": 3.0,
	"ai_agent/embedding_retries": 1,
	"ai_agent/rerank_model": "",
	"ai_agent/rerank_timeout_s": 2.0,
	"ai_agent/rag_query_router_enabled": true,
	"ai_agent/rag_token_budget": 1500,
	"ai_agent/graph_max_depth": 2,
	"ai_agent/graph_max_neighbors": 5,
	"ai_agent/asset_understanding_enabled": false,
	"ai_agent/asset_understanding_model": "",
	"ai_agent/asset_understanding_endpoint": "",
	"ai_agent/asset_understanding_api_key": "",
	"ai_agent/asset_understanding_timeout_s": 10.0,
	"ai_agent/asset_understanding_max_tokens": 500,
	"ai_agent/log_level": "info",
	"ai_agent/log_to_file": true,
	"ai_agent/log_file_path": "res://logs/ai_agent_frontend.log",
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
	"ai_agent/runner_timeout_ms": 120000,
	"ai_agent/session_history_json": ""
}

const PROPERTY_HINTS := {
	"ai_agent/permission_mode": {
		"hint": PROPERTY_HINT_ENUM,
		"hint_string": "default,plan,auto_approve,read_only"
	},
	"ai_agent/ui_language": {
		"hint": PROPERTY_HINT_ENUM,
		"hint_string": "zh,en"
	},
	"ai_agent/effort": {
		"hint": PROPERTY_HINT_ENUM,
		"hint_string": "quick,standard,deep,verify,advisor"
	},
	"ai_agent/llm_api_key": {
		"hint": PROPERTY_HINT_PASSWORD,
		"hint_string": ""
	},
	"ai_agent/llm_request_timeout_s": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1,600,1,suffix:s"
	},
	"ai_agent/embedding_provider": {
		"hint": PROPERTY_HINT_ENUM,
		"hint_string": "disabled,openai,local,bge-m3"
	},
	"ai_agent/embedding_api_key": {
		"hint": PROPERTY_HINT_PASSWORD,
		"hint_string": ""
	},
	"ai_agent/embedding_timeout_s": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "0.1,3,0.1,suffix:s"
	},
	"ai_agent/embedding_retries": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "0,2,1"
	},
	"ai_agent/rerank_timeout_s": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "0.1,2,0.1,suffix:s"
	},
	"ai_agent/rag_token_budget": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "128,16384,128,suffix:tokens"
	},
	"ai_agent/graph_max_depth": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "0,8,1"
	},
	"ai_agent/graph_max_neighbors": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1,100,1"
	},
	"ai_agent/asset_understanding_api_key": {
		"hint": PROPERTY_HINT_PASSWORD,
		"hint_string": ""
	},
	"ai_agent/asset_understanding_timeout_s": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "0.1,120,0.1,suffix:s"
	},
	"ai_agent/asset_understanding_max_tokens": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1,4096,1,suffix:tokens"
	},
	"ai_agent/log_level": {
		"hint": PROPERTY_HINT_ENUM,
		"hint_string": "debug,info,warn,error,off"
	},
	"ai_agent/log_file_path": {
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": "*.log,*.txt"
	},
	"ai_agent/event_poll_interval_sec": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "0.2,10,0.1,suffix:s"
	},
	"ai_agent/python_executable": {
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": ""
	},
	"ai_agent/service_module_dir": {
		"hint": PROPERTY_HINT_GLOBAL_DIR,
		"hint_string": ""
	},
	"ai_agent/test_executable": {
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": ""
	},
	"ai_agent/test_output_log": {
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": ""
	},
	"ai_agent/headless_executable": {
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": ""
	},
	"ai_agent/headless_output_log": {
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": ""
	},
	"ai_agent/runner_timeout_ms": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1000,600000,1000,suffix:ms"
	}
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
	var property_hint: Dictionary = PROPERTY_HINTS.get(key, {})
	settings.add_property_info({
		"name": key,
		"type": value_type,
		"hint": int(property_hint.get("hint", PROPERTY_HINT_NONE)),
		"hint_string": str(property_hint.get("hint_string", ""))
	})
