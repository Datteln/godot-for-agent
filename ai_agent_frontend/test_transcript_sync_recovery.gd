## 转录同步恢复集成测试（fix-transcript-sync-recovery 任务 3.2 / 3.3 / 3.7 / 3.8）。
##
## 回归场景：`read_class_docs` 产出 ClassInfo 之后，长 Thought 流式期间的
## 实时补丁丢失；其后的 bootstrap 审批与工具活动必须最终按序可见——要么由
## 连续游标续传闭合缺口，要么由权威快照兜底。全程不得重发任何命令。
##
## 任务 3.7：接收水位超前投影/渲染水位、投影窗口积压、Open 但静默的连接、
## Reset 与排队工具结果竞争四个回归。
## 任务 3.8：投影失败不得把 ACK 推进到已提交游标之后；从该游标重连后，
## 服务端重放（而非人工历史重载）使缺失条目恰好一次按序可见。
extends SceneTree

const ChatEventSocket = preload("res://addons/ai_agent/service/chat_event_socket.gd")
const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")
const TranscriptStore = preload("res://addons/ai_agent/transcript/transcript_store.gd")
const TranscriptProjector = preload("res://addons/ai_agent/transcript/transcript_projector.gd")
const TranscriptRecovery = preload("res://addons/ai_agent/transcript/transcript_recovery.gd")

var _failures := 0
var _checks := 0


class FakeSocket:
	var sent: Array[String] = []
	var closed := false
	var packets: Array = []

	func get_ready_state() -> int:
		return WebSocketPeer.STATE_OPEN

	func send_text(value: String) -> int:
		sent.append(value)
		return OK

	func close() -> void:
		closed = true

	func poll() -> void:
		pass

	func get_available_packet_count() -> int:
		return packets.size()

	func get_packet() -> PackedByteArray:
		return packets.pop_front()


class FakeService:
	extends Node

	var base_url := "http://127.0.0.1:9"
	var token := ""


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _entry(entry_id: String, ordinal: int, kind: String, state: String, revision: int, payload: Dictionary = {}) -> Dictionary:
	return {
		"entry_id": entry_id,
		"ordinal": ordinal,
		"kind": kind,
		"state": state,
		"revision": revision,
		"turn_id": "t1",
		"tool_call_id": null,
		"payload": payload,
	}


func _patch(session_id: String, seq: int, entry: Dictionary) -> Dictionary:
	return {
		"event_id": "%s:%d" % [session_id, seq],
		"session_id": session_id,
		"seq": seq,
		"type": "transcript_patch",
		"payload": {
			"patch_format": "full",
			"patch_version": 2,
			"entry": entry,
			"stream_key": str(entry.get("entry_id", "")),
		},
	}


func _snapshot(session_id: String, upto_seq: int, entries: Array) -> Dictionary:
	return {
		"transcript": {
			"version": 1,
			"session_id": session_id,
			"upto_event_seq": upto_seq,
			"legacy": false,
			"entries": entries,
		},
	}


func _init() -> void:
	_test_recovery_bounds()
	_test_gap_resume_closes_gap()
	_test_gap_escalates_to_snapshot()
	_test_server_retention_gap_routes_to_snapshot()
	_test_classinfo_drop_regression_resume()
	_test_classinfo_drop_regression_snapshot()
	_test_min_watermark_stall_detects_lagging_stage()
	_test_projection_backlog_routes_to_recovery()
	_test_silent_socket_probe_routes_recovery()
	_test_reset_barrier_discards_queued_tool_results()
	_test_commit_cursor_regression()
	_test_oversized_packet_guard_and_recovery()
	print("checks=%d failures=%d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


## 恢复状态机的有界性：续传一次、水合两次、预算耗尽后停止。
func _test_recovery_bounds() -> void:
	var r = TranscriptRecovery.new()
	_check(r.begin("sequence_gap", "resume", {}) == "resume", "first action is resume")
	_check(r.state == TranscriptRecovery.State.RESUMING, "state resuming")
	_check(r.begin("sequence_gap", "resume", {}) == "hydrate", "second trigger escalates to hydrate")
	_check(r.state == TranscriptRecovery.State.HYDRATING, "state hydrating")
	_check(r.begin("sequence_gap", "resume", {}) == "", "no action while hydrating")
	var exhausted: Array[Dictionary] = []
	r.recovery_exhausted.connect(func(details: Dictionary): exhausted.append(details))
	_check(r.retry_hydration("timeout", {}) == "hydrate", "hydration retry within budget")
	_check(r.retry_hydration("timeout", {}) == "", "hydration retry exhausted")
	_check(exhausted.size() == 1, "exhausted diagnostic emitted once")
	_check(r.retry_hydration("timeout", {}) == "", "exhausted stays exhausted")
	_check(exhausted.size() == 1, "exhausted not re-reported")
	r.finish("snapshot_hydrated")
	_check(r.state == TranscriptRecovery.State.IDLE, "finish returns to idle")
	_check(r.begin("new_gap", "hydrate", {}) == "hydrate", "budget resets after recovery")
	r.finish("done")
	# 停滞判定：可见进度刷新计时；服务端水位领先才可判定停滞。
	var r2 = TranscriptRecovery.new()
	r2.stall_threshold_s = 0.02
	r2.notify_visible_progress()
	_check(not r2.stalled(Time.get_ticks_msec()), "fresh progress is not stalled")
	r2.notify_server_progress(10)
	_check(r2.server_ahead_of(5), "server ahead of client cursor")
	_check(not r2.server_ahead_of(10), "equal watermark is not ahead")
	OS.delay_msec(30)
	_check(r2.stalled(Time.get_ticks_msec()), "stalled after threshold without progress")


## 序列缺口 → 从连续游标续传 → 重放闭合缺口（任务 2.4）。
func _test_gap_resume_closes_gap() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "s1"
	var gaps: Array[Dictionary] = []
	client.sequence_gap_detected.connect(func(details: Dictionary): gaps.append(details))
	client._handle_message({"version": 1, "type": "subscribed", "last_seq": 3, "visible_seq": 3, "visible_updated_at": 1.0})
	for seq in range(1, 4):
		client._handle_event(_patch("s1", seq, _entry("e%d" % seq, seq, "assistant", "complete", 1)))
	_check(client.highest_contiguous_seq() == 3, "contiguous before gap")
	# 心跳报告服务端可见水位已领先（长 Thought/审批已持久化）。
	client._handle_message({"version": 1, "type": "heartbeat", "last_seq": 7, "visible_seq": 7, "visible_updated_at": 2.0})
	client._handle_event(_patch("s1", 7, _entry("e7", 4, "approval", "pending", 1)))
	_check(gaps.size() == 1, "gap detected at seq 7")

	var recovery = TranscriptRecovery.new()
	recovery.notify_server_progress(int(client.server_progress().get("visible_seq", 0)))
	var action := recovery.begin("sequence_gap", "resume", {})
	_check(action == "resume", "recovery resumes from contiguous cursor")
	client.recover_from_acknowledged_cursor()
	# 模拟保留窗口重放：缺口前后的事件重新送达。
	for seq in range(4, 8):
		client._handle_event(_patch("s1", seq, _entry("e%d" % seq, seq, "assistant", "complete", 1)))
		recovery.notify_visible_progress(int(client.server_progress().get("visible_seq", 0)))
	_check(client.highest_contiguous_seq() == 7, "replay closed the gap")
	_check(recovery.state == TranscriptRecovery.State.IDLE, "resume episode finished by visible progress")


## 重放仍无法闭合缺口 → 升级为权威快照水合（任务 2.4）。
func _test_gap_escalates_to_snapshot() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "s1"
	var gaps: Array[Dictionary] = []
	client.sequence_gap_detected.connect(func(details: Dictionary): gaps.append(details))
	client._handle_message({"version": 1, "type": "subscribed", "last_seq": 1, "visible_seq": 1, "visible_updated_at": 1.0})
	client._handle_event(_patch("s1", 1, _entry("e1", 1, "user", "complete", 1)))
	client._handle_event(_patch("s1", 9, _entry("e9", 2, "approval", "pending", 1)))
	var recovery = TranscriptRecovery.new()
	_check(recovery.begin("sequence_gap", "resume", {}) == "resume", "gap resumes first")
	client.recover_from_acknowledged_cursor()
	client._handle_event(_patch("s1", 10, _entry("e10", 3, "tool_activity", "running", 1)))
	_check(gaps.size() == 2, "gap persists after resume")
	_check(recovery.begin("sequence_gap", "resume", {}) == "hydrate", "persistent gap escalates to snapshot")
	_check(recovery.state == TranscriptRecovery.State.HYDRATING, "state hydrating after escalation")

	# 快照原子替换：所有可见条目按 ordinal 呈现。
	var store = TranscriptStore.new()
	var projector = TranscriptProjector.new(store)
	var generation := projector.begin_hydration("s1")
	var entries := [
		_entry("e1", 1, "user", "complete", 1),
		_entry("e2", 2, "tool_activity", "resolved", 3, {"tool": "read_class_docs"}),
		_entry("e3", 3, "thought", "complete", 5),
		_entry("e9", 4, "approval", "pending", 1),
		_entry("e10", 5, "tool_activity", "running", 1),
	]
	_check(projector.apply_snapshot(_snapshot("s1", 10, entries), generation), "snapshot accepted")
	_check(store.upto_event_seq == 10, "snapshot cursor adopted")
	var ids: Array = store.ordered_entry_ids()
	_check(ids.size() == 5, "all visible entries recovered")
	var ordered_kinds: Array[String] = []
	for entry_id in ids:
		ordered_kinds.append(str(store.get_entry(str(entry_id)).get("kind", "")))
	_check(ordered_kinds == ["user", "tool_activity", "thought", "approval", "tool_activity"], "entries in ordinal order")
	# 过期/串会话快照必须被拒绝，不能覆盖当前转录（任务 2.5）。
	_check(not projector.apply_snapshot(_snapshot("other_session", 12, entries), generation), "stale session snapshot rejected")
	recovery.finish("snapshot_hydrated")
	_check(recovery.state == TranscriptRecovery.State.IDLE, "hydration finishes episode")


## 服务端保留缺口（类型化信号）直接走快照路径（任务 1.3 / 2.4）。
func _test_server_retention_gap_routes_to_snapshot() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "s1"
	var gaps: Array[Dictionary] = []
	client.history_gap_received.connect(func(details: Dictionary): gaps.append(details))
	client._handle_message({
		"version": 1, "type": "history_gap", "session_id": "s1",
		"after_seq": 3, "earliest_seq": 40, "last_seq": 60, "reason": "retention_gap",
	})
	_check(gaps.size() == 1, "server retention gap surfaced")
	_check(str(gaps[0].get("reason", "")) == "retention_gap", "typed gap reason")
	var recovery = TranscriptRecovery.new()
	_check(recovery.begin(str(gaps[0].get("reason", "retention_gap")), "hydrate", {}) == "hydrate", "typed gap hydrates directly")


## 端到端回归（续传路径）：read_class_docs 之后丢补丁，审批与工具活动最终可见。
func _test_classinfo_drop_regression_resume() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "map1"
	var store = TranscriptStore.new()
	var projector = TranscriptProjector.new(store)
	var recovery = TranscriptRecovery.new()
	var gaps: Array[Dictionary] = []
	client.sequence_gap_detected.connect(func(details: Dictionary): gaps.append(details))
	client.event_received.connect(func(event: Dictionary):
		if projector.apply_event(event, projector.generation):
			recovery.notify_visible_progress(int(client.server_progress().get("visible_seq", 0)))
	)

	var generation := projector.begin_hydration("map1")
	projector.apply_snapshot(_snapshot("map1", 0, []), generation)
	client._handle_message({"version": 1, "type": "subscribed", "last_seq": 0, "visible_seq": 0, "visible_updated_at": 0.0})

	# ClassInfo 工作流：用户请求 → read_class_docs 运行/解析 → 长 Thought 开始。
	client._handle_event(_patch("map1", 1, _entry("e1", 1, "user", "complete", 1, {"text": "map"})))
	client._handle_event(_patch("map1", 2, _entry("e2", 2, "tool_activity", "running", 1, {"tool": "read_class_docs"})))
	client._handle_event(_patch("map1", 3, _entry("e2", 2, "tool_activity", "resolved", 2, {"tool": "read_class_docs", "class": "TileMap"})))
	client._handle_event(_patch("map1", 4, _entry("e3", 3, "thought", "thinking", 1, {"content": "..."})))
	_check(store.entry_count() == 3, "entries projected before drop")

	# 丢失 seq5/seq6（长 Thought 的后续修订与完成态），直接收到 seq7 审批。
	client._handle_message({"version": 1, "type": "heartbeat", "last_seq": 7, "visible_seq": 7, "visible_updated_at": 9.0})
	client._handle_event(_patch("map1", 7, _entry("e4", 4, "approval", "pending", 1, {"tool": "bootstrap_map_agent"})))
	_check(gaps.size() == 1, "drop detected as sequence gap")
	_check(recovery.begin("sequence_gap", "resume", client.transport_diagnostics()) == "resume", "regression resumes first")
	client.recover_from_acknowledged_cursor()

	# 保留窗口重放把缺口补齐：Thought 修订/完成 + 审批重新送达。
	client._handle_event(_patch("map1", 4, _entry("e3", 3, "thought", "thinking", 1, {"content": "..."})))
	client._handle_event(_patch("map1", 5, _entry("e3", 3, "thought", "thinking", 2, {"content": "...."})))
	client._handle_event(_patch("map1", 6, _entry("e3", 3, "thought", "complete", 3, {"content": "....."})))
	client._handle_event(_patch("map1", 7, _entry("e4", 4, "approval", "pending", 1, {"tool": "bootstrap_map_agent"})))

	_check(recovery.state == TranscriptRecovery.State.IDLE, "resume closed the regression gap")
	_check(client.highest_contiguous_seq() == 7, "contiguous restored through seq 7")
	var kinds: Array[String] = []
	for entry_id in store.ordered_entry_ids():
		kinds.append(str(store.get_entry(str(entry_id)).get("kind", "")))
	_check(kinds == ["user", "tool_activity", "thought", "approval"], "ClassInfo workflow fully visible in order")
	_check(str(store.get_entry("e3").get("state", "")) == "complete", "long thought completed after recovery")
	_check(int(store.get_entry("e3").get("revision", 0)) == 3, "thought at latest revision")
	_check(str(store.get_entry("e4").get("state", "")) == "pending", "bootstrap approval visible")
	_check(fake.sent.all(func(message: String):
		return not message.contains("user_message") and not message.contains("approve") and not message.contains("interrupt")
	), "recovery never resubmits commands")


## 端到端回归（快照兜底路径）：保留窗口也无法闭合时水合权威快照。
func _test_classinfo_drop_regression_snapshot() -> void:
	var store = TranscriptStore.new()
	var projector = TranscriptProjector.new(store)
	var recovery = TranscriptRecovery.new()
	var generation := projector.begin_hydration("map2")
	projector.apply_snapshot(_snapshot("map2", 4, [
		_entry("e1", 1, "user", "complete", 1),
		_entry("e2", 2, "tool_activity", "resolved", 2, {"tool": "read_class_docs", "class": "TileMap"}),
	]), generation)
	# 模拟续传失败升级：重放无法闭合缺口 → 水合包含全部后续条目的快照。
	_check(recovery.begin("sequence_gap", "resume", {}) == "resume", "snapshot path also resumes first")
	_check(recovery.begin("sequence_gap", "resume", {}) == "hydrate", "escalates when replay cannot close")
	var full_snapshot := _snapshot("map2", 8, [
		_entry("e1", 1, "user", "complete", 1),
		_entry("e2", 2, "tool_activity", "resolved", 2, {"tool": "read_class_docs", "class": "TileMap"}),
		_entry("e3", 3, "thought", "complete", 3),
		_entry("e4", 4, "approval", "approved", 2, {"tool": "bootstrap_map_agent"}),
		_entry("e5", 5, "tool_activity", "resolved", 2, {"tool": "apply_map_edit"}),
	])
	_check(projector.apply_snapshot(full_snapshot, generation), "authoritative snapshot applied")
	_check(store.upto_event_seq == 8, "resume cursor set to snapshot upto_event_seq")
	recovery.finish("snapshot_hydrated")
	var kinds: Array[String] = []
	for entry_id in store.ordered_entry_ids():
		kinds.append(str(store.get_entry(str(entry_id)).get("kind", "")))
	_check(kinds == ["user", "tool_activity", "thought", "approval", "tool_activity"], "post-approval tool activity visible in order")
	_check(str(store.get_entry("e5").get("state", "")) == "resolved", "post-approval tool result recovered")


## 按测试用途装配一个未入树的 ChatPanel：只连接恢复链路相关组件，
## 不构建完整 UI（无编辑器环境下 `_ready` 不可用）。
func _make_panel() -> VBoxContainer:
	var panel = ChatPanel.new()
	panel.editor_interface = null
	panel._transcript_store = TranscriptStore.new()
	panel._projector = TranscriptProjector.new(panel._transcript_store)
	panel._recovery = TranscriptRecovery.new()
	panel._event_socket = ChatEventSocket.new()
	panel._event_socket.editor_interface = null
	panel._event_socket._stopped = true
	return panel


## 3.7(a)：接收水位超前投影/渲染水位时，停滞判定必须比较四级水位的最小值
## 并报告滞后的可见阶段（任务 2.8），而不是只看接收水位。
func _test_min_watermark_stall_detects_lagging_stage() -> void:
	var panel := _make_panel()
	panel._recovery.stall_threshold_s = 0.02
	panel._state = ChatPanel.AgentState.WAITING_LLM
	# 传输层已接收并提交到 seq7，但投影/渲染停在 seq4（补丁卡在批处理/视口）。
	panel._event_socket._session_id = "stall-a"
	panel._event_socket._highest_contiguous_seq = 7
	panel._event_socket._committed_seq = 7
	panel._transcript_store.upto_event_seq = 4
	panel._rendered_upto_event_seq = 4
	var marks: Dictionary = panel.transcript_watermarks()
	_check(int(marks.get("visible_floor_seq", -1)) == 4, "floor is min of all visible watermarks")
	_check(str(marks.get("lagging_stage", "")) == "projected", "lagging stage names the upstream bottleneck")
	_check(str(panel._lagging_stage(7, 7, 7, 4)) == "rendered", "solely rendered lag reported as rendered")
	_check(str(panel._lagging_stage(7, 5, 6, 6)) == "committed", "committed lag reported before presentation")
	panel._recovery.notify_visible_progress()
	panel._recovery.notify_server_progress(7)
	_check(panel._recovery.server_ahead_of(4), "server ahead of slowest watermark")
	_check(not panel._recovery.server_ahead_of(7), "received watermark alone is not the bar")
	OS.delay_msec(30)
	panel._check_visible_stall()
	_check(panel._recovery.state == TranscriptRecovery.State.RESUMING, "min-watermark stall starts bounded recovery")
	_check(panel._recovery.reason == "visible_stall", "stall trigger named")


## 3.7(b)：投影窗口无法在有界时间内推进投影水位时（暂存积压），
## 必须路由到既有恢复状态机而不是让补丁无限期不可见（任务 2.9）。
func _test_projection_backlog_routes_to_recovery() -> void:
	var panel := _make_panel()
	panel._recovery.stall_threshold_s = 0.01
	panel._state = ChatPanel.AgentState.WAITING_LLM
	panel._event_socket._session_id = "stall-b"
	var backlog_patch := _patch("stall-b", 1, _entry("e1", 1, "thought", "thinking", 1))
	panel._patch_batcher.enqueue("e1", backlog_patch)
	_check(panel._patch_batcher.pending_event_count() == 1, "pending patch count tracked")
	_check(panel._patch_batcher.oldest_pending_usec() > 0, "oldest enqueue time tracked")
	panel._recovery.notify_visible_progress()
	panel._recovery.notify_server_progress(2)
	panel._check_visible_stall()
	_check(panel._recovery.state == TranscriptRecovery.State.IDLE, "fresh backlog does not recover early")
	OS.delay_msec(20)
	panel._check_visible_stall()
	_check(panel._recovery.state == TranscriptRecovery.State.RESUMING, "stuck projection window routes to recovery")
	_check(panel._recovery.reason == "projection_backlog", "backlog trigger named")
	_check(panel._patch_batcher.is_empty(), "resume clears stale staging for replay")


## 3.7(c)：连接 Open 但超过新鲜度窗口没有任何报文时，用有界探针确认服务端
## 状态；服务端领先则走续传恢复，无活跃轮次则不盲目恢复（任务 2.10）。
func _test_silent_socket_probe_routes_recovery() -> void:
	var panel := _make_panel()
	panel._state = ChatPanel.AgentState.WAITING_LLM
	panel._event_socket._session_id = "stall-c"
	panel._event_socket._state = panel._event_socket.STATE_SUBSCRIBED
	# 心跳/事件全部缺席，超出新鲜度窗口。
	panel._event_socket._last_packet_msec = Time.get_ticks_msec() - (ChatPanel.SOCKET_SILENCE_THRESHOLD_MS + 5_000)
	panel._recovery.notify_visible_progress()
	panel._check_visible_stall()
	_check(panel._socket_silence_probe_msec > 0, "silence beyond window starts bounded probe")
	_check(panel._recovery.state == TranscriptRecovery.State.IDLE, "probe alone does not recover")
	# 探针响应：活跃轮次存在且服务端游标领先 → 续传恢复。
	panel._on_probe_response({
		"exists": true,
		"pointer": {"session_id": "stall-c", "pending_turn_id": "t1", "last_event_seq": 6},
	})
	_check(panel._socket_silence_probe_msec == 0, "probe settled after response")
	_check(panel._recovery.state == TranscriptRecovery.State.RESUMING, "server ahead routes resume recovery")
	_check(panel._recovery.reason == "socket_silent_server_ahead", "silence trigger named")
	panel._recovery.finish("confirmed")
	# 再次静默：探针可再次发起；服务端无活跃轮次时不盲目恢复。
	panel._event_socket._last_packet_msec = Time.get_ticks_msec() - (ChatPanel.SOCKET_SILENCE_THRESHOLD_MS + 5_000)
	panel._check_visible_stall()
	_check(panel._socket_silence_probe_msec > 0, "next silence episode probes again")
	panel._on_probe_response({"exists": false})
	_check(panel._socket_silence_probe_msec == 0, "probe settled on negative response")
	_check(panel._recovery.state == TranscriptRecovery.State.IDLE, "no active turn defers to idle timeout")


## 3.7(d)：Reset 与排队的工具结果竞争——中断屏障必须取消在途与排队请求、
## 先发服务端中断再重置，并按世代拒绝旧轮次的迟到响应（任务 2.11）。
##
## 测试环境没有可运行的场景树（真实 HTTP 请求无法发起），这里以受控状态
## 模拟严格串行队列：在途一个工具结果回传、排队一个；重置后观测屏障动作。
func _test_reset_barrier_discards_queued_tool_results() -> void:
	var client = AgentHttpClient.new()
	client.editor_interface = null
	client.service = FakeService.new()
	client._request_timeout_timer = Timer.new()
	client._idle_recovery_timer = Timer.new()
	client._create_chat_http()
	client.current_turn_id = "t-old"
	# 模拟严格串行队列：第一个工具结果回传在途，第二个排队等待。
	client._busy = true
	client._inflight_path = "/chat"
	client._inflight_generation = 0
	client._queue.append({
		"method": "POST",
		"path": "/chat",
		"payload": {"session_id": "s-reset", "tool_results": [{"tool_use_id": "c2"}]},
		"generation": 0,
	})
	# Reset 中断屏障。
	client.reset_session()
	_check(client.current_turn_id == "", "reset clears active turn id")
	_check(client._suppress_events, "late pre-reset events suppressed until next message")
	_check(client._request_generation == 1, "request generation bumped to reject stale callbacks")
	# 严格串行的单一工作槽里最后尝试的是 /reset，证明 /chat/interrupt
	# 先于 /reset 发出；排队的工具结果在进入工作槽前已被清空。
	_check(client._inflight_path == "/reset", "interrupt attempted before reset in strict order")
	_check(client._queue.is_empty(), "queued tool result discarded by reset barrier")
	_check(not client._busy, "in-flight chat cancelled by reset barrier")
	# 旧请求的迟到回调按世代拒绝，不能触碰新请求状态。
	var responses: Array[Dictionary] = []
	client.response_received.connect(func(response: Dictionary): responses.append(response))
	client._busy = true
	client._inflight_generation = client._request_generation - 1
	client._on_request_completed(
		HTTPRequest.RESULT_SUCCESS, 200, PackedStringArray(),
		JSON.stringify({"type": "tool_calls", "turn_id": "t-old", "calls": [{"id": "late"}]}).to_utf8_buffer()
	)
	_check(responses.is_empty(), "late pre-reset response rejected by generation")
	_check(client._busy, "late completion cannot mutate the new request state")


## 追加增量事件包络（修订缺口场景）。
func _delta_event(session: String, seq: int, entry_id: String, revision: int, base_revision: int, append: String) -> Dictionary:
	return {
		"event_id": "%s:%d" % [session, seq], "session_id": session, "seq": seq,
		"type": "transcript_patch",
		"payload": {
			"patch_format": "append_delta", "patch_version": 2, "stream_key": entry_id,
			"entry_id": entry_id, "kind": "assistant", "state": "streaming",
			"revision": revision, "base_revision": base_revision, "text_field": "text", "append_text": append,
		},
	}


## 3.8：传输-视口提交回归。包已到达但投影失败时，ACK 绝不越过最后已提交
## 事件；从已提交游标重连后，服务端重放（而非人工历史重载）使缺失的
## Thought/工具/审批条目恰好一次按序可见。
func _test_commit_cursor_regression() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "commit1"
	client._stopped = false
	var store = TranscriptStore.new()
	var projector = TranscriptProjector.new(store)
	var generation := projector.begin_hydration("commit1")
	projector.apply_snapshot(_snapshot("commit1", 0, []), generation)
	var received_events: Array[Dictionary] = []
	client.event_received.connect(func(event: Dictionary): received_events.append(event))

	# seq1：投影成功 → 提交 → 才有 ACK。
	client._handle_event(_patch("commit1", 1, _entry("e1", 1, "user", "complete", 1)))
	_check(fake.sent.size() == 0, "no ack on receipt alone")
	_check(projector.apply_event(received_events[0], generation), "seq1 projected")
	client.commit_seq(1)
	_check(client.committed_seq() == 1, "seq1 committed after store acceptance")
	_check(fake.sent.size() == 1, "ack sent only after commit")

	# seq2：畸形报文（缺条目身份）→ 投影器在呈现前拒绝；拒绝可被重放修复，
	# 投影器保持就绪，接收但不提交、不 ACK。
	var malformed := _patch("commit1", 2, {
		"entry_id": "", "ordinal": 2, "kind": "thought", "state": "thinking", "revision": 1, "payload": {},
	})
	client._handle_event(malformed)
	_check(client.highest_contiguous_seq() == 2, "seq2 received")
	_check(not projector.apply_event(received_events[1], generation), "seq2 rejected before presentation")
	_check(projector.is_ready(), "replayable rejection keeps projector ready")
	_check(client.committed_seq() == 1, "rejected event never committed")
	_check(fake.sent.size() == 1, "no ack past committed cursor")

	# seq3/seq4：后续条目正常投影，但提交游标连续语义下仍停在 seq1。
	client._handle_event(_patch("commit1", 3, _entry("e3", 3, "tool_activity", "resolved", 1, {"tool": "apply_map_edit"})))
	client._handle_event(_patch("commit1", 4, _entry("e4", 4, "approval", "pending", 1)))
	_check(projector.apply_event(received_events[2], generation), "seq3 projected")
	_check(projector.apply_event(received_events[3], generation), "seq4 projected")
	client.commit_seq(3)
	client.commit_seq(4)
	_check(client.committed_seq() == 1, "committed cursor waits for contiguous seq2")
	_check(fake.sent.size() == 1, "uncommitted tail is not acknowledged")

	# 从已提交游标重连：订阅 after_seq 必须来自提交游标而非接收游标。
	client._stopped = false
	client._seen_event_ids.clear()
	client._subscribe_sent = false
	client._process(0.0)
	var subscribe = JSON.parse_string(fake.sent[fake.sent.size() - 1])
	_check(subscribe is Dictionary and str(subscribe.get("type", "")) == "subscribe", "reconnect sends subscribe")
	_check(int(subscribe.get("after_seq", -1)) == 1, "resubscribe from committed cursor")

	# 服务端重放：seq2 以完整补丁重新送达并被接受 → 连续提交追上尾部。
	client._handle_event(_patch("commit1", 2, _entry("e2", 2, "thought", "complete", 3)))
	_check(received_events.size() == 5, "replayed seq2 re-delivered for projection")
	_check(projector.apply_event(received_events[4], generation), "replayed thought projected")
	client.commit_seq(2)
	_check(client.committed_seq() == 4, "contiguous commits catch up through pending tail")
	var last_ack = JSON.parse_string(fake.sent[fake.sent.size() - 1])
	_check(last_ack is Dictionary and int(last_ack.get("seq", 0)) == 4, "ack advances after contiguous commit")

	# 重放再次送达 seq3/seq4：它们已在提交游标之内，socket 直接丢弃，
	# 不会再进入投影造成重复条目（任务 2.12 / 3.8）。
	client._handle_event(_patch("commit1", 3, _entry("e3", 3, "tool_activity", "resolved", 1, {"tool": "apply_map_edit"})))
	client._handle_event(_patch("commit1", 4, _entry("e4", 4, "approval", "pending", 1)))
	_check(received_events.size() == 5, "already-committed replay dropped before projection")
	var kinds: Array[String] = []
	for entry_id in store.ordered_entry_ids():
		kinds.append(str(store.get_entry(str(entry_id)).get("kind", "")))
	_check(kinds == ["user", "thought", "tool_activity", "approval"], "server replay renders entries once in order")
	_check(store.entry_count() == 4, "no duplicated entries from replay")
	_check(str(store.get_entry("e2").get("state", "")) == "complete", "recovered thought completed")


## 5.6：超尺寸终态工具补丁绝不在主线程解析。拒收只留下尺寸诊断、不推进任何
## 游标；从已提交游标重连后，服务端重放使后续 Thought/工具/审批按序可见。
## 超尺寸包本身构造成合法事件报文——若守卫失效（真的被 JSON 解析），
## `event_received` 就会多出一条事件，断言即可捕获。
func _test_oversized_packet_guard_and_recovery() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "big1"
	client._stopped = false
	var store = TranscriptStore.new()
	var projector = TranscriptProjector.new(store)
	var generation := projector.begin_hydration("big1")
	projector.apply_snapshot(_snapshot("big1", 0, []), generation)
	var received_events: Array[Dictionary] = []
	client.event_received.connect(func(event: Dictionary): received_events.append(event))
	var oversized: Array[Dictionary] = []
	client.oversized_packet_rejected.connect(func(details: Dictionary): oversized.append(details))

	# seq1 正常到达并提交。
	client._handle_event(_patch("big1", 1, _entry("e1", 1, "user", "complete", 1)))
	_check(projector.apply_event(received_events[0], generation), "seq1 projected")
	client.commit_seq(1)
	_check(client.committed_seq() == 1, "seq1 committed before oversized packet")

	# 构造一个超过入站预算、但内容合法的终态工具补丁报文。
	var giant_text := "z".repeat(ChatEventSocket.MAX_INBOUND_PACKET_BYTES + 1024)
	var giant_event := _patch("big1", 2, _entry("e2", 2, "tool_activity", "resolved", 1, {"tool": "grep_code", "blob": giant_text}))
	var wire := JSON.stringify({"version": 1, "type": "event", "event": giant_event})
	fake.packets.append(wire.to_utf8_buffer())
	client._process(0.0)

	_check(oversized.size() == 1, "oversized packet rejected before JSON parsing")
	_check(int(oversized[0].get("packet_bytes", 0)) > ChatEventSocket.MAX_INBOUND_PACKET_BYTES, "diagnostic carries packet size")
	_check(not oversized[0].has("payload"), "diagnostic is size-only redacted")
	_check(received_events.size() == 1, "oversized packet never parsed into an event")
	_check(client.committed_seq() == 1, "oversized packet never committed")
	_check(client.highest_contiguous_seq() == 1, "received cursor untouched by oversized packet")
	_check(fake.closed, "socket closed after oversized rejection")

	# 从已提交游标重连：重放使后续条目按序可见（快照/重放兜底）。
	client._seen_event_ids.clear()
	client._subscribe_sent = false
	client._process(0.0)
	var subscribe = JSON.parse_string(fake.sent[fake.sent.size() - 1])
	_check(subscribe is Dictionary and str(subscribe.get("type", "")) == "subscribe", "reconnect subscribes after oversized reject")
	_check(int(subscribe.get("after_seq", -1)) == 1, "reconnect from committed cursor after oversized reject")
	client._handle_event(_patch("big1", 2, _entry("e2", 2, "tool_activity", "resolved", 1, {"tool": "grep_code", "oversized": true})))
	client._handle_event(_patch("big1", 3, _entry("e3", 3, "thought", "complete", 2)))
	client._handle_event(_patch("big1", 4, _entry("e4", 4, "approval", "pending", 1)))
	for index in range(1, 4):
		_check(projector.apply_event(received_events[index], generation), "replayed entry projected after oversized reject")
		client.commit_seq(int(received_events[index].get("seq", 0)))
	_check(client.committed_seq() == 4, "replay commits through later entries")
	var recovered_kinds: Array[String] = []
	for entry_id in store.ordered_entry_ids():
		recovered_kinds.append(str(store.get_entry(str(entry_id)).get("kind", "")))
	_check(recovered_kinds == ["user", "tool_activity", "thought", "approval"], "post-oversized entries recovered in order")