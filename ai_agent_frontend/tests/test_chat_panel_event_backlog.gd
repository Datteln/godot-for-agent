extends SceneTree

const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")


class RecordingPanel extends ChatPanel:
	var handled_sequences: Array[int] = []

	func _handle_event(event: Dictionary) -> void:
		handled_sequences.append(int(event.get("seq", 0)))

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
	if not panel._streaming_controller.has_pending():
		push_error("large backlog did not yield to a later frame")
		quit(1)
		return
	while panel._streaming_controller.has_pending():
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

	var fragments: Array = panel._streaming_controller.coalesce([
		{"seq": 1, "type": "agent_text_delta", "payload": {"frame_id": "f", "message_index": 2, "text": "a", "append_delta": true}},
		{"seq": 2, "type": "agent_text_delta", "payload": {"frame_id": "f", "message_index": 2, "text": "b", "append_delta": true}},
	])
	if fragments.size() != 2:
		push_error("append-only fragments were coalesced")
		quit(1)
		return

	panel._timeline_controller.reset_epoch("epoch-1")
	panel._timeline_controller.present_event({
		"seq": 204,
		"session_epoch": "epoch-1",
		"type": "agent_text_delta",
		"payload": {"frame_id": "old-frame", "message_index": 1, "message_id": "old-frame:1", "text": "old", "preview_id": "old-preview", "provisional": true},
	})
	panel._timeline_controller.present_event({"seq": 205, "session_epoch": "epoch-1", "type": "submission_preview_discarded", "payload": {"preview_ids": ["new-preview"]}})
	if panel._timeline_controller.store.size() != 1:
		push_error("stale discard boundary altered another preview")
		quit(1)
		return
	panel._timeline_controller.present_event({"seq": 206, "session_epoch": "epoch-1", "type": "submission_preview_committed", "payload": {"preview_ids": ["old-preview"]}})
	if str(panel._timeline_controller.store.get_item(0).get("lifecycle", "")) != "committed":
		push_error("commit boundary duplicated or invalidated preview text")
		quit(1)
		return
	panel._timeline_controller.present_event({"seq": 207, "session_epoch": "epoch-1", "type": "agent_text_delta", "payload": {"frame_id": "finished-frame", "message_index": 9, "message_id": "finished-frame:9", "text": "discard", "preview_id": "discarded-preview", "provisional": true}})
	panel._timeline_controller.present_event({"seq": 208, "session_epoch": "epoch-1", "type": "submission_preview_discarded", "payload": {"preview_ids": ["discarded-preview"]}})
	if panel._timeline_controller.store.size() != 1:
		push_error("discarded finalized preview was not visibly invalidated")
		quit(1)
		return

	quit()
