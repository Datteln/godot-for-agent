extends SceneTree

const Contracts = preload("res://addons/ai_agent/timeline/chat_timeline_contracts.gd")
const Projector = preload("res://addons/ai_agent/timeline/chat_timeline_projector.gd")
const Store = preload("res://addons/ai_agent/timeline/chat_timeline_store.gd")


func _init() -> void:
	var store := Store.new()
	if not bool(store.apply_mutation({"kind": "reset_epoch", "session_epoch": "epoch-1"}).get("ok", false)):
		_fail("timeline epoch reset failed")
		return
	var projector := Projector.new()
	var delta := {
		"type": "agent_text_delta",
		"seq": 7,
		"session_epoch": "epoch-1",
		"payload": {"frame_id": "f1", "message_index": 3, "message_id": "f1:3", "text": "hello", "preview_id": "p1", "provisional": true},
	}
	if not _apply_projection(projector, store, delta):
		return
	var delta_append := delta.duplicate(true)
	delta_append["seq"] = 8
	delta_append["payload"]["text"] = " world"
	delta_append["payload"]["append_delta"] = true
	if not _apply_projection(projector, store, delta_append):
		return
	var final_event := {
		"type": "final",
		"seq": 9,
		"session_epoch": "epoch-1",
		"payload": {"frame_id": "f1", "message_index": 3, "message_id": "f1:3", "text": "hello world"},
	}
	if not _apply_projection(projector, store, final_event):
		return
	if store.size() != 1:
		_fail("stream and final did not share one item")
		return
	var item := store.get_item(0)
	if str(item.get("lifecycle", "")) != "committed" or str(item.get("copy_text", "")) != "hello world":
		_fail("stream final lifecycle/content changed")
		return

	var invalid := store.apply_mutation({"kind": "mystery", "item_id": "x"})
	if bool(invalid.get("ok", false)):
		_fail("unknown mutation did not fail closed")
		return
	var mismatched := item.duplicate(true)
	mismatched["item_id"] = "other"
	mismatched["session_epoch"] = "epoch-old"
	if bool(store.apply_mutation({"kind": "insert", "item": mismatched}).get("ok", false)):
		_fail("old epoch item was accepted")
		return

	var reasoning := projector.project({
		"type": "agent_reasoning_delta",
		"seq": 6,
		"session_epoch": "epoch-1",
		"payload": {"frame_id": "f1", "message_index": 3, "message_id": "f1:3", "text": "why", "preview_id": "r1", "provisional": true},
	})
	if not bool(reasoning.get("ok", false)) or not bool(store.apply_all(reasoning.get("mutations", [])).get("ok", false)):
		_fail("reasoning projection failed")
		return
	if str(store.get_item(0).get("kind", "")) != "reasoning":
		_fail("timeline order key did not place reasoning before body")
		return
	if not bool(store.apply_mutation({"kind": "discard", "preview_id": "r1"}).get("ok", false)):
		_fail("preview discard failed")
		return
	if store.size() != 1:
		_fail("preview discard touched unrelated item")
		return
	quit()


func _apply_projection(projector: RefCounted, store: RefCounted, event: Dictionary) -> bool:
	var projection: Dictionary = projector.project(event)
	if not bool(projection.get("ok", false)):
		_fail("projection rejected valid event: %s" % str(projection.get("reason", "")))
		return false
	var result: Dictionary = store.apply_all(projection.get("mutations", []))
	if not bool(result.get("ok", false)):
		_fail("store rejected valid mutation: %s" % str(result.get("reason", "")))
		return false
	return true


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
