extends SceneTree

## fix-tool-block-interrupt-and-selection-fallback 的中断收尾回归测试（headless）。

const Controller = preload("res://addons/ai_agent/controllers/chat_timeline_controller.gd")

var failures: Array = []


func _initialize() -> void:
	_test_interrupt_resolves_pending_tool_items()
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


func _tool_event(seq: int, turn_id: String, call_id: String) -> Dictionary:
	return {
		"type": "tool_calls",
		"seq": seq,
		"session_epoch": "t1",
		"payload": {
			"turn_id": turn_id,
			"frame_id": "f1",
			"calls": [{"id": call_id, "name": "read_scene_tree", "input": {}, "needs_confirm": false}],
			"count": 1,
			"text": "",
		},
	}


func _test_interrupt_resolves_pending_tool_items() -> void:
	var controller := Controller.new()
	controller.reset_epoch("t1")
	controller.present_event(_tool_event(1, "turn_x", "c1"))
	controller.present_event(_tool_event(2, "turn_y", "c2"))
	_check(str(controller.store.item_by_id("tool:c1").get("status", "")) == "pending", "c1_pending_before")
	var resolved := controller.interrupt_pending_tools("turn_x")
	_check(resolved == 1, "interrupt_resolves_one")
	_check(str(controller.store.item_by_id("tool:c1").get("status", "")) == "interrupted", "c1_interrupted")
	_check(str(controller.store.item_by_id("tool:c2").get("status", "")) == "pending", "c2_untouched_other_turn")
	_check(controller.interrupt_pending_tools("turn_x") == 0, "second_interrupt_idempotent")