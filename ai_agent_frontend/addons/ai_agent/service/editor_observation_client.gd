@tool
extends Node

## 只读、回环地址限定的 EditorPlugin CodeAct 观察客户端。

const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

var _plugin: EditorPlugin
var _editor_interface: EditorInterface
var _service: Node
var _registration_http: HTTPRequest
var _socket: WebSocketPeer
var _instance_id := ""
var _project_id := ""
var _hello_token := ""
var _hello_sent := false
var _running := false
var _saved_scene_versions: Dictionary = {}


func configure(plugin: EditorPlugin, editor_interface: EditorInterface, service: Node) -> void:
	_plugin = plugin
	_editor_interface = editor_interface
	_service = service
	_instance_id = "%s-%s" % [OS.get_process_id(), Time.get_ticks_usec()]
	_project_id = ProjectSettings.globalize_path("res://").trim_suffix("/").trim_suffix("\\")
	_registration_http = HTTPRequest.new()
	_registration_http.request_completed.connect(_on_registration_completed)
	add_child(_registration_http)
	if not _plugin.scene_saved.is_connected(_on_scene_saved):
		_plugin.scene_saved.connect(_on_scene_saved)
	_record_active_scene_version()


func start() -> void:
	_running = true
	set_process(true)
	if not _service.service_started.is_connected(_on_service_started):
		_service.service_started.connect(_on_service_started)
	if str(_service.base_url) != "":
		_register(str(_service.base_url))


func stop() -> void:
	_running = false
	set_process(false)
	_socket = null
	if _registration_http != null:
		_registration_http.cancel_request()


func _on_service_started(base_url: String) -> void:
	_register(base_url)


func _register(base_url: String) -> void:
	if not _running or not _is_loopback_url(base_url):
		return
	var body := JSON.stringify({"project_id": _project_id, "instance_id": _instance_id})
	var headers := PackedStringArray(["Content-Type: application/json"])
	if str(_service.token) != "":
		headers.append("Authorization: Bearer " + str(_service.token))
	var error := _registration_http.request(
		base_url.trim_suffix("/") + "/internal/codeact/editor/register",
		headers,
		HTTPClient.METHOD_POST,
		body
	)
	if error != OK:
		FrontendLogger.warn(_editor_interface, "EditorObservation", "Registration request failed.", {"error": error})


func _on_registration_completed(result: int, response_code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	if result != HTTPRequest.RESULT_SUCCESS or response_code < 200 or response_code >= 300:
		return
	var decoded = JSON.parse_string(body.get_string_from_utf8())
	if not (decoded is Dictionary):
		return
	_hello_token = str(decoded.get("token", ""))
	if _hello_token == "":
		return
	_socket = WebSocketPeer.new()
	_hello_sent = false
	# 握手必须与主通道一致携带 Bearer：服务端 editor socket 路由同样受
	# 全局鉴权保护，缺少 Authorization 会得到 401（Godot Output 打印
	# "Invalid status code. Got: '401', expected '101'"）。
	if str(_service.token) != "":
		_socket.handshake_headers = PackedStringArray(
			["Authorization: Bearer " + str(_service.token)]
		)
	var socket_url := _websocket_root(str(_service.base_url)) + "/internal/codeact/editor/socket?project_id=%s&instance_id=%s" % [
		_project_id.uri_encode(),
		_instance_id.uri_encode(),
	]
	_socket.connect_to_url(socket_url)


func _process(_delta: float) -> void:
	if _socket == null:
		return
	_socket.poll()
	if _socket.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	if not _hello_sent:
		_socket.send_text(JSON.stringify({"token": _hello_token}))
		_hello_sent = true
	while _socket.get_available_packet_count() > 0:
		var decoded = JSON.parse_string(_socket.get_packet().get_string_from_utf8())
		if decoded is Dictionary:
			_socket.send_text(JSON.stringify(_handle_request(decoded)))


func _handle_request(request: Dictionary) -> Dictionary:
	var method := str(request.get("method", ""))
	var parameters: Dictionary = request.get("parameters", {}) if request.get("parameters") is Dictionary else {}
	var response := {
		"task_execution_id": str(request.get("task_execution_id", "")),
		"call_id": str(request.get("call_id", "")),
		"method": method,
		"project_id": _project_id,
	}
	match method:
		"godot.editor.status":
			response.merge(_status_payload())
		"godot.editor.reload_for_validation":
			response.merge(_reload_for_validation(str(parameters.get("path", ""))))
		"godot.editor.viewport_capture":
			response.merge(_capture_viewport())
		"godot.editor.runtime_state":
			response.merge({"playing": _editor_interface.is_playing_scene(), "playing_scene": _editor_interface.get_playing_scene()})
		"godot.editor.debugger_errors", "godot.editor.profiler_snapshot":
			response["error_code"] = "editor_unavailable"
			response["message"] = "This Godot build does not expose a stable read-only API for this observation."
		_:
			response["error_code"] = "authorization_denied"
	return response


func _status_payload() -> Dictionary:
	var opened_files: Dictionary = {}
	for path in _editor_interface.get_open_scenes():
		var relative := str(path).trim_prefix("res://")
		opened_files[relative] = _scene_dirty(relative)
	return {"opened_files": opened_files, "playing": _editor_interface.is_playing_scene()}


func _reload_for_validation(path: String) -> Dictionary:
	var relative := path.trim_prefix("res://")
	var status := _status_payload()
	var opened: Dictionary = status.get("opened_files", {})
	if not opened.has(relative):
		return {"error_code": "editor_unavailable", "message": "target is not open"}
	if bool(opened[relative]):
		return {"error_code": "editor_dirty_conflict", "message": "target has unsaved Editor changes"}
	if not _editor_interface.has_method("reload_scene_from_path"):
		return {"error_code": "editor_unavailable", "message": "safe reload API is unavailable"}
	_editor_interface.call("reload_scene_from_path", "res://" + relative)
	_record_active_scene_version()
	return {"reloaded": true, "path": relative}


func _capture_viewport() -> Dictionary:
	var viewport := _editor_interface.get_editor_viewport_2d()
	if viewport == null or viewport.get_texture() == null:
		return {"error_code": "editor_unavailable", "message": "2D viewport is unavailable"}
	var png := viewport.get_texture().get_image().save_png_to_buffer()
	return {"artifact": {"mime_type": "image/png", "data_base64": Marshalls.raw_to_base64(png)}}


func _scene_dirty(relative_path: String) -> bool:
	var root := _editor_interface.get_edited_scene_root()
	if root == null or str(root.scene_file_path).trim_prefix("res://") != relative_path:
		return true
	var undo := _plugin.get_undo_redo()
	var current_version := undo.get_history_undo_redo(0).get_version()
	return int(_saved_scene_versions.get(relative_path, -1)) != current_version


func _record_active_scene_version() -> void:
	var root := _editor_interface.get_edited_scene_root()
	if root == null or str(root.scene_file_path) == "":
		return
	var relative := str(root.scene_file_path).trim_prefix("res://")
	_saved_scene_versions[relative] = _plugin.get_undo_redo().get_history_undo_redo(0).get_version()


func _on_scene_saved(path: String) -> void:
	var relative := path.trim_prefix("res://")
	_saved_scene_versions[relative] = _plugin.get_undo_redo().get_history_undo_redo(0).get_version()


func _is_loopback_url(url: String) -> bool:
	var lowered := url.to_lower()
	return lowered.begins_with("http://127.0.0.1") or lowered.begins_with("http://localhost") or lowered.begins_with("http://[::1]")


func _websocket_root(http_root: String) -> String:
	if http_root.begins_with("https://"):
		return "wss://" + http_root.trim_prefix("https://").trim_suffix("/")
	return "ws://" + http_root.trim_prefix("http://").trim_suffix("/")
