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
	"ai_agent/compact_summary_use_llm": "default",
	"ai_agent/compact_summary_model": "",
	"ai_agent/request_timeout_sec": 30.0,
	"ai_agent/chat_request_timeout_sec": 300.0,
	"ai_agent/chat_request_hard_cap_sec": 1800.0,
	"ai_agent/embedding_provider": "disabled",
	"ai_agent/embedding_model": "text-embedding-3-small",
	"ai_agent/embedding_endpoint": "https://api.openai.com/v1",
	"ai_agent/embedding_api_key": "",
	"ai_agent/embedding_timeout_s": 3.0,
	"ai_agent/embedding_retries": 1,
	"ai_agent/rerank_model": "",
	"ai_agent/rerank_timeout_s": 2.0,
	"ai_agent/rag_query_router_enabled": true,
	"ai_agent/rag_auto_build_enabled": true,
	"ai_agent/rag_auto_watch_interval_s": 1.0,
	"ai_agent/rag_auto_watch_debounce_s": 0.75,
	"ai_agent/rag_token_budget": 1500,
	"ai_agent/graph_max_depth": 2,
	"ai_agent/graph_max_neighbors": 5,
	"ai_agent/asset_understanding_enabled": false,
	"ai_agent/asset_understanding_model": "",
	"ai_agent/asset_understanding_endpoint": "",
	"ai_agent/asset_understanding_api_key": "",
	"ai_agent/asset_understanding_timeout_s": 10.0,
	"ai_agent/asset_understanding_max_tokens": 500,
	"ai_agent/asset_understanding_concurrency": 3,
	"ai_agent/log_level": "info",
	"ai_agent/log_to_file": true,
	"ai_agent/log_file_path": "res://logs/ai_agent_frontend.log",
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
	"ai_agent/system_command_timeout_ms": 120000,
	"ai_agent/gd_script_timeout_ms": 60000,
	"ai_agent/export_timeout_ms": 600000,
	"ai_agent/session_history_json": ""
}

## 旧版设置曾使用 `full_access`；服务端权限契约改为 `auto_approve` 后，
## EditorSettings 会保留该旧值，不能仅依赖缺失值的默认填充。
const LEGACY_PERMISSION_MODE_ALIASES := {
	"full_access": "auto_approve",
}
const PERMISSION_MODES := {
	"default": true,
	"plan": true,
	"auto_approve": true,
	"read_only": true,
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
	"ai_agent/compact_summary_use_llm": {
		"hint": PROPERTY_HINT_ENUM,
		"hint_string": "default,on,off"
	},
	"ai_agent/llm_api_key": {
		"hint": PROPERTY_HINT_PASSWORD,
		"hint_string": ""
	},
	"ai_agent/llm_request_timeout_s": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1,600,1,suffix:s"
	},
	"ai_agent/request_timeout_sec": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1,120,1,suffix:s"
	},
	"ai_agent/chat_request_timeout_sec": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1,1800,1,suffix:s"
	},
	"ai_agent/chat_request_hard_cap_sec": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "60,7200,10,suffix:s"
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
	"ai_agent/asset_understanding_concurrency": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1,16,1"
	},
	"ai_agent/log_level": {
		"hint": PROPERTY_HINT_ENUM,
		"hint_string": "debug,info,warn,error,off"
	},
	"ai_agent/log_file_path": {
		"hint": PROPERTY_HINT_GLOBAL_FILE,
		"hint_string": "*.log,*.txt"
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
	},
	"ai_agent/system_command_timeout_ms": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1000,600000,1000,suffix:ms"
	},
	"ai_agent/gd_script_timeout_ms": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1000,600000,1000,suffix:ms"
	},
	"ai_agent/export_timeout_ms": {
		"hint": PROPERTY_HINT_RANGE,
		"hint_string": "1000,1800000,1000,suffix:ms"
	}
}


static func apply_defaults(editor_interface: EditorInterface) -> void:
	var settings := editor_interface.get_editor_settings()
	for key in DEFAULTS.keys():
		if not settings.has_setting(key):
			settings.set_setting(key, DEFAULTS[key])
		elif key == "ai_agent/permission_mode":
			var normalized_mode := normalize_permission_mode(settings.get_setting(key))
			if settings.get_setting(key) != normalized_mode:
				settings.set_setting(key, normalized_mode)
		_add_property_info(settings, key, DEFAULTS[key])


## 该方法也由 HTTP 请求边界使用，保证历史配置或外部脚本写入非法值时，
## 不会把无法通过后端 schema 校验的枚举发到 `/chat`。
static func normalize_permission_mode(value: Variant) -> String:
	var mode := str(value).strip_edges().to_lower()
	if LEGACY_PERMISSION_MODE_ALIASES.has(mode):
		return str(LEGACY_PERMISSION_MODE_ALIASES[mode])
	if PERMISSION_MODES.has(mode):
		return mode
	return str(DEFAULTS["ai_agent/permission_mode"])


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
