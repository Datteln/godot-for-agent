extends SceneTree

## fix-tool-call-event-routing 的行为回归测试（headless）。
## 覆盖：agent_tool_calls 仅投影不执行；空 calls 的 tool_calls 为 no-op。

const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")
const HttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")

var failures: Array = []


func _initialize() -> void:
	_test_agent_tool_calls_display_only()
	_test_empty_tool_calls_noop()
	if failures.is_empty():
		print("ALL_GDSCRIPT_TESTS_PASSED")
		quit(0)
	else:
		print("GDSCRIPT_TEST_FAILURES:")
		for entry in failures:
			print("  - ", entry)
		quit(1)


func _check(condition: bool, name: String) -> void:
	if not condition:
		failures.append(name)


func _make_panel() -> Control:
	var panel = ChatPanel.new()
	panel._timeline_controller.reset_epoch("t1")
	return panel


func _test_agent_tool_calls_display_only() -> void:
	var panel := _make_panel()
	var event := {
		"type": "agent_tool_calls",
		"seq": 1,
		"session_epoch": "t1",
		"payload": {"frame_id": "f1", "agent": "map-agent", "tools": ["edit_map"]},
	}
	panel._handle_event(event)
	_check(int(panel._state) == int(ChatPanel.AgentState.IDLE), "agent_tool_calls_keeps_idle")
	_check(panel._timeline_controller.store.size() == 0, "agent_tool_calls_creates_no_items")
	panel.free()


func _test_empty_tool_calls_noop() -> void:
	var panel := _make_panel()
	var before: int = int(panel._timeline_controller.store.size())
	var event := {
		"type": "tool_calls",
		"seq": 2,
		"session_epoch": "t1",
		"payload": {"turn_id": "turn_a", "calls": [], "count": 0, "text": ""},
	}
	panel._handle_event(event)
	_check(int(panel._state) == int(ChatPanel.AgentState.IDLE), "empty_tool_calls_keeps_idle")
	_check(str(panel._session_state.snapshot().get("active_turn_id", "")) != "turn_a", "empty_tool_calls_does_not_adopt_turn")
	_check(panel._timeline_controller.store.size() == before, "empty_tool_calls_no_timeline_mutation")
	panel.free()