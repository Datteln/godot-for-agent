@tool
extends Node

signal event_received(event: Dictionary)
signal connection_state_changed(state: String)
signal protocol_error_received(details: Dictionary)
signal history_gap_received(details: Dictionary)
signal resync_required_received(details: Dictionary)

const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

const PROTOCOL_VERSION := 1
const STATE_DISCONNECTED := "disconnected"
const STATE_CONNECTING := "connecting"
const STATE_SUBSCRIBED := "subscribed"
const STATE_RECONNECTING := "reconnecting"
const STATE_STOPPED := "stopped"
const RECONNECT_MAX_DELAY_S := 20.0

var editor_interface: EditorInterface
var service: Node

var socket_factory: Callable
var _socket = WebSocketPeer.new()
var _state := STATE_DISCONNECTED
var _session_id := ""
var _highest_contiguous_seq := 0
var _seen_event_ids: Dictionary = {}
var _reconnect_delay_s := 1.0
var _next_reconnect_at_msec := 0
var _stopped := true
var _awaiting_history_hydration := false
var _subscribe_sent := false

# 脱敏传输诊断（任务 1.1）：只记录序号、计数、字节数与时间戳，不含正文。
var _diag_events_received := 0
var _diag_bytes_received := 0
var _diag_acks_sent := 0
var _diag_last_received_seq := 0
var _diag_last_received_msec := 0
var _diag_last_ack_msec := 0
var _diag_resync_count := 0
var _diag_gap_count := 0


func start(session_id: String, after_seq: int = 0) -> void:
	# 事件序号按会话独立计数：切换到不同会话时必须清零并清空去重表，
	# 否则旧会话遗留的高位游标会把新会话的低序号事件全部误丢弃。
	if session_id != _session_id:
		_highest_contiguous_seq = 0
		_seen_event_ids.clear()
		_reconnect_delay_s = 1.0
	_session_id = session_id
	_highest_contiguous_seq = max(_highest_contiguous_seq, after_seq)
	_stopped = false
	_awaiting_history_hydration = false
	_subscribe_sent = false
	_connect()


func stop() -> void:
	_stopped = true
	_awaiting_history_hydration = false
	if _socket.get_ready_state() != WebSocketPeer.STATE_CLOSED:
		_socket.close()
	_set_state(STATE_STOPPED)


func switch_session(session_id: String) -> void:
	if _session_id == session_id:
		return
	if _socket.get_ready_state() != WebSocketPeer.STATE_CLOSED:
		_socket.close()
	_session_id = session_id
	_highest_contiguous_seq = 0
	_seen_event_ids.clear()
	_reconnect_delay_s = 1.0
	_awaiting_history_hydration = false
	_subscribe_sent = false
	_stopped = false
	_connect()


func hydrate_from_history(last_event_seq: int) -> void:
	_highest_contiguous_seq = max(0, last_event_seq)
	_seen_event_ids.clear()
	_awaiting_history_hydration = false
	if not _stopped and _socket.get_ready_state() == WebSocketPeer.STATE_CLOSED:
		_connect()


func resume_from_pointer(pointer: Dictionary) -> void:
	var session_id := str(pointer.get("session_id", _session_id))
	if session_id != _session_id:
		switch_session(session_id)
	_highest_contiguous_seq = max(_highest_contiguous_seq, int(pointer.get("last_event_seq", 0)))


func _process(_delta: float) -> void:
	if _stopped:
		return
	_socket.poll()
	var ready_state := _socket.get_ready_state()
	if ready_state == WebSocketPeer.STATE_OPEN:
		if not _subscribe_sent:
			_subscribe_sent = true
			_send({
				"version": PROTOCOL_VERSION,
				"type": "subscribe",
				"session_id": _session_id,
				"after_seq": _highest_contiguous_seq
			})
		_read_packets()
		return
	if ready_state == WebSocketPeer.STATE_CLOSED and not _awaiting_history_hydration:
		_schedule_reconnect()
		if Time.get_ticks_msec() >= _next_reconnect_at_msec:
			_connect()


func _connect() -> void:
	if _stopped or _awaiting_history_hydration or _session_id.strip_edges().is_empty():
		return
	_socket = _new_socket()
	_subscribe_sent = false
	_socket.handshake_headers = _headers()
	var error: int = _socket.connect_to_url(_websocket_url(), null)
	if error != OK:
		FrontendLogger.warn(editor_interface, "EventSocket", "WebSocket connection failed.", {"error": error})
		_schedule_reconnect()
		return
	_set_state(STATE_RECONNECTING if _reconnect_delay_s > 1.0 else STATE_CONNECTING)


func _read_packets() -> void:
	while _socket.get_available_packet_count() > 0:
		var packet: PackedByteArray = _socket.get_packet()
		_diag_bytes_received += packet.size()
		var parsed: Variant = JSON.parse_string(packet.get_string_from_utf8())
		if not (parsed is Dictionary):
			_emit_protocol_error({"code": "invalid_server_message"})
			continue
		_handle_message(parsed)


func _handle_message(message: Dictionary) -> void:
	if int(message.get("version", 0)) != PROTOCOL_VERSION:
		_emit_protocol_error({"code": "unsupported_version", "message": message})
		return
	match str(message.get("type", "")):
		"subscribed":
			_reconnect_delay_s = 1.0
			_set_state(STATE_SUBSCRIBED)
		"event":
			_handle_event(message.get("event", {}))
		"heartbeat":
			_send({"version": PROTOCOL_VERSION, "type": "heartbeat"})
		"protocol_error":
			_emit_protocol_error(message)
		"history_gap":
			_begin_history_hydration(message, true)
		"resync_required":
			_begin_history_hydration(message, false)
		_:
			_emit_protocol_error({"code": "unknown_server_message", "message": message})


func _handle_event(raw_event: Variant) -> void:
	if not (raw_event is Dictionary):
		_emit_protocol_error({"code": "invalid_event"})
		return
	var event: Dictionary = raw_event
	var event_id := str(event.get("event_id", ""))
	var seq := int(event.get("seq", 0))
	if event_id.is_empty() or seq <= 0:
		_emit_protocol_error({"code": "invalid_event_identity", "event": event})
		return
	if _seen_event_ids.has(event_id) or seq <= _highest_contiguous_seq:
		return
	if seq != _highest_contiguous_seq + 1:
		_diag_gap_count += 1
		_begin_history_hydration({
			"type": "history_gap",
			"session_id": _session_id,
			"after_seq": _highest_contiguous_seq,
			"received_seq": seq
		}, true)
		return
	_seen_event_ids[event_id] = true
	_highest_contiguous_seq = seq
	_diag_events_received += 1
	_diag_last_received_seq = seq
	_diag_last_received_msec = Time.get_ticks_msec()
	_send({"version": PROTOCOL_VERSION, "type": "ack", "seq": _highest_contiguous_seq})
	_diag_acks_sent += 1
	_diag_last_ack_msec = Time.get_ticks_msec()
	event_received.emit(event)


func _begin_history_hydration(details: Dictionary, is_gap: bool) -> void:
	if _awaiting_history_hydration:
		return
	_awaiting_history_hydration = true
	if _socket.get_ready_state() != WebSocketPeer.STATE_CLOSED:
		_socket.close()
	_set_state(STATE_DISCONNECTED)
	if is_gap:
		history_gap_received.emit(details)
	else:
		_diag_resync_count += 1
		resync_required_received.emit(details)


## 空闲恢复入口（任务 4.2）：从已确认游标强制重连并重新订阅。
##
## 不清零 `_highest_contiguous_seq`——恢复的语义正是“从客户端已连续确认的
## 位置续订”，服务端重放该游标之后的事件。重置重连退避以便立刻尝试。
func recover_from_acknowledged_cursor() -> void:
	if _stopped or _session_id.strip_edges().is_empty():
		return
	_awaiting_history_hydration = false
	if _socket.get_ready_state() != WebSocketPeer.STATE_CLOSED:
		_socket.close()
	_reconnect_delay_s = 1.0
	_next_reconnect_at_msec = 0
	_connect()


## 返回脱敏传输诊断快照（任务 1.1）；只含序号/计数/字节/时间戳。
func transport_diagnostics() -> Dictionary:
	return {
		"state": _state,
		"highest_contiguous_seq": _highest_contiguous_seq,
		"events_received": _diag_events_received,
		"bytes_received": _diag_bytes_received,
		"acks_sent": _diag_acks_sent,
		"last_received_seq": _diag_last_received_seq,
		"last_received_msec": _diag_last_received_msec,
		"last_ack_msec": _diag_last_ack_msec,
		"resync_count": _diag_resync_count,
		"gap_count": _diag_gap_count,
	}


func _schedule_reconnect() -> void:
	if _next_reconnect_at_msec > Time.get_ticks_msec():
		return
	_set_state(STATE_RECONNECTING)
	_next_reconnect_at_msec = Time.get_ticks_msec() + int(_reconnect_delay_s * 1000.0)
	_reconnect_delay_s = min(RECONNECT_MAX_DELAY_S, _reconnect_delay_s * 2.0)


func _send(message: Dictionary) -> void:
	if _socket.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	_socket.send_text(JSON.stringify(message))


func _new_socket():
	if socket_factory.is_valid():
		return socket_factory.call()
	return WebSocketPeer.new()


func _websocket_url() -> String:
	var base_url := str(service.base_url) if service != null else ""
	if base_url.begins_with("https://"):
		base_url = "wss://" + base_url.substr("https://".length())
	elif base_url.begins_with("http://"):
		base_url = "ws://" + base_url.substr("http://".length())
	while base_url.ends_with("/"):
		base_url = base_url.substr(0, base_url.length() - 1)
	return base_url + "/chat/events/ws"


func _headers() -> PackedStringArray:
	var headers := PackedStringArray()
	if service != null and str(service.token) != "":
		headers.append("Authorization: Bearer " + str(service.token))
	return headers


func _set_state(value: String) -> void:
	if _state == value:
		return
	_state = value
	connection_state_changed.emit(value)


func _emit_protocol_error(details: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "EventSocket", "Protocol error.", details)
	protocol_error_received.emit(details)
