@tool
extends Node

signal service_started(base_url: String)
signal service_stopped
signal service_failed(message: String)

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")

const PORT_MIN := 49152
const PORT_MAX := 65535

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
	if auto_start:
		base_url = _with_port(base_url, _pick_port())
		_start_python_service()
		if _pid <= 0:
			return

	service_started.emit(base_url)


func stop() -> void:
	if _pid > 0:
		OS.kill(_pid)
	_pid = -1
	service_stopped.emit()


func is_running() -> bool:
	return _pid > 0


func _start_python_service() -> void:
	var python := str(ConfigMigrations.get_value(editor_interface, "ai_agent/python_executable"))
	if python.strip_edges().is_empty():
		python = _detect_python()

	var module_dir := str(ConfigMigrations.get_value(editor_interface, "ai_agent/service_module_dir"))
	if module_dir.strip_edges().is_empty():
		service_failed.emit("ai_agent/service_module_dir is empty; start the Python service manually or configure it.")
		return

	var old_project_root := OS.get_environment("AI_AGENT_PROJECT_ROOT")
	var old_port := OS.get_environment("AI_AGENT_PORT")
	var old_pythonpath := OS.get_environment("PYTHONPATH")
	OS.set_environment("AI_AGENT_PROJECT_ROOT", ProjectSettings.globalize_path("res://"))
	OS.set_environment("AI_AGENT_PORT", _port_from_url(base_url))
	OS.set_environment("PYTHONPATH", module_dir + _path_separator() + old_pythonpath)
	# token 经 stdin 首行传入（--token-stdin），不放命令行/环境变量——避免出现在系统进程列表。
	var args := ["-m", "app.main", "--token-stdin"]
	var pipe := OS.execute_with_pipe(python, args)
	OS.set_environment("AI_AGENT_PROJECT_ROOT", old_project_root)
	OS.set_environment("AI_AGENT_PORT", old_port)
	OS.set_environment("PYTHONPATH", old_pythonpath)

	if pipe.is_empty() or int(pipe.get("pid", -1)) <= 0:
		service_failed.emit("Failed to start Python service with: " + python)
		return

	_pid = int(pipe["pid"])
	var stdio: FileAccess = pipe.get("stdio")
	if stdio != null:
		stdio.store_line(token)
		stdio.flush()


func _detect_python() -> String:
	if OS.get_name() == "Windows":
		return "python"
	return "python3"


func _path_separator() -> String:
	return ";" if OS.get_name() == "Windows" else ":"


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
