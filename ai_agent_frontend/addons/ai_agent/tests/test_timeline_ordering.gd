extends SceneTree

## fix-chat-timeline-ordering 的排序回归测试（headless）。
## 覆盖：乐观气泡对账后位于 turn 首位；事件/消息/本地交错；store 拒绝非 int 键。

const Controller = preload("res://addons/ai_agent/controllers/chat_timeline_controller.gd")
const Store = preload("res://addons/ai_agent/timeline/chat_timeline_store.gd")

var failures: Array = []


func _initialize() -> void:
	_test_ordering_with_optimistic_user_reconciliation()
	_test_store_rejects_non_int_order_key()
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


func _test_ordering_with_optimistic_user_reconciliation() -> void:
	var controller := Controller.new()
	controller.reset_epoch("t1")
	var local_id := controller.present_local_text("user", "hello")
	_check(local_id != "", "optimistic_user_item_created")
	controller.present_event({"type": "agent_step", "seq": 5, "session_epoch": "t1", "payload": {"frame_id": "f1", "agent": "coordinator", "depth": 0, "loop": 0}})
	controller.present_event({"type": "agent_reasoning_delta", "seq": 6, "session_epoch": "t1", "payload": {"message_id": "m1", "frame_id": "f1", "text": "think", "append_delta": false, "token_count": 1}})
	var notice_id := controller.present_local_text("system", "stopped")
	_check(controller.promote_local_to_next_insert(local_id), "promote_local_ok")
	controller.present_event({"type": "user_submitted", "seq": 7, "session_epoch": "t1", "payload": {"text": "hello", "message_id": "mu", "frame_id": "f0"}})
	var store := controller.store
	_check(store.index_of("user:mu") == 0, "user_echo_first_after_reconciliation")
	_check(store.index_of("event:5") == 1, "event_item_second")
	_check(store.index_of("reasoning:m1") == 2, "reasoning_third")
	_check(store.index_of(notice_id) == 3, "local_notice_after_reasoning")
	_check(store.index_of(local_id) == -1, "optimistic_local_removed")


func _test_store_rejects_non_int_order_key() -> void:
	var store := Store.new()
	store.apply_mutation({"kind": "reset_epoch", "session_epoch": "t1"})
	var item := {
		"item_id": "bad:1",
		"session_epoch": "t1",
		"order_key": ["root", 0, 1],
		"kind": "message",
		"role": "assistant",
		"content_blocks": [{"type": "markdown", "text": "x"}],
		"lifecycle": "committed",
		"status": "",
		"copy_text": "x",
		"style_token": "assistant",
		"source": {},
	}
	var result := store.apply_mutation({"kind": "insert", "item": item})
	_check(not bool(result.get("ok", false)), "non_int_key_rejected")
	_check(str(result.get("reason", "")) == "mixed_order_key_types", "rejection_reason_typed")