## 聊天事件 socket 契约测试（fix-transcript-sync-recovery 更新；
## fix-transcript-delivery-semantics 任务 3.1）。
##
## 覆盖：事件去重、接收与提交分离后的 ack 契约（任务 2.12：ACK 只能来自
## 已提交游标，接收不确认）、心跳/订阅确认携带的服务端可见进度字段（任务 1.2）、
## 序列缺口不再直接水合而是发出脱敏诊断并交给恢复状态机（任务 2.2/2.4）、
## 恢复路径不重发任何命令（任务 2.5）、上一连接已接收但未提交的事件在
## 重连重放时被重新投递（任务 1.1 / 3.1）。
extends SceneTree

const ChatEventSocket = preload("res://addons/ai_agent/service/chat_event_socket.gd")
const TranscriptStore = preload("res://addons/ai_agent/transcript/transcript_store.gd")
const TranscriptProjector = preload("res://addons/ai_agent/transcript/transcript_projector.gd")

var _failures := 0
var _checks := 0


class FakeSocket:
	var sent: Array[String] = []
	var closed := false

	func get_ready_state() -> int:
		return WebSocketPeer.STATE_OPEN

	func send_text(value: String) -> int:
		sent.append(value)
		return OK

	func close() -> void:
		closed = true


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _init() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "s1"
	client._stopped = false
	var received: Array[Dictionary] = []
	var gaps: Array[Dictionary] = []
	client.event_received.connect(func(event: Dictionary): received.append(event))
	client.sequence_gap_detected.connect(func(details: Dictionary): gaps.append(details))

	# 订阅确认携带可见进度基线字段（任务 1.2）。
	client._handle_message({"version": 1, "type": "subscribed", "last_seq": 0, "visible_seq": 0, "visible_updated_at": 0.0})
	_check(client._state == client.STATE_SUBSCRIBED, "subscribed state")

	client._handle_event({"event_id": "s1:1", "seq": 1, "type": "text", "payload": {}})
	client._handle_event({"event_id": "s1:1", "seq": 1, "type": "text", "payload": {}})
	_check(received.size() == 1, "duplicate event dedup")
	_check(client._highest_contiguous_seq == 1, "contiguous cursor after seq 1")
	# 接收与提交分离（任务 2.12）：接收推进接收水位但不发送任何 ACK。
	_check(fake.sent.size() == 0, "no ack before presentation commit")
	_check(client.committed_seq() == 0, "committed cursor stays at 0 before commit")
	client.commit_seq(1)
	_check(client.committed_seq() == 1, "commit advances committed cursor")
	_check(fake.sent.size() == 1, "one ack sent after commit")
	var ack = JSON.parse_string(fake.sent[0])
	_check(ack is Dictionary and ack.get("type") == "ack" and int(ack.get("seq")) == 1, "ack seq comes from committed cursor")
	client.commit_seq(1)
	_check(fake.sent.size() == 1, "duplicate commit does not re-ack")

	# 心跳携带服务端可见进度：客户端只记录序号/时间戳，不视为可见进度。
	client._handle_message({"version": 1, "type": "heartbeat", "last_seq": 5, "visible_seq": 4, "visible_updated_at": 123.5})
	_check(int(client.server_progress().get("visible_seq")) == 4, "heartbeat visible_seq parsed")
	_check(int(client.server_progress().get("last_seq")) == 5, "heartbeat last_seq parsed")
	_check(client._highest_contiguous_seq == 1, "heartbeat does not advance contiguous cursor")

	# 序列缺口（任务 2.2）：不推进游标、不 ack 后来的序号、关闭连接，
	# 发出脱敏缺口诊断；不再直接进入历史水合（恢复顺序由状态机决定）。
	client._handle_event({"event_id": "s1:3", "seq": 3, "type": "text", "payload": {}})
	_check(gaps.size() == 1, "sequence gap signal emitted")
	if gaps.size() == 1:
		_check(int(gaps[0].get("expected_seq")) == 2, "gap expected_seq")
		_check(int(gaps[0].get("received_seq")) == 3, "gap received_seq")
		_check(str(gaps[0].get("session_id")) == "s1", "gap session_id")
	_check(fake.closed, "socket closed on gap")
	_check(client._highest_contiguous_seq == 1, "gap does not advance contiguous cursor")
	_check(client.committed_seq() == 1, "gap does not advance committed cursor")
	_check(not client._awaiting_history_hydration, "client-side gap does not force hydration")
	_check(fake.sent.all(func(message: String): return not message.contains("user_message")), "no command resubmitted")

	# 续传重放（任务 2.4 / 2.12）：从已提交游标重连后，缺口前后的事件重新送达；
	# 已接收未提交的事件可被再次分发并参与投影。
	client._handle_event({"event_id": "s1:2", "seq": 2, "type": "text", "payload": {}})
	client._handle_event({"event_id": "s1:3", "seq": 3, "type": "text", "payload": {}})
	_check(received.size() == 3, "replay re-delivers missing and pending events")
	_check(client._highest_contiguous_seq == 3, "replay closes the received gap")

	# 乱序提交按连续序号推进：先提交 3，游标不动；补上 2 后一次推进到 3，
	# ACK 只来自提交游标（任务 2.12）。
	client.commit_seq(3)
	_check(client.committed_seq() == 1, "out-of-order commit waits for contiguity")
	client.commit_seq(2)
	_check(client.committed_seq() == 3, "contiguous commit catches up")
	var ack3 = JSON.parse_string(fake.sent[fake.sent.size() - 1])
	_check(ack3 is Dictionary and int(ack3.get("seq")) == 3, "ack follows contiguous commit")

	# 续传入口保持以已提交游标为订阅起点（任务 2.4 / 2.12）。
	_check(client.highest_contiguous_seq() == 3, "received cursor accessor")
	_check(client.committed_seq() == 3, "committed cursor is the resume cursor")

	# 任务 3.1：上一连接已接收但未提交的事件，重连重放时必须重新投递；
	# 进度提交后 Store 保持幂等。
	_test_received_uncommitted_redelivered_after_reconnect()
	quit(1 if _failures > 0 else 0)


## 任务 3.1：事件在上一连接已接收但从未提交（Store/视口未接受）时，
## 重连后服务端从已提交游标重放该事件——客户端必须重新投递它参与投影，
## 而不是仅凭 event_id 曾被接收就丢弃。进度提交后，重放已提交事件被
## 连接级去重抑制，Store 的 event_id/revision 幂等保证不产生重复条目。
func _test_received_uncommitted_redelivered_after_reconnect() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "replay1"
	client._stopped = false
	var received: Array[Dictionary] = []
	client.event_received.connect(func(event: Dictionary): received.append(event))
	client._handle_message({"version": 1, "type": "subscribed", "last_seq": 0, "visible_seq": 0, "visible_updated_at": 0.0})

	# 事件 1、2 到达并被接收，但从未提交（例如投影器/视口尚未接受）。
	client._handle_event({"event_id": "replay1:1", "seq": 1, "type": "transcript_patch", "payload": {}})
	client._handle_event({"event_id": "replay1:2", "seq": 2, "type": "transcript_patch", "payload": {}})
	_check(received.size() == 2, "3.1: events received on first connection")
	_check(client.highest_contiguous_seq() == 2, "3.1: received cursor advanced")
	_check(client.committed_seq() == 0, "3.1: nothing committed before reconnect")

	# 重连：接收簿记重置到已提交游标（任务 1.1），重放纪元递增。
	var epoch_before: int = client._replay_epoch
	client._reset_connection_bookkeeping()
	_check(client._replay_epoch == epoch_before + 1, "3.1: replay epoch advances per connection")
	_check(client.highest_contiguous_seq() == 0, "3.1: receipt cursor reset to committed on reconnect")
	_check(client._seen_event_ids.is_empty(), "3.1: connection-scoped dedup cleared on reconnect")

	# 服务端从 after_seq=0 重放 seq1、seq2：即使上一连接已接收也必须重新投递。
	client._handle_event({"event_id": "replay1:1", "seq": 1, "type": "transcript_patch", "payload": {}})
	client._handle_event({"event_id": "replay1:2", "seq": 2, "type": "transcript_patch", "payload": {}})
	_check(received.size() == 4, "3.1: replayed uncommitted events re-delivered")
	_check(client.highest_contiguous_seq() == 2, "3.1: replay advances receipt cursor again")

	# 进度提交：Store/视口接受后提交游标推进并发送 ACK（任务 1.2 连续提交语义）。
	client.commit_seq(1)
	client.commit_seq(2)
	_check(client.committed_seq() == 2, "3.1: replay commits progress")
	var ack = JSON.parse_string(fake.sent[fake.sent.size() - 1])
	_check(ack is Dictionary and int(ack.get("seq", 0)) == 2, "3.1: ack follows committed progress")
	var diagnostics: Dictionary = client.transport_diagnostics()
	_check(int(diagnostics.get("committed_seq", -1)) == 2, "3.1: diagnostics expose committed cursor")
	_check(int(diagnostics.get("blocking_seq", -1)) == 0, "3.1: no blocking seq after contiguous commit")

	# 已提交事件的重放：连接内不再分发。
	client._handle_event({"event_id": "replay1:1", "seq": 1, "type": "transcript_patch", "payload": {}})
	client._handle_event({"event_id": "replay1:2", "seq": 2, "type": "transcript_patch", "payload": {}})
	_check(received.size() == 4, "3.1: already-committed replay suppressed")

	# Store 幂等：同一事件两次投影只产生一个条目。
	var store = TranscriptStore.new()
	var projector = TranscriptProjector.new(store)
	var generation := projector.begin_hydration("replay1")
	projector.apply_snapshot({
		"transcript": {
			"version": 1, "session_id": "replay1", "upto_event_seq": 0,
			"legacy": false, "entries": [],
		},
	}, generation)
	var entry := {"entry_id": "e1", "ordinal": 1, "kind": "user", "state": "complete", "revision": 1, "payload": {"text": "x"}}
	var patch1 := {"event_id": "replay1:1", "session_id": "replay1", "seq": 1, "type": "transcript_patch", "payload": {"entry": entry, "stream_key": "e1"}}
	var patch2 := patch1.duplicate(true)
	_check(projector.apply_event(patch1, generation), "3.1: first delivery projects")
	_check(not projector.apply_event(patch2, generation), "3.1: re-delivery deduped by Store")
	_check(store.entry_count() == 1, "3.1: Store remains idempotent")