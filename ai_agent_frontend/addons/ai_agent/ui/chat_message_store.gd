@tool
extends RefCounted

var _messages: Array[Dictionary] = []
var _prefix_heights := PackedFloat32Array()
var _heights_dirty := true
var item_spacing := 0.0


func add_message(data: Dictionary) -> int:
	_messages.append(_normalize_message(data, _messages.size()))
	_heights_dirty = true
	return _messages.size() - 1


func append_messages(messages: Array) -> void:
	for data in messages:
		if data is Dictionary:
			_messages.append(_normalize_message(data, _messages.size()))
	_heights_dirty = true


func prepend_messages(messages: Array) -> float:
	var normalized: Array[Dictionary] = []
	var added_height := 0.0
	for data in messages:
		if data is Dictionary:
			var message := _normalize_message(data, normalized.size())
			normalized.append(message)
			var measured := float(message.get("measured_height", 0.0))
			added_height += (measured if measured > 1.0 else float(message.get("estimated_height", 64.0))) + item_spacing
	if normalized.is_empty():
		return 0.0
	_messages = normalized + _messages
	for index in range(_messages.size()):
		_messages[index]["index"] = index
	_heights_dirty = true
	return added_height


func _normalize_message(data: Dictionary, index: int) -> Dictionary:
	var message := data.duplicate(true)
	message["index"] = index
	message["estimated_height"] = maxf(1.0, float(message.get("estimated_height", 64.0)))
	message["measured_height"] = maxf(0.0, float(message.get("measured_height", 0.0)))
	return message


func get_message(index: int) -> Dictionary:
	if index < 0 or index >= _messages.size():
		return {}
	return _messages[index]


func update_message(index: int, data: Dictionary) -> void:
	if index < 0 or index >= _messages.size():
		return
	var message: Dictionary = _messages[index]
	for key in data:
		message[key] = data[key]
	message["index"] = index
	message["estimated_height"] = maxf(1.0, float(message.get("estimated_height", 64.0)))
	_heights_dirty = true


func get_range(from_index: int, to_index: int) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for index in range(maxi(0, from_index), mini(to_index, _messages.size())):
		result.append(_messages[index])
	return result


func size() -> int:
	return _messages.size()


func update_height(index: int, height: float) -> void:
	if index < 0 or index >= _messages.size() or height <= 0.0:
		return
	_messages[index]["measured_height"] = height
	_heights_dirty = true


func height_at(index: int) -> float:
	if index < 0 or index >= _messages.size():
		return 0.0
	var message := _messages[index]
	var measured := float(message.get("measured_height", 0.0))
	var height := measured if measured > 1.0 else float(message.get("estimated_height", 64.0))
	return height + item_spacing


func total_height(from_index: int = 0, to_index: int = -1, excluded: Dictionary = {}) -> float:
	var end := _messages.size() if to_index < 0 else mini(to_index, _messages.size())
	var total := 0.0
	for index in range(maxi(0, from_index), end):
		if not excluded.has(index):
			total += height_at(index)
	return total


func find_index_at_scroll(y: float) -> int:
	if _messages.is_empty():
		return 0
	_rebuild_prefix_heights()
	var low := 0
	var high := _messages.size() - 1
	while low < high:
		var middle := int((low + high) / 2)
		if _prefix_heights[middle] < y:
			low = middle + 1
		else:
			high = middle
	return low


func clear() -> void:
	_messages.clear()
	_prefix_heights = PackedFloat32Array()
	_heights_dirty = false


func remove_message(index: int) -> void:
	if index < 0 or index >= _messages.size():
		return
	_messages.remove_at(index)
	_heights_dirty = true
	for next_index in range(index, _messages.size()):
		_messages[next_index]["index"] = next_index


func move_message(index: int, target_index: int) -> int:
	if index < 0 or index >= _messages.size():
		return index
	var message := _messages[index]
	_messages.remove_at(index)
	var destination := clampi(target_index, 0, _messages.size())
	_messages.insert(destination, message)
	for next_index in range(_messages.size()):
		_messages[next_index]["index"] = next_index
	_heights_dirty = true
	return destination


func _rebuild_prefix_heights() -> void:
	if not _heights_dirty:
		return
	_prefix_heights = PackedFloat32Array()
	_prefix_heights.resize(_messages.size())
	var total := 0.0
	for index in range(_messages.size()):
		total += height_at(index)
		_prefix_heights[index] = total
	_heights_dirty = false
