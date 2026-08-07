extends SceneTree

const LogEntryRenderer = preload("res://addons/ai_agent/ui/log_entry_renderer.gd")
const Projector = preload("res://addons/ai_agent/timeline/chat_timeline_projector.gd")
const Registry = preload("res://addons/ai_agent/timeline/chat_item_renderer_registry.gd")
const Store = preload("res://addons/ai_agent/timeline/chat_timeline_store.gd")
const VirtualScroller = preload("res://addons/ai_agent/ui/chat_virtual_scroller.gd")


func _init() -> void:
	var projector := Projector.new()
	var live_store := Store.new()
	live_store.apply_mutation({"kind": "reset_epoch", "session_epoch": "epoch-1"})
	var final_event := {
		"type": "final",
		"seq": 12,
		"session_epoch": "epoch-1",
		"payload": {"frame_id": "f1", "message_index": 4, "message_id": "f1:4", "text": "**done**"},
	}
	var projection := projector.project(final_event)
	if not bool(projection.get("ok", false)) or not bool(live_store.apply_all(projection.get("mutations", [])).get("ok", false)):
		_fail("live final projection failed")
		return
	var live_item := live_store.item_by_id("assistant:f1:4")

	var history_store := Store.new()
	history_store.apply_mutation({"kind": "reset_epoch", "session_epoch": "epoch-1"})
	var history_projection := projector.project({
		"type": "timeline_item",
		"session_epoch": "epoch-1",
		"order_key": live_item.get("order_key", []),
		"payload": {"item": live_item},
	})
	if not bool(history_projection.get("ok", false)) or not bool(history_store.apply_all(history_projection.get("mutations", [])).get("ok", false)):
		_fail("history item projection failed")
		return
	if history_store.get_item(0) != live_store.get_item(0):
		_fail("live/history canonical structures diverged")
		return

	var tool_store := Store.new()
	tool_store.apply_mutation({"kind": "reset_epoch", "session_epoch": "epoch-1"})
	var call := {"id": "tool-1", "name": "apply_text_edit", "frame_id": "f1", "input": {"path": "res://a.gd", "before_text": "a", "content": "b"}}
	var tool_call_projection := projector.project({"type": "tool_calls", "seq": 13, "session_epoch": "epoch-1", "payload": {"calls": [call]}})
	tool_store.apply_all(tool_call_projection.get("mutations", []))
	var tool_result_projection := projector.project({"type": "front_tool_result", "seq": 14, "session_epoch": "epoch-1", "payload": {"frame_id": "f1", "tool_use_id": "tool-1", "call": call, "result": {"status": "applied"}, "status": "applied"}})
	if not bool(tool_store.apply_all(tool_result_projection.get("mutations", [])).get("ok", false)):
		_fail("tool result did not finalize preview item")
		return
	if tool_store.size() != 1 or str(tool_store.get_item(0).get("lifecycle", "")) != "committed":
		_fail("tool preview/result created parallel items")
		return

	var registry := Registry.new()
	registry.log_renderer = LogEntryRenderer.new()
	var live_node := registry.create_item_node(live_item)
	var history_node := registry.create_item_node(history_store.get_item(0))
	if live_node == null or history_node == null or live_node.get_child_count() != history_node.get_child_count():
		_fail("live/history renderer structure diverged")
		return
	if str(live_node.get_meta("copy_text", "")) != str(history_node.get_meta("copy_text", "")):
		_fail("live/history copy policy diverged")
		return
	if live_node.get_combined_minimum_size() != history_node.get_combined_minimum_size():
		_fail("live/history rendered size diverged")
		return
	live_node.free()
	history_node.free()
	if not _test_render_budget_and_prepend_anchor(registry):
		return
	quit()


func _test_render_budget_and_prepend_anchor(registry: RefCounted) -> bool:
	var store := Store.new()
	store.apply_mutation({"kind": "reset_epoch", "session_epoch": "epoch-budget"})
	for index in range(500):
		if not bool(store.apply_mutation({"kind": "insert", "item": _item(index + 10, "epoch-budget")}).get("ok", false)):
			_fail("large timeline fixture was rejected")
			return false
	var scroll := ScrollContainer.new()
	scroll.size = Vector2(640, 240)
	var item_list := VBoxContainer.new()
	scroll.add_child(item_list)
	get_root().add_child(scroll)
	var scroller := VirtualScroller.new()
	scroller.setup(scroll, item_list, store, registry)
	scroller.sync(0.0, false)
	var state: Dictionary = scroller.debug_state()
	if int(state.get("cache_size", 0)) > scroller.MIN_VISIBLE_ITEMS + scroller.BUFFER_ITEMS * 2:
		_fail("virtual scroller rendered beyond its bounded visible window")
		return false
	var prepended: Array = [_item(1, "epoch-budget"), _item(2, "epoch-budget"), _item(3, "epoch-budget")]
	var expected_anchor_offset := 0.0
	for item in prepended:
		expected_anchor_offset += float(item.get("estimated_height", 0.0)) + store.item_spacing
	if not bool(store.apply_mutation({"kind": "prepend_page", "items": prepended}).get("ok", false)):
		_fail("prepend page was rejected")
		return false
	state = scroller.debug_state()
	if absf(float(state.get("pending_anchor_offset", 0.0)) - expected_anchor_offset) > 0.01:
		_fail("prepend page did not preserve the existing visible anchor offset")
		return false
	scroll.free()
	return true


func _item(order: int, epoch: String) -> Dictionary:
	return {
		"schema_version": 1,
		"item_id": "budget:%d" % order,
		"session_epoch": epoch,
		"order_key": [order, 0],
		"kind": "message",
		"role": "assistant",
		"content_blocks": [{"type": "plain_text", "text": "row %d" % order}],
		"lifecycle": "committed",
		"status": "complete",
		"copy_text": "row %d" % order,
		"style_token": "assistant",
		"source": {"frame_id": "budget", "message_index": order},
		"estimated_height": 40.0,
	}


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
