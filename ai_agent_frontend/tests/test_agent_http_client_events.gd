extends SceneTree

const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const AgentEventSocket = preload("res://addons/ai_agent/service/agent_event_socket.gd")
const AgentStateStore = preload("res://addons/ai_agent/state/agent_state_store.gd")
const ChatEventAcceptor = preload("res://addons/ai_agent/state/chat_event_acceptor.gd")
const ContextCollector = preload("res://addons/ai_agent/context/context_collector.gd")
const FileStateCache = preload("res://addons/ai_agent/context/file_state_cache.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")
const SessionTurnState = preload("res://addons/ai_agent/state/session_turn_state.gd")
const ChatPanelText = preload("res://addons/ai_agent/ui/chat_panel_text.gd")


class RecordingClient extends AgentHttpClient:
	var watchdog_refreshes := 0

	func _maybe_extend_request_timeout() -> void:
		watchdog_refreshes += 1


class FrontendAckFailureRecorder extends RefCounted:
	var hits: Array[String] = []

	func hit(name: String) -> bool:
		hits.append(name)
		return true


func _init() -> void:
	var state := SessionTurnState.new()
	state.configure("s1", "epoch-new", 3)
	state.begin_reset()
	state.adopt_reset("s2", "epoch-reset", 9)
	var reset_snapshot: Dictionary = state.snapshot()
	if (
		str(reset_snapshot.get("session_id", "")) != "s2"
		or str(reset_snapshot.get("session_epoch", "")) != "epoch-reset"
		or int(reset_snapshot.get("accepted_seq", 0)) != 9
	):
		push_error("new-session reset did not atomically adopt identity and cursor")
		quit(1)
		return
	state.configure("s1", "epoch-new", 3)
	var socket := AgentEventSocket.new()
	root.add_child(socket)
	socket.configure(state)
	var collector := ContextCollector.new()
	root.add_child(collector)
	if not (collector.project_files() is Array):
		push_error("project-file cache did not return an array")
		quit(1)
		return
	collector._project_files_cache = ["res://cached.gd"]
	collector._project_files_dirty = false
	var cached_files: Array = collector.project_files()
	cached_files.append("res://caller-only.gd")
	if collector.project_files() != ["res://cached.gd"]:
		push_error("project-file cache leaked mutable caller state")
		quit(1)
		return
	collector._mark_project_files_dirty()
	if not collector._project_files_dirty:
		push_error("project-file cache invalidation signal did not mark state dirty")
		quit(1)
		return
	for locale in ["zh", "en"]:
		for key in ["ws_connect_failed", "ws_closed", "ws_reconnect_exhausted"]:
			if ChatPanelText.text(locale, key) == key:
				push_error("missing localized WebSocket presentation text: " + locale + "/" + key)
				quit(1)
				return
	var redacted := FrontendLogger._redact_dictionary({
		"session_id": "s1",
		"turn_id": "t1",
		"session_epoch": "e1",
		"count": 2,
	})
	if (
		str(redacted.get("session_id", "")) != "<redacted>"
		or str(redacted.get("turn_id", "")) != "<redacted>"
		or str(redacted.get("session_epoch", "")) != "<redacted>"
		or int(redacted.get("count", 0)) != 2
	):
		push_error("frontend diagnostics did not redact retained identifiers")
		quit(1)
		return
	var acceptor := ChatEventAcceptor.new()
	acceptor.state = state
	var accepted := acceptor.accept_batch({
		"session_epoch": "epoch-new",
		"events": [
			{"seq": 4, "session_epoch": "epoch-new", "type": "agent_text_delta"},
			{"seq": 5, "session_epoch": "epoch-new", "type": "final"},
		],
	})
	if not bool(accepted.get("accepted", false)) or int(accepted.get("accepted_seq", 0)) != 5:
		push_error("contiguous WebSocket batch was rejected")
		quit(1)
		return
	var duplicate := acceptor.accept_batch({
		"session_epoch": "epoch-new",
		"events": [
			{"seq": 5, "session_epoch": "epoch-new", "type": "final"},
			{"seq": 6, "session_epoch": "epoch-new", "type": "status"},
		],
	})
	if duplicate.get("events", []).size() != 1 or int(duplicate.get("accepted_seq", 0)) != 6:
		push_error("duplicate WebSocket event was not ignored exactly once")
		quit(1)
		return
	var gap := acceptor.accept_batch({
		"session_epoch": "epoch-new",
		"events": [{"seq": 8, "session_epoch": "epoch-new", "type": "status"}],
	})
	if bool(gap.get("accepted", true)) or str(gap.get("reason", "")) != "sequence_gap":
		push_error("sequence gap did not require a snapshot")
		quit(1)
		return
	var stale := acceptor.accept_batch({
		"session_epoch": "epoch-old",
		"events": [{"seq": 7, "session_epoch": "epoch-old", "type": "status"}],
	})
	if bool(stale.get("accepted", true)) or str(stale.get("reason", "")) != "stale_epoch":
		push_error("stale epoch crossed the event acceptor")
		quit(1)
		return

	var recording := RecordingClient.new()
	recording.notify_application_progress()
	if recording.watchdog_refreshes != 1:
		push_error("application progress did not refresh the request watchdog")
		quit(1)
		return
	recording.send_tool_results([{"tool_use_id": "legacy"}], "model")
	if not recording._queue.is_empty():
		push_error("retired frontend tool results were re-injected into /chat")
		quit(1)
		return
	if not recording._queue.is_empty() or recording._request_generation != 0:
		push_error("application progress replayed a command")
		quit(1)
		return

	var file_cache := FileStateCache.new()
	file_cache.snapshot("res://project.godot", true)
	file_cache.clear()
	if file_cache.has_state("res://project.godot"):
		push_error("read-before-edit authorization crossed reset")
		quit(1)
		return

	var state_store := AgentStateStore.new()
	state_store.set_value("session_epoch", "epoch-old")
	state_store.set_value("pending_calls", [{"id": "old"}])
	state_store.add_event({"seq": 8, "type": "old"})
	state_store.reset("epoch-new", 9)
	if (
		str(state_store.state.get("session_epoch", "")) != "epoch-new"
		or int(state_store.state.get("last_event_seq", 0)) != 9
		or not state_store.state.get("pending_calls", []).is_empty()
		or not state_store.state.get("event_log", []).is_empty()
	):
		push_error("frontend reset did not clear Session-owned state")
		quit(1)
		return

	if recording._hit_test_failpoint("frontend_ack_before_adopt"):
		push_error("production frontend acknowledgement failpoint was enabled")
		quit(1)
		return
	var ack_failures := FrontendAckFailureRecorder.new()
	recording.install_test_failure_injector(ack_failures.hit)
	if not recording._hit_test_failpoint("frontend_ack_before_adopt"):
		push_error("before-adopt failpoint was not deterministic")
		quit(1)
		return
	if not recording._hit_test_failpoint("frontend_ack_after_adopt"):
		push_error("after-adopt failpoint was not deterministic")
		quit(1)
		return
	if ack_failures.hits != ["frontend_ack_before_adopt", "frontend_ack_after_adopt"]:
		push_error("frontend acknowledgement failpoint order changed")
		quit(1)
		return

	quit()
