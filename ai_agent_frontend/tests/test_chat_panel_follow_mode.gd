extends SceneTree

const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")


func _init() -> void:
	var panel := ChatPanel.new()
	panel._scroll = ScrollContainer.new()
	var bar := panel._scroll.get_v_scroll_bar()
	bar.max_value = 200.0
	bar.page = 20.0

	panel._auto_scroll = true
	panel._user_scroll_intent = false
	panel._on_scroll_value_changed(40.0)
	if not panel._auto_scroll:
		push_error("content growth disabled follow mode without user intent")
		quit(1)
		return

	panel._user_scroll_intent = true
	panel._on_scroll_value_changed(40.0)
	if panel._auto_scroll:
		push_error("identified upward navigation did not disable follow mode")
		quit(1)
		return

	panel._on_scroll_value_changed(180.0)
	if not panel._auto_scroll:
		push_error("returning to the bottom did not re-enable follow mode")
		quit(1)
		return

	bar.max_value = 260.0
	bar.page = 20.0
	panel._scroll.scroll_vertical = 120
	panel._post_final_scroll_frames = 1
	panel._last_selection_refresh_ms = Time.get_ticks_msec()
	panel._process(0.016)
	if panel._scroll.scroll_vertical < 239:
		push_error("final layout correction did not settle at the bottom")
		quit(1)
		return

	quit()
