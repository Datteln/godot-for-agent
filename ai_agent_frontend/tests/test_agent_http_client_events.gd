extends SceneTree

const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const AgentStateStore = preload("res://addons/ai_agent/state/agent_state_store.gd")
const FileStateCache = preload("res://addons/ai_agent/context/file_state_cache.gd")


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
	var client := AgentHttpClient.new()

	var old_backend := client._parse_event_page({
		"events": [
			{"seq": 4, "type": "agent_text_delta", "payload": {"text": "a"}},
			{"seq": 5, "type": "agent_text_delta", "payload": {"text": "b"}},
		]
	}, 3)
	if not bool(old_backend.get("valid", false)):
		push_error("older backend response was rejected")
		quit(1)
		return
	if bool(old_backend.get("has_more", true)) or int(old_backend.get("cursor", 0)) != 5:
		push_error("older backend optional paging defaults changed")
		quit(1)
		return

	var bounded := client._parse_event_page({
		"events": [
			{"seq": 5, "type": "duplicate"},
			"malformed",
			{"seq": 6, "type": "accepted"},
			{"seq": 8, "type": "accepted"},
		],
		"cursor": 999,
		"has_more": true,
	}, 5)
	var accepted: Array = bounded.get("events", [])
	if accepted.size() != 2 or int(bounded.get("cursor", 0)) != 8:
		push_error("cursor advanced beyond accepted events")
		quit(1)
		return
	if not bool(bounded.get("has_more", false)):
		push_error("has_more was not parsed")
		quit(1)
		return

	var malformed := client._parse_event_page({"events": {}}, 8)
	if bool(malformed.get("valid", true)) or int(malformed.get("cursor", 0)) != 8:
		push_error("malformed event page changed the cursor")
		quit(1)
		return

	var recording := RecordingClient.new()
	var preview_body := JSON.stringify({
		"events": [{
			"seq": 1,
			"type": "agent_text_delta",
			"payload": {
				"provisional": true,
				"preview_id": "preview-1",
				"text": "hello",
			},
		}],
		"has_more": false,
	}).to_utf8_buffer()
	recording._on_events_completed(
		HTTPRequest.RESULT_SUCCESS,
		200,
		PackedStringArray(),
		preview_body,
	)
	if recording.watchdog_refreshes != 1:
		push_error("provisional preview did not refresh the request watchdog")
		quit(1)
		return
	if not recording._queue.is_empty() or recording._request_generation != 0:
		push_error("provisional preview triggered request replay or fallback")
		quit(1)
		return

	var delivered_epochs: Array = []
	recording.events_received.connect(func(events: Array) -> void: delivered_epochs.append(events))
	recording._session_epoch = "epoch-new"
	recording._last_event_seq = 8
	recording._on_events_completed(
		HTTPRequest.RESULT_SUCCESS,
		200,
		PackedStringArray(),
		JSON.stringify({
			"session_epoch": "epoch-old",
			"events": [{"seq": 9, "type": "stale"}],
		}).to_utf8_buffer(),
	)
	if recording._last_event_seq != 8 or not delivered_epochs.is_empty():
		push_error("late old-epoch event response crossed reset")
		quit(1)
		return
	recording._on_events_completed(
		HTTPRequest.RESULT_SUCCESS,
		200,
		PackedStringArray(),
		JSON.stringify({
			"session_epoch": "epoch-new",
			"events": [{"seq": 9, "type": "fresh"}],
		}).to_utf8_buffer(),
	)
	if recording._last_event_seq != 9 or delivered_epochs.size() != 1:
		push_error("new-epoch reconnect cursor did not resume from acknowledgement")
		quit(1)
		return

	var file_cache := FileStateCache.new()
	file_cache.snapshot("res://project.godot", true)
	if not file_cache.has_state("res://project.godot"):
		push_error("file authorization fixture was not established")
		quit(1)
		return
	file_cache.clear()
	if file_cache.has_state("res://project.godot"):
		push_error("read-before-edit authorization crossed reset")
		quit(1)
		return

	var state_store := AgentStateStore.new()
	state_store.set_value("session_epoch", "epoch-old")
	state_store.set_value("pending_calls", [{"id": "old"}])
	state_store.set_value("recovery_pointer", {"turn_id": "t4"})
	state_store.add_event({"seq": 8, "type": "old"})
	state_store.reset("epoch-new", 9)
	if (
		str(state_store.state.get("session_epoch", "")) != "epoch-new"
		or int(state_store.state.get("last_event_seq", 0)) != 9
		or not state_store.state.get("pending_calls", []).is_empty()
		or state_store.state.get("recovery_pointer") != null
		or not state_store.state.get("event_log", []).is_empty()
	):
		push_error("frontend reset did not clear all session-owned state")
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
	if ack_failures.hits != [
		"frontend_ack_before_adopt",
		"frontend_ack_after_adopt",
	]:
		push_error("frontend acknowledgement failpoint order changed")
		quit(1)
		return
	if not recording._queue.is_empty() or recording._request_generation != 0:
		push_error("frontend acknowledgement failpoint replayed a request")
		quit(1)
		return

	quit()
