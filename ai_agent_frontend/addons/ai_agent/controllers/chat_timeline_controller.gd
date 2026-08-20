class_name ChatTimelineController
extends RefCounted

const Projector = preload("res://addons/ai_agent/timeline/chat_timeline_projector.gd")
const Store = preload("res://addons/ai_agent/timeline/chat_timeline_store.gd")

signal projection_rejected(reason: String, event: Dictionary)

var projector := Projector.new()
var store := Store.new()
var _epoch := ""
var _local_order := 0
var _stamp_counter := 0
var _high_water_seq := 0
var _stamped_keys := {}
var _reserved_stamp: Array = []


func reset_epoch(epoch: String) -> bool:
	_epoch = epoch
	_local_order = 0
	_stamp_counter = 0
	_high_water_seq = 0
	_stamped_keys = {}
	_reserved_stamp = []
	return bool(store.apply_mutation({"kind": "reset_epoch", "session_epoch": epoch}).get("ok", false))


func present_event(event: Dictionary) -> bool:
	var seq := int(event.get("seq", 0))
	if seq > _high_water_seq:
		_high_water_seq = seq
	var projection := projector.project(event, _epoch)
	if not bool(projection.get("ok", false)):
		projection_rejected.emit(str(projection.get("reason", "projection_failed")), event.duplicate(true))
		return false
	var mutations: Array = projection.get("mutations", [])
	for mutation_value in mutations:
		if mutation_value is Dictionary and str((mutation_value as Dictionary).get("kind", "")) == "insert":
			_stamp_item((mutation_value as Dictionary).get("item", {}))
	# boundary 事件（commit/discard）的 preview_ids 可能含个别已失效条目
	# （比如先被移除的 tool 条目），逐条应用而非整批原子：单条失败只提示，
	# 不能拖垮同批其它 finalize（否则 reasoning 会永远停留在 streaming 状态）。
	for mutation_value in mutations:
		if not (mutation_value is Dictionary):
			continue
		var result := store.apply_mutation(mutation_value as Dictionary)
		if not bool(result.get("ok", false)):
			projection_rejected.emit(str(result.get("reason", "mutation_failed")), event.duplicate(true))
	return true


func prepend_history(records: Array) -> bool:
	var items: Array = []
	for raw_record in records:
		if not (raw_record is Dictionary):
			projection_rejected.emit("invalid_history_record", {})
			return false
		var record: Dictionary = raw_record
		var seq := int(record.get("seq", 0))
		if seq > _high_water_seq:
			_high_water_seq = seq
		var projection := projector.project(record, _epoch)
		if not bool(projection.get("ok", false)):
			projection_rejected.emit(str(projection.get("reason", "history_projection_failed")), record.duplicate(true))
			return false
		for mutation in projection.get("mutations", []):
			if str(mutation.get("kind", "")) == "insert":
				_stamp_item(mutation.get("item", {}))
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
		"order_key": [_high_water_seq, 1_000_000 + _local_order, 0],
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


## 移除本地临时条目（如乐观用户气泡），并把它的 order_key 保留给下一个
## insert（通常是 user_submitted 回声的 canonical 条目），使对账后位置不变。
func promote_local_to_next_insert(local_item_id: String) -> bool:
	var item := store.item_by_id(local_item_id)
	if item.is_empty():
		return false
	_reserved_stamp = (item.get("order_key", []) as Array).duplicate(true)
	return bool(store.apply_mutation({"kind": "remove", "item_id": local_item_id}).get("ok", false))


func remove_item(item_id: String) -> bool:
	return bool(store.apply_mutation({"kind": "remove", "item_id": item_id}).get("ok", false))


## 把非整数首元素的 order_key（投影器对消息/工具条目的字符串 frame 键）
## stamp 进单一整数序列空间；同一 item_id 的后续重复 insert 复用同一键。
func _stamp_item(raw_item: Variant) -> void:
	if not (raw_item is Dictionary):
		return
	var item: Dictionary = raw_item
	var item_id := str(item.get("item_id", ""))
	if item_id.is_empty():
		return
	if _stamped_keys.has(item_id):
		item["order_key"] = (_stamped_keys[item_id] as Array).duplicate(true)
		return
	var key: Array = (item.get("order_key", []) as Array).duplicate(true)
	if key.is_empty() or not (key[0] is int):
		var third := 0
		if key.size() >= 3 and (key[2] is int):
			third = int(key[2])
		if _reserved_stamp.is_empty():
			_stamp_counter += 1
			key = [_high_water_seq, _stamp_counter, third]
		else:
			key = _reserved_stamp.duplicate(true)
			_reserved_stamp = []
	_stamped_keys[item_id] = key.duplicate(true)
	item["order_key"] = key

## 中断边界收尾：把指定 turn 内所有非终态的 provisional 工具条目标记为
## interrupted，禁止任何工具块永久停留 pending。
func interrupt_pending_tools(turn_id: String) -> int:
	var resolved := 0
	var index := 0
	while index < store.size():
		var item := store.get_item(index)
		index += 1
		if str(item.get("kind", "")) != "tool_result":
			continue
		var source: Dictionary = item.get("source", {}) if item.get("source", {}) is Dictionary else {}
		if str(source.get("turn_id", "")) != turn_id:
			continue
		var status := str(item.get("status", ""))
		if status in ["applied", "error", "interrupted", "discarded", "complete"]:
			continue
		var result := store.apply_mutation({
			"kind": "patch",
			"item_id": str(item.get("item_id", "")),
			"patch": {"status": "interrupted"},
		})
		if bool(result.get("ok", false)):
			resolved += 1
	return resolved