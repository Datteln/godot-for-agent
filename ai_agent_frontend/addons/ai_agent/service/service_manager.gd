@tool
extends Node

signal service_started(base_url: String)
signal service_stopped
signal service_failed(message: String)

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

const PORT_MIN := 49152
const PORT_MAX := 65535

const COMPACT_ENV_SETTINGS := {
	"AI_AGENT_COMPACT_SUMMARY_MODEL": "ai_agent/compact_summary_model"
}

const RAG_ENV_SETTINGS := {
	"AI_AGENT_EMBEDDING_PROVIDER": "ai_agent/embedding_provider",
	"AI_AGENT_EMBEDDING_MODEL": "ai_agent/embedding_model",
	"AI_AGENT_EMBEDDING_ENDPOINT": "ai_agent/embedding_endpoint",
	"AI_AGENT_EMBEDDING_API_KEY": "ai_agent/embedding_api_key",
	"AI_AGENT_EMBEDDING_TIMEOUT_S": "ai_agent/embedding_timeout_s",
	"AI_AGENT_EMBEDDING_RETRIES": "ai_agent/embedding_retries",
	"AI_AGENT_RERANK_MODEL": "ai_agent/rerank_model",
	"AI_AGENT_RERANK_TIMEOUT_S": "ai_agent/rerank_timeout_s",
	"AI_AGENT_RAG_QUERY_ROUTER_ENABLED": "ai_agent/rag_query_router_enabled",
	"AI_AGENT_RAG_AUTO_BUILD_ENABLED": "ai_agent/rag_auto_build_enabled",
	"AI_AGENT_RAG_AUTO_WATCH_INTERVAL_S": "ai_agent/rag_auto_watch_interval_s",
	"AI_AGENT_RAG_AUTO_WATCH_DEBOUNCE_S": "ai_agent/rag_auto_watch_debounce_s",
	"AI_AGENT_RAG_TOKEN_BUDGET": "ai_agent/rag_token_budget",
	"AI_AGENT_GRAPH_MAX_DEPTH": "ai_agent/graph_max_depth",
	"AI_AGENT_GRAPH_MAX_NEIGHBORS": "ai_agent/graph_max_neighbors",
	"AI_AGENT_ASSET_UNDERSTANDING_ENABLED": "ai_agent/asset_understanding_enabled",
	"AI_AGENT_ASSET_UNDERSTANDING_MODEL": "ai_agent/asset_understanding_model",
	"AI_AGENT_ASSET_UNDERSTANDING_ENDPOINT": "ai_agent/asset_understanding_endpoint",
	"AI_AGENT_ASSET_UNDERSTANDING_API_KEY": "ai_agent/asset_understanding_api_key",
	"AI_AGENT_ASSET_UNDERSTANDING_TIMEOUT_S": "ai_agent/asset_understanding_timeout_s",
	"AI_AGENT_ASSET_UNDERSTANDING_MAX_TOKENS": "ai_agent/asset_understanding_max_tokens",
	"AI_AGENT_ASSET_UNDERSTANDING_CONCURRENCY": "ai_agent/asset_understanding_concurrency"
}

var editor_interface: EditorInterface
var token: String = ""
var base_url: String = ""

var _pid: int = -1


func start() -> void:
	if editor_interface == null:
		service_failed.emit("EditorInterface is not available.")
		return

	ConfigMigrations.apply_defaults(editor_interface)
	base_url = str(ConfigMigrations.get_value(editor_interface, "ai_agent/service_url"))
	token = _generate_token()

	var auto_start := bool(ConfigMigrations.get_value(editor_interface, "ai_agent/auto_start_service"))
	FrontendLogger.info(editor_interface, "Service", "Starting service manager.", {
		"auto_start": auto_start,
		"base_url": base_url
	})
	if auto_start:
		base_url = _with_port(base_url, _pick_port())
		FrontendLogger.debug(editor_interface, "Service", "Auto-start selected port.", {"base_url": base_url})
		_start_python_service()
		if _pid <= 0:
			return

	FrontendLogger.info(editor_interface, "Service", "Service manager ready.", {
		"base_url": base_url,
		"pid": _pid
	})
	service_started.emit(base_url)


func stop() -> void:
	if _pid > 0:
		FrontendLogger.info(editor_interface, "Service", "Stopping auto-started service.", {"pid": _pid})
		OS.kill(_pid)
	_pid = -1
	service_stopped.emit()


func is_running() -> bool:
	return _pid > 0


func _start_python_service() -> void:
	var module_dir := str(ConfigMigrations.get_value(editor_interface, "ai_agent/service_module_dir"))
	if module_dir.strip_edges().is_empty():
		FrontendLogger.error(editor_interface, "Service", "Service module directory is not configured.")
		service_failed.emit("ai_agent/service_module_dir is empty; start the Python service manually or configure it.")
		return
	module_dir = module_dir.strip_edges()

	var python := str(ConfigMigrations.get_value(editor_interface, "ai_agent/python_executable")).strip_edges()
	if python.is_empty():
		python = _detect_python(module_dir)
	elif _looks_like_path(python) and not FileAccess.file_exists(python):
		var detected_python := _detect_python(module_dir)
		if _looks_like_path(detected_python) and FileAccess.file_exists(detected_python):
			python = detected_python
		else:
			FrontendLogger.error(editor_interface, "Service", "Configured Python executable does not exist.", {
				"python": python
			})
			service_failed.emit("ai_agent/python_executable does not exist: " + python)
			return

	var old_project_root := OS.get_environment("AI_AGENT_PROJECT_ROOT")
	var old_port := OS.get_environment("AI_AGENT_PORT")
	var old_pythonpath := OS.get_environment("PYTHONPATH")
	var old_llm_base_url := OS.get_environment("AI_AGENT_LLM_BASE_URL")
	var old_llm_api_key := OS.get_environment("AI_AGENT_LLM_API_KEY")
	var old_llm_model := OS.get_environment("AI_AGENT_LLM_MODEL")
	var old_llm_quick_model := OS.get_environment("AI_AGENT_LLM_QUICK_MODEL")
	var old_llm_standard_model := OS.get_environment("AI_AGENT_LLM_STANDARD_MODEL")
	var old_llm_deep_model := OS.get_environment("AI_AGENT_LLM_DEEP_MODEL")
	var old_llm_verify_model := OS.get_environment("AI_AGENT_LLM_VERIFY_MODEL")
	var old_llm_advisor_model := OS.get_environment("AI_AGENT_LLM_ADVISOR_MODEL")
	var old_llm_fallback_model := OS.get_environment("AI_AGENT_LLM_FALLBACK_MODEL")
	var old_llm_timeout := OS.get_environment("AI_AGENT_LLM_REQUEST_TIMEOUT_S")
	var old_rag_environment := _capture_environment(RAG_ENV_SETTINGS.keys())
	var old_compact_environment := _capture_environment(
		["AI_AGENT_COMPACT_SUMMARY_USE_LLM"] + COMPACT_ENV_SETTINGS.keys()
	)
	var old_managed_process := OS.get_environment("AI_AGENT_MANAGED_PROCESS")
	# 子进程的 stdout/stderr 由 `execute_with_pipe()` 的 stdio 管道持有，本插件
	# 只用它给子进程写入 token，从不读取输出。管道写满后子进程下一次写日志会
	# 永久阻塞、冻住它的事件循环，导致所有 HTTP 请求（包括 /chat/interrupt、
	# /doctor）都卡死。告诉后端它是被管理的子进程，让它跳过控制台 handler，
	# 只写文件日志。
	OS.set_environment("AI_AGENT_MANAGED_PROCESS", "1")
	OS.set_environment("AI_AGENT_PROJECT_ROOT", ProjectSettings.globalize_path("res://"))
	OS.set_environment("AI_AGENT_PORT", _port_from_url(base_url))
	OS.set_environment("PYTHONPATH", module_dir + _path_separator() + old_pythonpath)
	_apply_llm_environment()
	_apply_rag_environment()
	_apply_compact_environment()
	# token 经 stdin 首行传入（--token-stdin），不放命令行/环境变量——避免出现在系统进程列表。
	var args := ["-m", "app.main", "--token-stdin"]
	FrontendLogger.info(editor_interface, "Service", "Launching Python service.", {
		"python": python,
		"module_dir": module_dir,
		"base_url": base_url
	})
	var pipe := OS.execute_with_pipe(python, args)
	OS.set_environment("AI_AGENT_MANAGED_PROCESS", old_managed_process)
	OS.set_environment("AI_AGENT_PROJECT_ROOT", old_project_root)
	OS.set_environment("AI_AGENT_PORT", old_port)
	OS.set_environment("PYTHONPATH", old_pythonpath)
	OS.set_environment("AI_AGENT_LLM_BASE_URL", old_llm_base_url)
	OS.set_environment("AI_AGENT_LLM_API_KEY", old_llm_api_key)
	OS.set_environment("AI_AGENT_LLM_MODEL", old_llm_model)
	OS.set_environment("AI_AGENT_LLM_QUICK_MODEL", old_llm_quick_model)
	OS.set_environment("AI_AGENT_LLM_STANDARD_MODEL", old_llm_standard_model)
	OS.set_environment("AI_AGENT_LLM_DEEP_MODEL", old_llm_deep_model)
	OS.set_environment("AI_AGENT_LLM_VERIFY_MODEL", old_llm_verify_model)
	OS.set_environment("AI_AGENT_LLM_ADVISOR_MODEL", old_llm_advisor_model)
	OS.set_environment("AI_AGENT_LLM_FALLBACK_MODEL", old_llm_fallback_model)
	OS.set_environment("AI_AGENT_LLM_REQUEST_TIMEOUT_S", old_llm_timeout)
	_restore_environment(old_rag_environment)
	_restore_environment(old_compact_environment)

	if pipe.is_empty() or int(pipe.get("pid", -1)) <= 0:
		FrontendLogger.error(editor_interface, "Service", "Failed to create Python service process.", {
			"python": python,
			"module_dir": module_dir
		})
		service_failed.emit("Failed to start Python service with: " + python)
		return

	_pid = int(pipe["pid"])
	FrontendLogger.info(editor_interface, "Service", "Python service process created.", {"pid": _pid})
	var stdio: FileAccess = pipe.get("stdio")
	if stdio != null:
		stdio.store_line(token)
		stdio.flush()


func _detect_python(module_dir: String = "") -> String:
	if OS.get_name() == "Windows":
		var candidates: Array[String] = []
		var project_root := ProjectSettings.globalize_path("res://")
		candidates.append(project_root.path_join(".venv").path_join("Scripts").path_join("python.exe"))
		if not module_dir.strip_edges().is_empty():
			candidates.append(module_dir.get_base_dir().path_join(".venv").path_join("Scripts").path_join("python.exe"))
			candidates.append(module_dir.path_join(".venv").path_join("Scripts").path_join("python.exe"))
		for candidate in candidates:
			if FileAccess.file_exists(candidate):
				return candidate
		return "python"
	return "python3"


func _looks_like_path(value: String) -> bool:
	return value.is_absolute_path() or value.find("/") >= 0 or value.find("\\") >= 0


func _path_separator() -> String:
	return ";" if OS.get_name() == "Windows" else ":"


func _apply_llm_environment() -> void:
	_set_env_from_setting("AI_AGENT_LLM_BASE_URL", "ai_agent/llm_base_url")
	_set_env_from_setting("AI_AGENT_LLM_API_KEY", "ai_agent/llm_api_key")
	_set_env_from_setting("AI_AGENT_LLM_MODEL", "ai_agent/llm_model")
	_set_env_from_setting("AI_AGENT_LLM_QUICK_MODEL", "ai_agent/llm_quick_model")
	_set_env_from_setting("AI_AGENT_LLM_STANDARD_MODEL", "ai_agent/llm_standard_model")
	_set_env_from_setting("AI_AGENT_LLM_DEEP_MODEL", "ai_agent/llm_deep_model")
	_set_env_from_setting("AI_AGENT_LLM_VERIFY_MODEL", "ai_agent/llm_verify_model")
	_set_env_from_setting("AI_AGENT_LLM_ADVISOR_MODEL", "ai_agent/llm_advisor_model")
	_set_env_from_setting("AI_AGENT_LLM_FALLBACK_MODEL", "ai_agent/llm_fallback_model")
	_set_env_from_setting("AI_AGENT_LLM_REQUEST_TIMEOUT_S", "ai_agent/llm_request_timeout_s")


func _apply_rag_environment() -> void:
	for env_key in RAG_ENV_SETTINGS:
		_set_env_from_setting(str(env_key), str(RAG_ENV_SETTINGS[env_key]))


func _apply_compact_environment() -> void:
	# `ai_agent/compact_summary_use_llm` 是三态枚举（default/on/off），仅在
	# 用户显式选择 on/off 时才写入环境变量；"default" 时刻意不设置该变量，
	# 留给后端 `compact_summary_use_llm` 字段保持其默认值——若写入空字符串，
	# pydantic 在解析这个 bool 字段时会直接报错而无法启动服务。
	var use_llm_mode := str(ConfigMigrations.get_value(editor_interface, "ai_agent/compact_summary_use_llm")).strip_edges()
	if use_llm_mode == "on":
		OS.set_environment("AI_AGENT_COMPACT_SUMMARY_USE_LLM", "true")
	elif use_llm_mode == "off":
		OS.set_environment("AI_AGENT_COMPACT_SUMMARY_USE_LLM", "false")
	for env_key in COMPACT_ENV_SETTINGS:
		_set_env_from_setting(str(env_key), str(COMPACT_ENV_SETTINGS[env_key]))


func _capture_environment(keys: Array) -> Dictionary:
	var snapshot := {}
	for key in keys:
		snapshot[str(key)] = OS.get_environment(str(key))
	return snapshot


func _restore_environment(snapshot: Dictionary) -> void:
	for key in snapshot:
		OS.set_environment(str(key), str(snapshot[key]))


func _set_env_from_setting(env_key: String, setting_key: String) -> void:
	var value := str(ConfigMigrations.get_value(editor_interface, setting_key)).strip_edges()
	OS.set_environment(env_key, value)


func _port_from_url(url: String) -> String:
	var parts := url.split(":")
	if parts.size() < 3:
		return "8765"
	var tail := str(parts[2])
	return tail.split("/")[0]


func _with_port(url: String, port: int) -> String:
	var parts := url.split(":")
	if parts.size() < 3:
		return url
	var tail := str(parts[2])
	var path_part := ""
	var slash_index := tail.find("/")
	if slash_index >= 0:
		path_part = tail.substr(slash_index)
	return "%s:%s:%d%s" % [parts[0], parts[1], port, path_part]


func _pick_port() -> int:
	return randi_range(PORT_MIN, PORT_MAX)


func _generate_token() -> String:
	var crypto := Crypto.new()
	var bytes := crypto.generate_random_bytes(32)
	return Marshalls.raw_to_base64(bytes)
