class_name ChatTimelineController
extends RefCounted

const Projector = preload("res://addons/ai_agent/timeline/chat_timeline_projector.gd")
const Store = preload("res://addons/ai_agent/timeline/chat_timeline_store.gd")

signal projection_rejected(reason: String, event: Dictionary)

var projector := Projector.new()
var store := Store.new()
var _epoch := ""
var _local_order := 1_000_000_000


func reset_epoch(epoch: String) -> bool:
	_epoch = epoch
	_local_order = 1_000_000_000
	return bool(store.apply_mutation({"kind": "reset_epoch", "session_epoch": epoch}).get("ok", false))


func present_event(event: Dictionary) -> bool:
	var projection := projector.project(event, _epoch)
	if not bool(projection.get("ok", false)):
		projection_rejected.emit(str(projection.get("reason", "projection_failed")), event.duplicate(true))
		return false
	var result := store.apply_all(projection.get("mutations", []))
	if not bool(result.get("ok", false)):
		projection_rejected.emit(str(result.get("reason", "mutation_failed")), event.duplicate(true))
		return false
	return true


func prepend_history(records: Array) -> bool:
	var items: Array = []
	for raw_record in records:
		if not (raw_record is Dictionary):
			projection_rejected.emit("invalid_history_record", {})
			return false
		var projection := projector.project(raw_record, _epoch)
		if not bool(projection.get("ok", false)):
			projection_rejected.emit(str(projection.get("reason", "history_projection_failed")), raw_record.duplicate(true))
			return false
		for mutation in projection.get("mutations", []):
			if str(mutation.get("kind", "")) == "insert":
				items.append((mutation.get("item", {}) as Dictionary).duplicate(true))
			else:
				var result := store.apply_mutation(mutation)
				if not bool(result.get("ok", false)):
					return false
	return bool(store.apply_mutation({"kind": "prepend_page", "items": items}).get("ok", false))


func present_local_text(role: String, text: String, style_token: String = "") -> String:
	_local_order += 1
	var item_id := "local:%s:%d" % [_epoch, _local_order]
	var item := {
		"item_id": item_id,
		"session_epoch": _epoch,
		"order_key": [_local_order, 0, 0],
		"kind": "error" if role == "error" else ("message" if role in ["user", "assistant"] else "system"),
		"role": role,
		"content_blocks": [{"type": "markdown", "text": text}],
		"lifecycle": "committed",
		"status": "",
		"copy_text": text,
		"style_token": style_token if not style_token.is_empty() else role,
		"source": {"local_intent": true},
		"estimated_height": 64.0,
	}
	var result := store.apply_mutation({"kind": "insert", "item": item})
	return item_id if bool(result.get("ok", false)) else ""


func remove_item(item_id: String) -> bool:
	return bool(store.apply_mutation({"kind": "remove", "item_id": item_id}).get("ok", false))
