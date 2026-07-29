extends SceneTree

const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")


class RecordingPanel extends ChatPanel:
	var handled_sequences: Array[int] = []
	var invalidation_messages: Array[String] = []

	func _handle_event(event: Dictionary) -> void:
		handled_sequences.append(int(event.get("seq", 0)))

	func _append_message(_role: String, text: String, _color = null) -> void:
		invalidation_messages.append(text)


func _init() -> void:
	var panel := RecordingPanel.new()
	var burst: Array = []
	for seq in range(1, 204):
		burst.append({
			"seq": seq,
			"type": "agent_text_delta",
			"payload": {
				"frame_id": "f1",
				"message_index": 1,
				"text": str(seq),
				"append_delta": true,
			},
		})
	panel._on_events(burst)
	if panel.handled_sequences.size() > panel.EVENT_DRAIN_BATCH_SIZE:
		push_error("203-event burst drained in one render batch")
		quit(1)
		return
	if panel._event_queue.is_empty():
		push_error("large backlog did not yield to a later frame")
		quit(1)
		return
	while panel._draining_events:
		panel._drain_event_queue()
	if panel.handled_sequences.size() != 203:
		push_error("backlog did not complete")
		quit(1)
		return
	for index in range(203):
		if panel.handled_sequences[index] != index + 1:
			push_error("backlog order changed")
			quit(1)
			return

	var fragments := panel._coalesce_events([
		{"seq": 1, "type": "agent_text_delta", "payload": {"frame_id": "f", "message_index": 2, "text": "a", "append_delta": true}},
		{"seq": 2, "type": "agent_text_delta", "payload": {"frame_id": "f", "message_index": 2, "text": "b", "append_delta": true}},
	])
	if fragments.size() != 2:
		push_error("append-only fragments were coalesced")
		quit(1)
		return

	panel._preview_registry["old-preview"] = {
		"request_id": "old-request",
		"kind": "text",
		"stream_key": "old-frame:1",
	}
	panel._resolve_provisional_previews({"preview_ids": ["new-preview"]}, false)
	if not panel._preview_registry.has("old-preview"):
		push_error("stale discard boundary altered another preview")
		quit(1)
		return
	panel._resolve_provisional_previews({"preview_ids": ["old-preview"]}, true)
	if not panel.invalidation_messages.is_empty():
		push_error("commit boundary duplicated or invalidated preview text")
		quit(1)
		return
	panel._preview_registry["discarded-preview"] = {
		"request_id": "discarded-request",
		"kind": "text",
		"stream_key": "finished-frame:9",
	}
	panel._resolve_provisional_previews({"preview_ids": ["discarded-preview"]}, false)
	if panel.invalidation_messages.size() != 1:
		push_error("discarded finalized preview was not visibly invalidated")
		quit(1)
		return

	quit()
