extends SceneTree

const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")


class RecordingClient extends AgentHttpClient:
	var watchdog_refreshes := 0

	func _maybe_extend_request_timeout() -> void:
		watchdog_refreshes += 1


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

	quit()
