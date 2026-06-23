@tool
extends Node

signal response_received(response: Dictionary)
signal events_received(events: Array)
signal error_occurred(message: String)

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

## 后端偶发卡死（例如 RAG 文件监视器扫描超时、重排模型网络请求挂起）时，
## 单条请求可能永远不触发 `request_completed`。队列是严格串行的（见
## `_pump`），一旦卡住就会让诊断/扩展/命令/记忆等后续点击全部悄悄堆积、永
## 不发出。这个看门狗超时保证即使后端没有响应，本地队列也能在有限时间内
## 恢复，而不是永久冻住。
##
## `/chat` 本身可能合法地跑很久（deep effort、delegate_many 派给多个子 agent、
## 工具执行……），不能套用和 `/doctor`、`/memory` 这类本该秒回的轻量端点一样
## 的短超时，所以 `/chat` 用单独的、更宽松的超时设置。
const DEFAULT_REQUEST_TIMEOUT_S := 30.0
const DEFAULT_CHAT_REQUEST_TIMEOUT_S := 300.0
## 上面那个超时现在按"空闲"语义续期（见 `_on_events_completed`）：只要
## `/chat/events` 轮询还能拿到新事件（流式文本、delegate、工具调用……），
## 就说明后端没卡死，超时计时器会被重置而不是任由它在固定总时长后到期。
## 这个硬上限是兜底：哪怕事件一直在零星地来、后端实际已经死循环/卡死，
## 单条 `/chat` 请求也不会无限续期下去。
const DEFAULT_CHAT_REQUEST_HARD_CAP_S := 1800.0

var editor_interface: EditorInterface
var service: Node
var current_turn_id: String = ""

var _http: HTTPRequest
var _event_http: HTTPRequest
var _queue: Array[Dictionary] = []
var _busy := false
var _inflight_generation := -1
var _inflight_path := ""
var _inflight_session_id := ""
var _request_generation := 0
var _last_event_seq := 0
var _suppress_events := false
var _event_timer: Timer
var _request_timeout_timer: Timer
var _timeout_generation := -1
var _inflight_started_at_msec := 0


func _ready() -> void:
	_create_chat_http()

	_create_event_http()

	_event_timer = Timer.new()
	_event_timer.one_shot = false
	add_child(_event_timer)
	_event_timer.timeout.connect(poll_events)
	_event_timer.stop()

	_request_timeout_timer = Timer.new()
	_request_timeout_timer.one_shot = true
	add_child(_request_timeout_timer)
	_request_timeout_timer.timeout.connect(_on_request_timeout)


func _create_chat_http() -> void:
	_http = HTTPRequest.new()
	_http.name = "ChatHttp"
	add_child(_http)
	_http.request_completed.connect(_on_request_completed)


func _create_event_http() -> void:
	_event_http = HTTPRequest.new()
	_event_http.name = "EventHttp"
	add_child(_event_http)
	_event_http.request_completed.connect(_on_events_completed)


func _replace_chat_http() -> void:
	# A cancelled HTTPRequest may emit completion later. Destroying the node also
	# destroys that signal source, so it cannot mutate a newer request's state.
	if is_instance_valid(_http):
		if _http.request_completed.is_connected(_on_request_completed):
			_http.request_completed.disconnect(_on_request_completed)
		if _http.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
			_http.cancel_request()
		_http.queue_free()
	_create_chat_http()


func _replace_event_http() -> void:
	if is_instance_valid(_event_http):
		if _event_http.request_completed.is_connected(_on_events_completed):
			_event_http.request_completed.disconnect(_on_events_completed)
		if _event_http.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
			_event_http.cancel_request()
		_event_http.queue_free()
	_create_event_http()


func send_user_message(text: String, context: Dictionary, model = null) -> void:
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
		"model": model,
		"engine_version": Engine.get_version_info().get("string", ""),
		"language_hint": _language_hint(),
		"compact_summary_use_llm": _compact_summary_use_llm_override()
	}
	_enqueue("POST", "/chat", payload)


func send_tool_results(results: Array, model = null) -> void:
	FrontendLogger.info(editor_interface, "HTTP", "Queueing tool results.", {"count": results.size()})
	for item in results:
		if item is Dictionary:
			item["turn_id"] = current_turn_id
	var payload := {
		"session_id": _session_id(),
		"request_id": _new_request_id(),
		"model": model,
		"tool_results": results,
		"compact_summary_use_llm": _compact_summary_use_llm_override()
	}
	_enqueue("POST", "/chat", payload)


func _compact_summary_use_llm_override() -> Variant:
	# 三态配置项映射为 ChatRequest.compact_summary_use_llm 的 None/True/False：
	# "default" 时传 null，沿用服务端 `compact_summary_use_llm` 配置。
	match str(_setting("ai_agent/compact_summary_use_llm")):
		"on":
			return true
		"off":
			return false
		_:
			return null


func reset_session() -> void:
	current_turn_id = ""
	_request_generation += 1
	_suppress_events = false
	FrontendLogger.info(editor_interface, "HTTP", "Queueing session reset.", {"session_id": _session_id()})
	_enqueue("POST", "/reset", {"session_id": _session_id()})


func start_new_session(previous_session_id: String, new_session_id: String) -> void:
	FrontendLogger.warn(editor_interface, "HTTP", "Starting new session.", {
		"previous_session_id": previous_session_id,
		"new_session_id": new_session_id,
		"queue_size": _queue.size()
	})
	_request_generation += 1
	_queue.clear()
	_replace_chat_http()
	_busy = false
	_request_timeout_timer.stop()
	_replace_event_http()
	current_turn_id = ""
	_last_event_seq = 0
	_suppress_events = false
	if previous_session_id.strip_edges() != "" and previous_session_id != new_session_id:
		_enqueue("POST", "/chat/interrupt", {"session_id": previous_session_id})
	_enqueue("POST", "/reset", {"session_id": new_session_id})
	_configure_event_timer()


func interrupt_current() -> void:
	FrontendLogger.warn(editor_interface, "HTTP", "Interrupting current request.", {"queue_size": _queue.size()})
	_request_generation += 1
	_suppress_events = true
	_queue.clear()
	_replace_chat_http()
	# `cancel_request()` 不保证触发 `request_completed`（曾导致 `_busy` 卡死为
	# true，后续所有请求——包括下面要发的 `/chat/interrupt` 和用户的下一条
	# 消息——永远排在队列里发不出去）。这里不再等待那个信号，直接复位，
	# 迟到的信号会被 `_on_request_completed` 的生成号检查丢弃。
	_busy = false
	_request_timeout_timer.stop()
	_replace_event_http()
	if _event_timer != null:
		_event_timer.stop()
	current_turn_id = ""
	# 仅断开本地连接还不够：后端的 agent 循环（自动执行的静默工具）会继续跑完
	# 整轮并持续写入新事件，等下一条用户消息发出后这些旧事件会被一起拉取、
	# 误渲染成新对话内容。这里显式通知后端取消该会话仍在运行的请求。
	_enqueue("POST", "/chat/interrupt", {"session_id": _session_id()})


func switch_to_session(previous_session_id: String) -> void:
	FrontendLogger.info(editor_interface, "HTTP", "Switching session.", {
		"from": previous_session_id,
		"to": _session_id()
	})
	_request_generation += 1
	_queue.clear()
	_replace_chat_http()
	_busy = false
	_request_timeout_timer.stop()
	_replace_event_http()
	current_turn_id = ""
	_last_event_seq = 0
	_suppress_events = false
	if previous_session_id.strip_edges() != "" and previous_session_id != _session_id():
		_enqueue("POST", "/chat/interrupt", {"session_id": previous_session_id})
	_configure_event_timer()


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


func sync_event_cursor(last_event_seq: int) -> void:
	_last_event_seq = max(_last_event_seq, last_event_seq)


## 从恢复指针同步本地事件序号与挂起的 turn_id，供恢复提示"接受"分支调用。
func resume_from_pointer(pointer: Dictionary) -> void:
	_last_event_seq = max(_last_event_seq, int(pointer.get("last_event_seq", 0)))
	var pending_turn_id = pointer.get("pending_turn_id")
	current_turn_id = str(pending_turn_id) if pending_turn_id != null else ""
	_configure_event_timer()


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
	_inflight_generation = int(item.get("generation", _request_generation))
	_inflight_path = str(item["path"])
	var payload: Dictionary = item.get("payload", {}) if item.get("payload", {}) is Dictionary else {}
	_inflight_session_id = str(payload.get("session_id", ""))
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
	else:
		_timeout_generation = _inflight_generation
		_inflight_started_at_msec = Time.get_ticks_msec()
		_request_timeout_timer.start(_timeout_for_path(_inflight_path))


func _timeout_for_path(path: String) -> float:
	# `rebuild_index` 扫描全项目文件并逐块调用 embedding（可能还有 asset
	# 理解模型），耗时量级和 `/chat` 一样，不是 `/doctor`、`/memory` 这类该
	# 秒回的轻量端点。套用和它们一样的默认 30s 超时，只会让正常的大项目重建
	# 索引在后端还在跑的时候就被前端误判成"卡住"。
	if path == "/chat" or path == "/commands/rebuild_index":
		var chat_value := float(_setting("ai_agent/chat_request_timeout_sec"))
		return chat_value if chat_value > 0.0 else DEFAULT_CHAT_REQUEST_TIMEOUT_S
	var value := float(_setting("ai_agent/request_timeout_sec"))
	return value if value > 0.0 else DEFAULT_REQUEST_TIMEOUT_S


func _hard_cap_for_path(path: String) -> float:
	if path != "/chat" and path != "/commands/rebuild_index":
		return 0.0
	var cap := float(_setting("ai_agent/chat_request_hard_cap_sec"))
	return cap if cap > 0.0 else DEFAULT_CHAT_REQUEST_HARD_CAP_S


## 收到新事件说明这条仍在跑的 `/chat`（或 `rebuild_index`）请求后端没有
## 卡死，把"固定总时长"超时改写成"距离上一次有进展多久"的空闲超时——
## 真正耗时的长任务（深度 effort、delegate_many、多轮工具调用）只要还在
## 持续产生事件就不会被误判超时；`_hard_cap_for_path` 兜底真正卡死的场景。
func _maybe_extend_request_timeout() -> void:
	if not _busy or _timeout_generation != _request_generation:
		return
	if _inflight_session_id != _session_id():
		return
	var hard_cap := _hard_cap_for_path(_inflight_path)
	if hard_cap <= 0.0:
		return
	var elapsed_s := float(Time.get_ticks_msec() - _inflight_started_at_msec) / 1000.0
	if elapsed_s >= hard_cap:
		return
	FrontendLogger.debug(editor_interface, "HTTP", "Extending in-flight request timeout on new events.", {
		"path": _inflight_path,
		"elapsed_s": elapsed_s,
		"hard_cap_s": hard_cap
	})
	_request_timeout_timer.start(_timeout_for_path(_inflight_path))


func _on_request_timeout() -> void:
	# 同样靠生成号识别"迟到的"超时回调：如果它不属于当前这一代请求，说明
	# `_busy` 早被别的更新的请求（或一次显式 reset/interrupt）重新占用了，
	# 这里绝不能再去碰它。
	if not _busy or _timeout_generation != _request_generation:
		return
	var timed_out_path := _inflight_path
	var timed_out_session_id := _inflight_session_id
	FrontendLogger.error(editor_interface, "HTTP", "Request timed out; unblocking queue.", {
		"path": timed_out_path,
		"timeout_s": _timeout_for_path(timed_out_path),
		"queue_size": _queue.size()
	})
	_request_generation += 1
	_replace_chat_http()
	_busy = false
	var interrupt_enqueued := false
	if timed_out_path == "/chat":
		# 前端单方面放弃等待并不会让后端的 agent 循环停下来——它会继续跑完
		# 这一轮工具调用，并持续通过独立的事件轮询通道往外推流，造成"状态栏
		# 已经显示空闲，但某条 Thought 计时还在不停增长"的诡异现象。这里和
		# 用户主动点"停止"一样，显式通知后端取消这个 session 仍在运行的请求，
		# 并临时丢弃它后续迟到的事件，直到下一条用户消息重新开始一轮。
		current_turn_id = ""
		_suppress_events = true
		_enqueue("POST", "/chat/interrupt", {
			"session_id": timed_out_session_id if timed_out_session_id != "" else _session_id()
		})
		interrupt_enqueued = true
	error_occurred.emit("HTTP request timed out: " + timed_out_path)
	if not interrupt_enqueued:
		_pump()


func _on_request_completed(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	# `HTTPRequest.cancel_request()` 并不保证一定会触发 `request_completed`
	# （取决于内部连接处于哪个阶段），所以 `interrupt_current()` 在调用
	# cancel 后会立即把 `_busy` 复位、不等待这个信号。这里用生成号识别
	# "迟到的、属于已取消请求的" 完成回调：如果它不属于当前生成，说明
	# `_busy` 早已被别的（更新的）请求重新占用，绝不能再碰它，否则会把
	# 正在进行中的新请求状态错误地复位掉。
	var completed_generation := _inflight_generation
	if completed_generation != _request_generation:
		FrontendLogger.info(editor_interface, "HTTP", "Ignoring stale request completion.", {
			"completed_generation": completed_generation,
			"current_generation": _request_generation
		})
		return
	_request_timeout_timer.stop()
	_busy = false
	var text := body.get_string_from_utf8()
	if result != HTTPRequest.RESULT_SUCCESS or code < 200 or code >= 300:
		if _suppress_events:
			FrontendLogger.info(editor_interface, "HTTP", "Suppressed HTTP failure after interrupt.", {
				"code": code,
				"result": result
			})
			_pump()
			return
		# 后端对命令/记忆的参数错误改用 4xx + 结构化 body（含 ok/text）返回：HTTP
		# 状态码语义正确，正文仍保留可读消息。这类"客户端错误"应按业务响应分发、
		# 展示 text，而不是退化成一条 "HTTP 400" 传输错误。仅限传输成功 + 4xx +
		# 含 ok 字段的字典；5xx、传输失败、非 JSON 仍按错误处理。
		if result == HTTPRequest.RESULT_SUCCESS and code >= 400 and code < 500:
			var structured: Variant = JSON.parse_string(text)
			if structured is Dictionary and structured.has("ok"):
				FrontendLogger.info(editor_interface, "HTTP", "Structured client-error response.", {
					"code": code,
					"path": _inflight_path
				})
				response_received.emit(structured)
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
		if response.has("cancelled") and response.has("last_event_seq"):
			# `/chat/interrupt` 的确认：跳过中断前后后端可能残留写入的旧事件，
			# 不把这条纯内部 ack 转发给 ChatPanel。
			if _inflight_session_id == _session_id():
				_last_event_seq = max(_last_event_seq, int(response.get("last_event_seq", 0)))
			FrontendLogger.info(editor_interface, "HTTP", "Interrupt acknowledged by backend.", {
				"cancelled": response.get("cancelled", false),
				"last_event_seq": _last_event_seq,
				"path": _inflight_path,
				"session_id": _inflight_session_id
			})
			_pump()
			return
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
		var raw_events: Variant = parsed.get("events", [])
		if not (raw_events is Array):
			FrontendLogger.warn(editor_interface, "HTTP", "Ignored malformed events response.", {})
			return
		var events: Array = raw_events
		if not events.is_empty():
			FrontendLogger.debug(editor_interface, "HTTP", "Received events.", {"count": events.size()})
			_maybe_extend_request_timeout()
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
