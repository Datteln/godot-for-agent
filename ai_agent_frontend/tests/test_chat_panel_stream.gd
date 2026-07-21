extends SceneTree

const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")


func _init() -> void:
	var panel := ChatPanel.new()
	panel._history_replaying = true
	panel._render_text_delta_body("f7:4:2", "=-4\n\n")
	if panel._stream_display_text != "=-4\n\n" or not panel._stream_text_dirty:
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
