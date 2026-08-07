class_name ChatStreamingController
extends RefCounted

## Owns accepted-event coalescing and the frame-budgeted presentation queue.

var _queue: Array = []


func has_pending() -> bool:
	return not _queue.is_empty()


func clear() -> void:
	_queue.clear()


func enqueue(event: Dictionary) -> void:
	_queue.append(event)
	_queue.sort_custom(func(a, b):
		if not (a is Dictionary) or not (b is Dictionary):
			return false
		return int(a.get("seq", 0)) < int(b.get("seq", 0))
	)


func take_batch(maximum: int, time_budget_ms: int) -> Array:
	var batch: Array = []
	var started_ms := Time.get_ticks_msec()
	while not _queue.is_empty() and batch.size() < maximum:
		batch.append(_queue.pop_front())
		if Time.get_ticks_msec() - started_ms >= time_budget_ms:
			break
	return batch


func coalesce(events: Array) -> Array:
	var result: Array = []
	var latest_delta := {}
	var ordered_delta_keys: Array[String] = []
	for raw_event in events:
		if not (raw_event is Dictionary):
			continue
		var event: Dictionary = raw_event
		var event_type := str(event.get("type", ""))
		if event_type == "agent_reasoning_delta" or event_type == "agent_text_delta":
			var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
			if bool(payload.get("append_delta", false)):
				_flush_delta_events(result, latest_delta, ordered_delta_keys)
				result.append(event)
				continue
			_remember_delta_event(event, latest_delta, ordered_delta_keys)
			continue
		_flush_delta_events(result, latest_delta, ordered_delta_keys)
		result.append(event)
	_flush_delta_events(result, latest_delta, ordered_delta_keys)
	return result


func _remember_delta_event(
	event: Dictionary,
	latest_delta: Dictionary,
	ordered_delta_keys: Array[String]
) -> void:
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	var key := "%s:%s:%s" % [
		str(event.get("type", "")),
		str(payload.get("frame_id", "")),
		str(payload.get("message_index", ""))
	]
	if not latest_delta.has(key):
		ordered_delta_keys.append(key)
	latest_delta[key] = event


func _flush_delta_events(
	result: Array,
	latest_delta: Dictionary,
	ordered_delta_keys: Array[String]
) -> void:
	for key in ordered_delta_keys:
		if latest_delta.has(key):
			result.append(latest_delta[key])
	latest_delta.clear()
	ordered_delta_keys.clear()
