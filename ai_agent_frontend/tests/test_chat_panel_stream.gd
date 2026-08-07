extends SceneTree

const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")


func _init() -> void:
	var panel := ChatPanel.new()
	panel._timeline_controller.reset_epoch("epoch-1")
	panel._timeline_controller.present_event({
		"seq": 1,
		"session_epoch": "epoch-1",
		"type": "agent_text_delta",
		"payload": {"frame_id": "f7", "message_index": 4, "message_id": "f7:4", "text": "=-4\n\n"},
	})
	var item := panel._timeline_controller.store.item_by_id("assistant:f7:4")
	var blocks: Array = item.get("content_blocks", [])
	if blocks.is_empty() or str(blocks[0].get("text", "")) != "=-4\n\n":
		push_error("first text packet of a new stream segment was discarded")
		quit(1)
		return
	panel._history_refresh_needed = true
	if panel._history_request_before(0.0) != 0 or panel._history_request_before(999.0) != 0:
		push_error("stale history was not refreshed for both scroll directions")
		quit(1)
		return
	panel._history_refresh_needed = false
	panel._history_has_more = true
	panel._history_before = 80
	if panel._history_request_before(0.0) != 80 or panel._history_request_before(999.0) != -1:
		push_error("normal older-history pagination trigger changed")
		quit(1)
		return
	quit()
