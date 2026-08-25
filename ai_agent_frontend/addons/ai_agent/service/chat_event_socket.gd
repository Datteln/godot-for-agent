@tool
extends Node

signal event_received(event: Dictionary)
signal connection_state_changed(state: String)
signal protocol_error_received(details: Dictionary)
signal history_gap_received(details: Dictionary)
signal resync_required_received(details: Dictionary)
## 客户端检测到序列缺口（期望连续序号未到达）；恢复路由由外部状态机决定。
signal sequence_gap_detected(details: Dictionary)
## 客户端在解析前拒收了一个超尺寸报文（任务 5.3）；只携带字节数等脱敏
## 诊断，绝不含报文正文，恢复路由由外部状态机决定。
signal oversized_packet_rejected(details: Dictionary)

const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

const PROTOCOL_VERSION := 1
const STATE_DISCONNECTED := "disconnected"
const STATE_CONNECTING := "connecting"
const STATE_SUBSCRIBED := "subscribed"
const STATE_RECONNECTING := "reconnecting"
const STATE_STOPPED := "stopped"
const RECONNECT_MAX_DELAY_S := 20.0
## 入站报文尺寸守卫（任务 5.3）：在 `JSON.parse_string` 之前拒收超尺寸报文，
## 避免主线程为异常载荷分配字符串/字典而帧饥饿。阈值只需显著大于任何合法
## 实时补丁（服务端终态补丁预算远低于此），不追求贴近上限。
const MAX_INBOUND_PACKET_BYTES := 256 * 1024

var editor_interface: EditorInterface
var service: Node

var socket_factory: Callable
var _socket = WebSocketPeer.new()
var _state := STATE_DISCONNECTED
var _session_id := ""
var _highest_contiguous_seq := 0
## 已提交游标（任务 2.12）：对应事件已被权威 Store 与视口接受呈现。
## 只有它可以作为 ACK 与重连订阅的 `after_seq`；接收游标绝不能充当。
var _committed_seq := 0
## 已发出 ACK 的最高提交游标，避免重复发送同一序号的确认。
var _last_acked_seq := 0
## 乱序提交的暂存（任务 2.12）：提交按序号连续推进，先到的高序号在此等待。
var _pending_commits := {}
## 单订阅内未提交暂存的防御上限；超出说明提交链路异常，交给恢复状态机。
const MAX_PENDING_COMMITS := 4096
var _seen_event_ids: Dictionary = {}
var _reconnect_delay_s := 1.0
var _next_reconnect_at_msec := 0
var _stopped := true
var _awaiting_history_hydration := false
var _subscribe_sent := false

# 服务端可见进度字段（任务 1.2）：只含序号与时间戳，无正文。
var _server_last_seq := 0
var _server_visible_seq := 0
var _server_visible_updated_at := 0.0
var _diag_last_heartbeat_msec := 0
## 最近一次收到任何服务端报文的时间（任务 2.10 半开连接新鲜度检测）。
var _last_packet_msec := 0

# 脱敏传输诊断（任务 1.1）：只记录序号、计数、字节数与时间戳，不含正文。
var _diag_events_received := 0
var _diag_bytes_received := 0
var _diag_acks_sent := 0
var _diag_last_received_seq := 0
var _diag_last_received_msec := 0
var _diag_last_ack_msec := 0
var _diag_resync_count := 0
var _diag_gap_count := 0
## 入站超尺寸报文拒收计数（任务 5.3 脱敏诊断：只有计数，没有正文）。
var _diag_oversized_packets := 0


func start(session_id: String, after_seq: int = 0) -> void:
	# 事件序号按会话独立计数：切换到不同会话时必须清零并清空去重表，
	# 否则旧会话遗留的高位游标会把新会话的低序号事件全部误丢弃。
	if session_id != _session_id:
		_highest_contiguous_seq = 0
		_committed_seq = 0
		_last_acked_seq = 0
		_pending_commits.clear()
		_seen_event_ids.clear()
		_reconnect_delay_s = 1.0
	_session_id = session_id
	_highest_contiguous_seq = max(_highest_contiguous_seq, after_seq)
	# 快照/续订游标之前的条目已被水合呈现确认：提交游标同步抬升，
	# 之后的订阅与确认都从该游标开始（任务 2.12）。
	_committed_seq = max(_committed_seq, after_seq)
	_last_acked_seq = max(_last_acked_seq, _committed_seq)
	# 快照/续订游标之前的可见状态已被水合确认，同步抬升服务端可见水位基线。
	_server_visible_seq = maxi(_server_visible_seq, _highest_contiguous_seq)
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
	_committed_seq = 0
	_last_acked_seq = 0
	_pending_commits.clear()
	_seen_event_ids.clear()
	_reconnect_delay_s = 1.0
	_awaiting_history_hydration = false
	_subscribe_sent = false
	_stopped = false
	_connect()


func hydrate_from_history(last_event_seq: int) -> void:
	_highest_contiguous_seq = max(0, last_event_seq)
	_committed_seq = max(_committed_seq, _highest_contiguous_seq)
	_last_acked_seq = max(_last_acked_seq, _committed_seq)
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
			# 从已提交游标订阅（任务 2.12）：接收游标只证明报文到达，
			# 未呈现的事件必须可被服务端重放，不能作为续订起点。
			_send({
				"version": PROTOCOL_VERSION,
				"type": "subscribe",
				"session_id": _session_id,
				"after_seq": _committed_seq
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
	# 新连接可能从已提交游标重放未呈现事件：清空连接级去重表，
	# 让重放事件重新参与投影与提交（Store 层按 event_id 幂等）。
	_seen_event_ids.clear()
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
		_last_packet_msec = Time.get_ticks_msec()
		# 入站尺寸守卫（任务 5.3）：在 JSON 解析前拒收超尺寸报文，绝不在
		# 主线程为其分配字符串/字典；诊断只记录字节数，恢复走既有状态机。
		if packet.size() > MAX_INBOUND_PACKET_BYTES:
			_reject_oversized_packet(packet.size())
			return
		var parsed: Variant = JSON.parse_string(packet.get_string_from_utf8())
		if not (parsed is Dictionary):
			_emit_protocol_error({"code": "invalid_server_message"})
			continue
		_handle_message(parsed)


## 拒收超尺寸报文（任务 5.3）：关闭当前连接并把脱敏尺寸诊断交给恢复状态机。
## 不推进任何游标、不提交、不解析正文；重连后从已提交游标重放，无法闭合时
## 由状态机升级为权威快照水合（快照条目已在服务端净化）。
func _reject_oversized_packet(packet_bytes: int) -> void:
	_diag_oversized_packets += 1
	var details := {
		"session_id": _session_id,
		"packet_bytes": packet_bytes,
		"threshold_bytes": MAX_INBOUND_PACKET_BYTES,
		"committed_seq": _committed_seq,
	}
	FrontendLogger.warn(editor_interface, "EventSocket", "Rejected oversized inbound packet before parsing.", details)
	if _socket.get_ready_state() != WebSocketPeer.STATE_CLOSED:
		_socket.close()
	_set_state(STATE_DISCONNECTED)
	oversized_packet_rejected.emit(details)


func _handle_message(message: Dictionary) -> void:
	if int(message.get("version", 0)) != PROTOCOL_VERSION:
		_emit_protocol_error({"code": "unsupported_version", "message": message})
		return
	match str(message.get("type", "")):
		"subscribed":
			_reconnect_delay_s = 1.0
			_read_server_progress(message)
			_set_state(STATE_SUBSCRIBED)
		"event":
			_handle_event(message.get("event", {}))
		"heartbeat":
			_read_server_progress(message)
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
	if seq <= _committed_seq:
		# 已提交（已呈现并确认）的事件：重放重复送达直接忽略。
		return
	if seq <= _highest_contiguous_seq:
		# 已接收但未提交的事件（续传重放送达）：重新交给投影链路，
		# 由 Store 幂等去重；连接级去重表防止同一连接内重复分发。
		if not _seen_event_ids.has(event_id):
			_seen_event_ids[event_id] = true
			event_received.emit(event)
		return
	if seq != _highest_contiguous_seq + 1:
		_diag_gap_count += 1
		# 序列缺口（任务 2.2）：不推进连续游标、不确认该序号；断开连接并把
		# 脱敏诊断交给恢复状态机，由它决定续传还是快照水合（任务 2.4）。
		var gap_details := {
			"session_id": _session_id,
			"expected_seq": _highest_contiguous_seq + 1,
			"received_seq": seq,
			"event_id": event_id,
		}
		if _socket.get_ready_state() != WebSocketPeer.STATE_CLOSED:
			_socket.close()
		_set_state(STATE_DISCONNECTED)
		sequence_gap_detected.emit(gap_details)
		return
	# 接收确认与呈现提交分离（任务 2.12）：这里只推进接收水位并分发事件，
	# 绝不发送 ACK——ACK 只能来自提交游标（`commit_seq`）。
	_seen_event_ids[event_id] = true
	_highest_contiguous_seq = seq
	_diag_events_received += 1
	_diag_last_received_seq = seq
	_diag_last_received_msec = Time.get_ticks_msec()
	event_received.emit(event)


## 呈现提交（任务 2.12）：调用方在事件对应修订被权威 Store 与视口接受后
## 提交其序号；提交按连续序号推进，推进多少就确认（ACK）多少。
##
## 乱序提交先暂存于 `_pending_commits`；只有从 `_committed_seq + 1` 起连续
## 的部分才会真正推进游标。接收但未提交的事件绝不能被 ACK。
func commit_seq(seq: int) -> void:
	if _stopped or seq <= 0 or seq <= _committed_seq:
		return
	_pending_commits[seq] = true
	if _pending_commits.size() > MAX_PENDING_COMMITS:
		# 提交链路异常的防御上限：清空暂存让游标停在原地，
		# 停滞看门狗会经由恢复状态机重建同步（诊断不含正文）。
		_pending_commits.clear()
		FrontendLogger.warn(editor_interface, "EventSocket", "Pending commit backlog exceeded bound; holding committed cursor.", {
			"committed_seq": _committed_seq,
			"received_seq": _highest_contiguous_seq,
		})
		return
	while _pending_commits.has(_committed_seq + 1):
		_committed_seq += 1
		_pending_commits.erase(_committed_seq)
	_ack_committed()


## 向服务端确认已提交游标（只在游标前进且连接可用时发送）。
func _ack_committed() -> void:
	if _committed_seq <= _last_acked_seq:
		return
	if _socket.get_ready_state() != WebSocketPeer.STATE_OPEN:
		return
	_send({"version": PROTOCOL_VERSION, "type": "ack", "seq": _committed_seq})
	_last_acked_seq = _committed_seq
	_diag_acks_sent += 1
	_diag_last_ack_msec = Time.get_ticks_msec()


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
## 不清零 `_committed_seq`——恢复的语义正是“从客户端已提交（已呈现）的
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


## 读取心跳/订阅确认携带的可见进度字段（任务 1.2）；只含序号与时间戳。
func _read_server_progress(message: Dictionary) -> void:
	_server_last_seq = int(message.get("last_seq", _server_last_seq))
	_server_visible_seq = int(message.get("visible_seq", _server_visible_seq))
	_server_visible_updated_at = float(message.get("visible_updated_at", _server_visible_updated_at))
	_diag_last_heartbeat_msec = Time.get_ticks_msec()


## 客户端已连续接收的最高序号（接收水位；不再用于 ACK 或续订）。
func highest_contiguous_seq() -> int:
	return _highest_contiguous_seq


## 客户端已提交（已呈现）的最高连续序号（任务 2.12）：
## 唯一可作为 ACK 与续订 `after_seq` 的游标。
func committed_seq() -> int:
	return _committed_seq


## 当前是否处于已订阅状态（任务 2.10 半开连接新鲜度检测）。
func is_subscribed() -> bool:
	return _state == STATE_SUBSCRIBED


## 最近一次收到服务端报文的时间戳（毫秒；0 表示本连接尚未收到任何报文）。
func last_packet_msec() -> int:
	return _last_packet_msec


## 服务端可见进度的脱敏视图（任务 1.2 / 2.3 停滞检测输入）。
func server_progress() -> Dictionary:
	return {
		"last_seq": _server_last_seq,
		"visible_seq": _server_visible_seq,
		"visible_updated_at": _server_visible_updated_at,
		"last_heartbeat_msec": _diag_last_heartbeat_msec,
	}


## 返回脱敏传输诊断快照（任务 1.1 / 2.12）；只含序号/计数/字节/时间戳。
func transport_diagnostics() -> Dictionary:
	return {
		"state": _state,
		"highest_contiguous_seq": _highest_contiguous_seq,
		"received_seq": _highest_contiguous_seq,
		"committed_seq": _committed_seq,
		"last_acked_seq": _last_acked_seq,
		"pending_commits": _pending_commits.size(),
		"events_received": _diag_events_received,
		"bytes_received": _diag_bytes_received,
		"acks_sent": _diag_acks_sent,
		"last_received_seq": _diag_last_received_seq,
		"last_received_msec": _diag_last_received_msec,
		"last_ack_msec": _diag_last_ack_msec,
		"last_packet_msec": _last_packet_msec,
		"resync_count": _diag_resync_count,
		"gap_count": _diag_gap_count,
		"oversized_packets": _diag_oversized_packets,
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
