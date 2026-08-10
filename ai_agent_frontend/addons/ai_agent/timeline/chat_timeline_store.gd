class_name ChatTimelineStore
extends RefCounted

const Contracts = preload("res://addons/ai_agent/timeline/chat_timeline_contracts.gd")

signal mutation_applied(mutation: Dictionary, result: Dictionary)
signal mutation_rejected(mutation: Dictionary, reason: String)

var item_spacing := 0.0
var _session_epoch := ""
var _items_by_id: Dictionary = {}
var _ordered_ids: Array[String] = []
var _preview_items: Dictionary = {}
var _heights: Dictionary = {}


func apply_mutation(mutation: Dictionary) -> Dictionary:
	var validation := Contracts.validate_mutation(mutation)
	if not bool(validation.get("ok", false)):
		return _reject(mutation, str(validation.get("reason", "invalid_mutation")))
	var kind := str(mutation.get("kind", ""))
	var result: Dictionary
	match kind:
		"reset_epoch":
			result = _reset_epoch(str(mutation.get("session_epoch", "")))
		"insert":
			result = _insert(mutation.get("item", {}))
		"patch":
			result = _patch(_resolve_item_id(mutation), mutation.get("patch", {}))
		"finalize":
			result = _transition(_resolve_item_id(mutation), "committed", str(mutation.get("status", "complete")))
		"discard":
			result = _transition(_resolve_item_id(mutation), "discarded", str(mutation.get("status", "discarded")))
		"remove":
			result = _remove(_resolve_item_id(mutation))
		"prepend_page":
			result = _prepend_page(mutation.get("items", []))
		_:
			return _reject(mutation, "unknown_mutation")
	if not bool(result.get("ok", false)):
		return _reject(mutation, str(result.get("reason", "mutation_failed")))
	mutation_applied.emit(mutation.duplicate(true), result.duplicate(true))
	return result


func apply_all(mutations: Array) -> Dictionary:
	for raw_mutation in mutations:
		if not (raw_mutation is Dictionary):
			return {"ok": false, "reason": "invalid_mutation"}
		var result := apply_mutation(raw_mutation)
		if not bool(result.get("ok", false)):
			return result
	return {"ok": true}


func epoch() -> String:
	return _session_epoch


func size() -> int:
	return _ordered_ids.size()


func get_item(index: int) -> Dictionary:
	if index < 0 or index >= _ordered_ids.size():
		return {}
	return (_items_by_id.get(_ordered_ids[index], {}) as Dictionary).duplicate(true)


func item_by_id(item_id: String) -> Dictionary:
	return (_items_by_id.get(item_id, {}) as Dictionary).duplicate(true)


func index_of(item_id: String) -> int:
	return _ordered_ids.find(item_id)


func update_height(index: int, height: float) -> void:
	if index < 0 or index >= _ordered_ids.size() or height <= 0.0:
		return
	_heights[_ordered_ids[index]] = height


func height_at(index: int) -> float:
	if index < 0 or index >= _ordered_ids.size():
		return 0.0
	var item := get_item(index)
	var measured := float(_heights.get(_ordered_ids[index], 0.0))
	var estimated := float(item.get("estimated_height", 64.0))
	return (measured if measured > 1.0 else maxf(estimated, 1.0)) + item_spacing


func total_height(from_index: int = 0, to_index: int = -1, excluded: Dictionary = {}) -> float:
	var end := _ordered_ids.size() if to_index < 0 else mini(to_index, _ordered_ids.size())
	var total := 0.0
	for index in range(maxi(0, from_index), end):
		if not excluded.has(index):
			total += height_at(index)
	return total


func find_index_at_scroll(y: float) -> int:
	var total := 0.0
	for index in range(_ordered_ids.size()):
		total += height_at(index)
		if total >= y:
			return index
	return maxi(0, _ordered_ids.size() - 1)


func _reset_epoch(new_epoch: String) -> Dictionary:
	var removed := _ordered_ids.size()
	_session_epoch = new_epoch
	_items_by_id.clear()
	_ordered_ids.clear()
	_preview_items.clear()
	_heights.clear()
	return {"ok": true, "kind": "reset_epoch", "removed": removed}


func _insert(raw_item: Dictionary) -> Dictionary:
	var validation := Contracts.validate_item(raw_item)
	if not bool(validation.get("ok", false)):
		return validation
	var item: Dictionary = validation.get("item", {})
	if str(item.get("session_epoch", "")) != _session_epoch:
		return {"ok": false, "reason": "epoch_mismatch"}
	for key_part in (item.get("order_key", []) as Array):
		if not (key_part is int):
			return {"ok": false, "reason": "mixed_order_key_types"}
	var item_id := str(item.get("item_id", ""))
	if _items_by_id.has(item_id):
		var existing: Dictionary = _items_by_id[item_id]
		if existing.get("order_key", []) != item.get("order_key", []) or str(existing.get("kind", "")) != str(item.get("kind", "")):
			return {"ok": false, "reason": "ambiguous_item_identity"}
		return {"ok": true, "kind": "insert", "item_id": item_id, "index": index_of(item_id), "duplicate": true}
	_items_by_id[item_id] = item
	_index_preview(item)
	if str(item.get("lifecycle", "")) != "discarded":
		_ordered_ids.append(item_id)
		_sort_order()
	return {"ok": true, "kind": "insert", "item_id": item_id, "index": index_of(item_id), "duplicate": false}


func _patch(item_id: String, patch: Dictionary) -> Dictionary:
	if item_id.is_empty() or not _items_by_id.has(item_id):
		return {"ok": false, "reason": "item_not_found"}
	var item: Dictionary = _items_by_id[item_id]
	if str(item.get("lifecycle", "")) == "discarded":
		return {"ok": false, "reason": "discarded_item_patch"}
	for immutable_key in ["item_id", "session_epoch", "order_key", "kind", "role", "source"]:
		if patch.has(immutable_key):
			return {"ok": false, "reason": "immutable_identity_patch"}
	if patch.has("text"):
		var blocks: Array = item.get("content_blocks", []).duplicate(true)
		var block_index := int(patch.get("block_index", 0))
		if block_index < 0 or block_index >= blocks.size() or not (blocks[block_index] is Dictionary):
			return {"ok": false, "reason": "invalid_block_patch"}
		var block: Dictionary = blocks[block_index]
		var next_text := str(patch.get("text", ""))
		block["text"] = str(block.get("text", "")) + next_text if bool(patch.get("append_text", false)) else next_text
		if patch.has("token_count"):
			block["token_count"] = int(patch.get("token_count", 0))
		blocks[block_index] = block
		item["content_blocks"] = blocks
		item["copy_text"] = str(item.get("copy_text", "")) + next_text if bool(patch.get("append_text", false)) else str(patch.get("copy_text", next_text))
	if patch.has("content_blocks"):
		var next_blocks: Variant = patch.get("content_blocks", [])
		if not (next_blocks is Array) or next_blocks.is_empty():
			return {"ok": false, "reason": "invalid_content_blocks_patch"}
		var candidate := item.duplicate(true)
		candidate["content_blocks"] = next_blocks.duplicate(true)
		var candidate_validation := Contracts.validate_item(candidate)
		if not bool(candidate_validation.get("ok", false)):
			return {"ok": false, "reason": str(candidate_validation.get("reason", "invalid_content_blocks_patch"))}
		item["content_blocks"] = next_blocks.duplicate(true)
	for key in ["status", "style_token", "estimated_height"]:
		if patch.has(key):
			item[key] = patch[key]
	_items_by_id[item_id] = item
	_heights.erase(item_id)
	return {"ok": true, "kind": "patch", "item_id": item_id, "index": index_of(item_id)}


func _transition(item_id: String, lifecycle: String, status: String) -> Dictionary:
	if item_id.is_empty() or not _items_by_id.has(item_id):
		return {"ok": false, "reason": "item_not_found"}
	var item: Dictionary = _items_by_id[item_id]
	var previous := str(item.get("lifecycle", ""))
	if lifecycle == "committed" and previous == "discarded":
		return {"ok": false, "reason": "discarded_item_finalize"}
	if lifecycle == "discarded" and previous == "committed":
		return {"ok": false, "reason": "committed_item_discard"}
	var previous_index := index_of(item_id)
	item["lifecycle"] = lifecycle
	item["status"] = status
	_items_by_id[item_id] = item
	if lifecycle == "discarded" and previous_index >= 0:
		_ordered_ids.remove_at(previous_index)
	return {"ok": true, "kind": "finalize" if lifecycle == "committed" else "discard", "item_id": item_id, "index": previous_index}


func _remove(item_id: String) -> Dictionary:
	if item_id.is_empty() or not _items_by_id.has(item_id):
		return {"ok": false, "reason": "item_not_found"}
	var index := index_of(item_id)
	var item: Dictionary = _items_by_id[item_id]
	var preview_id := str((item.get("source", {}) as Dictionary).get("preview_id", ""))
	if not preview_id.is_empty():
		_preview_items.erase(preview_id)
	_items_by_id.erase(item_id)
	_heights.erase(item_id)
	if index >= 0:
		_ordered_ids.remove_at(index)
	return {"ok": true, "kind": "remove", "item_id": item_id, "index": index}


func _prepend_page(items: Array) -> Dictionary:
	var before_ids := _ordered_ids.duplicate()
	var inserted := 0
	for raw_item in items:
		var result := _insert(raw_item)
		if not bool(result.get("ok", false)):
			return result
		if not bool(result.get("duplicate", false)):
			inserted += 1
	var first_old_index := -1
	if not before_ids.is_empty():
		first_old_index = index_of(str(before_ids[0]))
	return {"ok": true, "kind": "prepend_page", "inserted": inserted, "first_old_index": first_old_index}


func _resolve_item_id(mutation: Dictionary) -> String:
	var item_id := str(mutation.get("item_id", ""))
	if not item_id.is_empty():
		return item_id
	return str(_preview_items.get(str(mutation.get("preview_id", "")), ""))


func _index_preview(item: Dictionary) -> void:
	var source: Dictionary = item.get("source", {}) if item.get("source", {}) is Dictionary else {}
	var preview_id := str(source.get("preview_id", "")).strip_edges()
	if not preview_id.is_empty():
		if _preview_items.has(preview_id) and str(_preview_items[preview_id]) != str(item.get("item_id", "")):
			return
		_preview_items[preview_id] = str(item.get("item_id", ""))


func _sort_order() -> void:
	_ordered_ids.sort_custom(func(left: String, right: String) -> bool:
		return _compare_order((_items_by_id[left] as Dictionary).get("order_key", []), (_items_by_id[right] as Dictionary).get("order_key", [])) < 0
	)


func _compare_order(left: Array, right: Array) -> int:
	for index in range(mini(left.size(), right.size())):
		var left_value := str(left[index])
		var right_value := str(right[index])
		if left_value == right_value:
			continue
		if left[index] is int and right[index] is int:
			return -1 if int(left[index]) < int(right[index]) else 1
		return -1 if left_value < right_value else 1
	if left.size() == right.size():
		return 0
	return -1 if left.size() < right.size() else 1


func _reject(mutation: Dictionary, reason: String) -> Dictionary:
	mutation_rejected.emit(mutation.duplicate(true), reason)
	return {"ok": false, "reason": reason}
