@tool
extends Node

signal response_received(response: Dictionary)
signal events_received(events: Array)
signal error_occurred(message: String)

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

var editor_interface: EditorInterface
var service: Node
var current_turn_id: String = ""

var _http: HTTPRequest
var _event_http: HTTPRequest
var _queue: Array[Dictionary] = []
var _busy := false
var _interrupted := false
var _request_generation := 0
var _last_event_seq := 0
var _suppress_events := false
var _event_timer: Timer


func _ready() -> void:
	_http = HTTPRequest.new()
	_http.name = "ChatHttp"
	add_child(_http)
	_http.request_completed.connect(_on_request_completed)

	_event_http = HTTPRequest.new()
	_event_http.name = "EventHttp"
	add_child(_event_http)
	_event_http.request_completed.connect(_on_events_completed)

	_event_timer = Timer.new()
	_event_timer.one_shot = false
	add_child(_event_timer)
	_event_timer.timeout.connect(poll_events)
	_configure_event_timer()


func send_user_message(text: String, context: Dictionary) -> void:
	_suppress_events = false
	_configure_event_timer()
	FrontendLogger.info(editor_interface, "HTTP", "Queueing user message.", {
		"chars": text.length(),
		"has_context": not context.is_empty()
	})
	var payload := {
		"session_id": _session_id(),
		"request_id": _new_request_id(),
		"user_message": text,
		"context": context,
		"permission_mode": _setting("ai_agent/permission_mode"),
		"effort": _setting("ai_agent/effort"),
		"output_style": _setting("ai_agent/output_style"),
		"engine_version": Engine.get_version_info().get("string", ""),
		"language_hint": _language_hint()
	}
	_enqueue("POST", "/chat", payload)


func send_tool_results(results: Array) -> void:
	FrontendLogger.info(editor_interface, "HTTP", "Queueing tool results.", {"count": results.size()})
	for item in results:
		if item is Dictionary:
			item["turn_id"] = current_turn_id
	var payload := {
		"session_id": _session_id(),
		"request_id": _new_request_id(),
		"tool_results": results
	}
	_enqueue("POST", "/chat", payload)


func reset_session() -> void:
	current_turn_id = ""
	_request_generation += 1
	_interrupted = false
	_suppress_events = false
	FrontendLogger.info(editor_interface, "HTTP", "Queueing session reset.", {"session_id": _session_id()})
	_enqueue("POST", "/reset", {"session_id": _session_id()})


func interrupt_current() -> void:
	FrontendLogger.warn(editor_interface, "HTTP", "Interrupting current request.", {"queue_size": _queue.size()})
	_request_generation += 1
	_suppress_events = true
	_queue.clear()
	if _http.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
		_interrupted = true
		_http.cancel_request()
	else:
		_busy = false
	if _event_http.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
		_event_http.cancel_request()
	if _event_timer != null:
		_event_timer.stop()
	current_turn_id = ""


func discard_pending() -> void:
	current_turn_id = ""
	FrontendLogger.info(editor_interface, "HTTP", "Queueing pending tool result discard.", {"session_id": _session_id()})
	_enqueue("POST", "/chat/discard-pending", {"session_id": _session_id()})


func fetch_doctor() -> void:
	_enqueue("GET", "/doctor", {})


func fetch_commands() -> void:
	_enqueue("GET", "/commands", {})


func fetch_skills() -> void:
	_enqueue("GET", "/skills", {})


func fetch_output_styles() -> void:
	_enqueue("GET", "/output-styles", {})


func run_command(name: String, args: Dictionary = {}) -> void:
	_enqueue("POST", "/commands/" + name.uri_encode(), {"session_id": _session_id(), "args": args})


func fetch_memory() -> void:
	_enqueue("GET", "/memory", {})


func save_memory(text: String, tags: Array = []) -> void:
	_enqueue("POST", "/memory", {"action": "save", "text": text, "tags": tags, "scope": "project"})


func delete_memory(item_id: String) -> void:
	_enqueue("POST", "/memory", {"action": "delete", "id": item_id})


func clear_memory() -> void:
	_enqueue("POST", "/memory", {"action": "clear"})


func fetch_recovery_pointer() -> void:
	_enqueue("GET", "/recovery-pointer", {})


func fetch_session_history(limit: int = 200) -> void:
	var path := "/sessions/%s/history?limit=%d" % [_session_id().uri_encode(), limit]
	_enqueue("GET", path, {})


## 从恢复指针同步本地事件序号与挂起的 turn_id，供恢复提示"接受"分支调用。
func resume_from_pointer(pointer: Dictionary) -> void:
	_last_event_seq = max(_last_event_seq, int(pointer.get("last_event_seq", 0)))
	var pending_turn_id = pointer.get("pending_turn_id")
	current_turn_id = str(pending_turn_id) if pending_turn_id != null else ""


func poll_events() -> void:
	if _event_http.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
		return
	var path := "/chat/events?session_id=%s&after=%d" % [_session_id().uri_encode(), _last_event_seq]
	var err := _event_http.request(_url(path), _headers(), HTTPClient.METHOD_GET)
	if err != OK:
		FrontendLogger.warn(editor_interface, "HTTP", "Failed to start event poll.", {"error": err})
		error_occurred.emit("Failed to poll events: " + str(err))


func _enqueue(method: String, path: String, payload: Dictionary) -> void:
	FrontendLogger.debug(editor_interface, "HTTP", "Enqueued request.", {
		"method": method,
		"path": path,
		"queue_size": _queue.size() + 1
	})
	_queue.append({
		"method": method,
		"path": path,
		"payload": payload,
		"generation": _request_generation
	})
	_pump()


func _pump() -> void:
	if _busy or _queue.is_empty():
		return
	var item: Dictionary = _queue.pop_front()
	while int(item.get("generation", _request_generation)) != _request_generation:
		if _queue.is_empty():
			return
		item = _queue.pop_front()
	_busy = true
	var method_name := str(item["method"])
	var method := HTTPClient.METHOD_GET
	var body := ""
	if method_name == "POST":
		method = HTTPClient.METHOD_POST
		body = JSON.stringify(item["payload"])
	var err := _http.request(_url(str(item["path"])), _headers(), method, body)
	if err != OK:
		_busy = false
		FrontendLogger.error(editor_interface, "HTTP", "Failed to start HTTP request.", {
			"path": str(item["path"]),
			"error": err
		})
		error_occurred.emit("HTTP request failed: " + str(err))
		_pump()


func _on_request_completed(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	_busy = false
	if _interrupted:
		_interrupted = false
		FrontendLogger.info(editor_interface, "HTTP", "Interrupted request completion ignored.")
		_pump()
		return
	var text := body.get_string_from_utf8()
	if result != HTTPRequest.RESULT_SUCCESS or code < 200 or code >= 300:
		if _suppress_events:
			FrontendLogger.info(editor_interface, "HTTP", "Suppressed HTTP failure after interrupt.", {
				"code": code,
				"result": result
			})
			_pump()
			return
		FrontendLogger.error(editor_interface, "HTTP", "HTTP request failed.", {
			"code": code,
			"result": result,
			"body_chars": text.length()
		})
		error_occurred.emit("HTTP %d: %s" % [code, text])
		_pump()
		return

	var parsed := JSON.parse_string(text)
	if parsed == null:
		FrontendLogger.error(editor_interface, "HTTP", "Invalid JSON response.", {"body_chars": text.length()})
		error_occurred.emit("Invalid JSON response.")
		_pump()
		return

	if parsed is Dictionary:
		var response: Dictionary = parsed
		FrontendLogger.debug(editor_interface, "HTTP", "Received response.", {
			"type": str(response.get("type", "data")),
			"keys": response.keys()
		})
		if response.get("type", "") == "tool_calls":
			current_turn_id = str(response.get("turn_id", ""))
		response_received.emit(response)
	else:
		response_received.emit({"type": "data", "value": parsed})
	_pump()


func _on_events_completed(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	if result != HTTPRequest.RESULT_SUCCESS or code < 200 or code >= 300:
		return
	var parsed := JSON.parse_string(body.get_string_from_utf8())
	if parsed is Dictionary:
		var events: Array = parsed.get("events", [])
		if not events.is_empty():
			FrontendLogger.debug(editor_interface, "HTTP", "Received events.", {"count": events.size()})
		for event in events:
			if event is Dictionary:
				_last_event_seq = max(_last_event_seq, int(event.get("seq", _last_event_seq)))
		if _suppress_events:
			if not events.is_empty():
				FrontendLogger.debug(editor_interface, "HTTP", "Suppressed events after interrupt.", {"count": events.size()})
			return
		if not events.is_empty():
			events_received.emit(events)


func _headers() -> PackedStringArray:
	var result := PackedStringArray(["Content-Type: application/json"])
	if service != null and str(service.token) != "":
		result.append("Authorization: Bearer " + str(service.token))
	return result


func _url(path: String) -> String:
	var root := ""
	if service != null:
		root = str(service.base_url)
	if root.strip_edges().is_empty():
		root = str(_setting("ai_agent/service_url"))
	while root.ends_with("/"):
		root = root.substr(0, root.length() - 1)
	return root + path


func _setting(key: String) -> Variant:
	if editor_interface == null:
		return ""
	return ConfigMigrations.get_value(editor_interface, key)


func _session_id() -> String:
	return str(_setting("ai_agent/session_id"))


func _language_hint() -> String:
	var root := ProjectSettings.globalize_path("res://")
	if FileAccess.file_exists(root.path_join(".csproj")):
		return "csharp"
	return "gdscript"


func _new_request_id() -> String:
	return "%d-%d" % [Time.get_ticks_usec(), randi()]


func _configure_event_timer() -> void:
	if editor_interface == null:
		return
	if bool(_setting("ai_agent/enable_event_stream")):
		_event_timer.wait_time = max(0.2, float(_setting("ai_agent/event_poll_interval_sec")))
		_event_timer.start()
	else:
		_event_timer.stop()
