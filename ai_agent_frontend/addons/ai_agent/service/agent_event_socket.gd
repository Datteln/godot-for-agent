@tool
extends Node

signal events_received(events: Array)
signal snapshot_required(problem: Dictionary)
signal epoch_changed(previous_epoch: String, new_epoch: String, last_event_seq: int)
signal socket_state_changed(state: String)
signal error_occurred(message: String)
signal application_progress(event: Dictionary)

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const ChatEventAcceptor = preload("res://addons/ai_agent/state/chat_event_acceptor.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

var editor_interface: EditorInterface
var service: Node
var session_state: RefCounted

var _peer: WebSocketPeer
var _acceptor := ChatEventAcceptor.new()
var _session_id := ""
var _manual_close := false
var _resume_sent := false
var _reconnect_attempt := 0
var _next_reconnect_at_msec := 0
var _last_packet_at_msec := 0


func _ready() -> void:
	set_process(true)


func configure(state: RefCounted) -> void:
	session_state = state
	_acceptor.state = state


func connect_stream() -> void:
	if session_state == null:
		return
	var snapshot: Dictionary = session_state.snapshot()
	_session_id = str(snapshot.get("session_id", ""))
	if _session_id.is_empty() or str(snapshot.get("session_epoch", "")).is_empty():
		return
	if _peer != null and _peer.get_ready_state() in [
		WebSocketPeer.STATE_CONNECTING,
		WebSocketPeer.STATE_OPEN,
	]:
		return
	_manual_close = false
	_open()


func close_stream() -> void:
	_manual_close = true
	_next_reconnect_at_msec = 0
	if _peer != null:
		_peer.close(1000, "client_close")
	_peer = null
	_resume_sent = false
	_emit_socket_state("closed")


func reconnect_from_state() -> void:
	close_stream()
	_manual_close = false
	_reconnect_attempt = 0
	connect_stream()


func _process(_delta: float) -> void:
	if _peer == null:
		if not _manual_close and _next_reconnect_at_msec > 0 and Time.get_ticks_msec() >= _next_reconnect_at_msec:
			_open()
		return
	_peer.poll()
	var ready := _peer.get_ready_state()
	if ready == WebSocketPeer.STATE_OPEN:
		if not _resume_sent:
			_send_resume()
		while _peer.get_available_packet_count() > 0:
			_receive_packet()
		var heartbeat_timeout_ms := int(_setting("ai_agent/ws_heartbeat_timeout_sec", 45.0) * 1000.0)
		if _last_packet_at_msec > 0 and Time.get_ticks_msec() - _last_packet_at_msec > heartbeat_timeout_ms:
			FrontendLogger.warn(editor_interface, "WebSocket", "Heartbeat timeout.")
			_peer.close(4000, "heartbeat_timeout")
	elif ready == WebSocketPeer.STATE_CLOSED:
		_peer = null
		_resume_sent = false
		if not _manual_close:
			_schedule_reconnect()


func _open() -> void:
	_peer = WebSocketPeer.new()
	var token := str(service.token) if service != null else ""
	_peer.handshake_headers = PackedStringArray(["Authorization: Bearer " + token])
	var error := _peer.connect_to_url(_socket_url())
	if error != OK:
		_peer = null
		error_occurred.emit("ws_connect_failed:" + str(error))
		_schedule_reconnect()
		return
	_last_packet_at_msec = Time.get_ticks_msec()
	_emit_socket_state("connecting")


func _send_resume() -> void:
	var snapshot: Dictionary = session_state.snapshot()
	_send({
		"type": "resume",
		"protocol_version": 1,
		"session_id": str(snapshot.get("session_id", "")),
		"session_epoch": str(snapshot.get("session_epoch", "")),
		"after_seq": int(snapshot.get("acknowledged_seq", 0)),
	})
	_resume_sent = true
	_emit_socket_state("resuming")


func _receive_packet() -> void:
	var packet := _peer.get_packet()
	_last_packet_at_msec = Time.get_ticks_msec()
	var parsed: Variant = JSON.parse_string(packet.get_string_from_utf8())
	if not (parsed is Dictionary):
		_fail_protocol("invalid_json")
		return
	var message: Dictionary = parsed
	match str(message.get("type", "")):
		"hello":
			_reconnect_attempt = 0
			session_state.set_reconnecting(false)
			_emit_socket_state("open")
		"event_batch":
			_accept_batch(message)
		"ping":
			_send({
				"type": "pong",
				"protocol_version": 1,
				"nonce": str(message.get("nonce", "")),
			})
		"epoch_changed":
			epoch_changed.emit(
				str(message.get("previous_epoch", "")),
				str(message.get("new_epoch", "")),
				int(message.get("last_event_seq", 0))
			)
			close_stream()
		"snapshot_required":
			snapshot_required.emit(message)
			close_stream()
		"close":
			var retryable := bool(message.get("retryable", false))
			if not retryable:
				_manual_close = true
			error_occurred.emit("ws_closed:" + str(message.get("code", "unknown")))
			_peer.close(4000, str(message.get("code", "closed")))
		_:
			_fail_protocol("unknown_message")


func _accept_batch(message: Dictionary) -> void:
	var result := _acceptor.accept_batch(message)
	if not bool(result.get("accepted", false)):
		if bool(result.get("snapshot_required", false)):
			snapshot_required.emit(result)
		close_stream()
		return
	var events: Array = result.get("events", [])
	for event in events:
		if event is Dictionary and str(event.get("type", "")) == "turn_progress":
			application_progress.emit(event)
	if not events.is_empty():
		events_received.emit(events)
	var accepted_seq := int(result.get("accepted_seq", 0))
	if session_state.acknowledge(accepted_seq):
		_send({
			"type": "ack",
			"protocol_version": 1,
			"session_epoch": str(session_state.snapshot().get("session_epoch", "")),
			"accepted_seq": accepted_seq,
		})


func _send(message: Dictionary) -> void:
	if _peer == null or _peer.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	_peer.send_text(JSON.stringify(message))


func _fail_protocol(reason: String) -> void:
	FrontendLogger.error(editor_interface, "WebSocket", "Protocol error.", {"reason": reason})
	snapshot_required.emit({"reason": reason})
	close_stream()


func _schedule_reconnect() -> void:
	var maximum := int(_setting("ai_agent/ws_reconnect_max_attempts", 8.0))
	if _reconnect_attempt >= maximum:
		_manual_close = true
		session_state.set_reconnecting(false)
		error_occurred.emit("ws_reconnect_exhausted")
		return
	_reconnect_attempt += 1
	session_state.set_reconnecting(true)
	var initial := _setting("ai_agent/ws_reconnect_initial_sec", 0.25)
	var cap := _setting("ai_agent/ws_reconnect_max_sec", 10.0)
	var delay := min(cap, initial * pow(2.0, float(_reconnect_attempt - 1)))
	var jitter := randf_range(0.75, 1.25)
	_next_reconnect_at_msec = Time.get_ticks_msec() + int(delay * jitter * 1000.0)
	_emit_socket_state("reconnecting")


func _socket_url() -> String:
	var root := str(service.base_url) if service != null else str(_setting_value("ai_agent/service_url"))
	while root.ends_with("/"):
		root = root.substr(0, root.length() - 1)
	if root.begins_with("https://"):
		root = "wss://" + root.substr(8)
	elif root.begins_with("http://"):
		root = "ws://" + root.substr(7)
	return root + "/chat/socket"


func _setting(key: String, fallback: float) -> float:
	var value := _setting_value(key)
	return float(value) if value is int or value is float else fallback


func _setting_value(key: String) -> Variant:
	if editor_interface == null:
		return null
	return ConfigMigrations.get_value(editor_interface, key)


func _emit_socket_state(value: String) -> void:
	socket_state_changed.emit(value)
