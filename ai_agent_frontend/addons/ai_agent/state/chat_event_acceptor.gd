@tool
extends RefCounted

## WebSocket 事件进入展示层前的唯一协议/epoch/序列验收器。

var state: RefCounted


func accept_batch(message: Dictionary) -> Dictionary:
	if state == null:
		return _problem("state_unavailable")
	var snapshot: Dictionary = state.snapshot()
	var expected_epoch := str(snapshot.get("session_epoch", ""))
	var message_epoch := str(message.get("session_epoch", ""))
	if message_epoch != expected_epoch:
		return _problem("stale_epoch")
	var raw_events: Variant = message.get("events", [])
	if not (raw_events is Array) or raw_events.is_empty():
		return _problem("invalid_batch")
	var accepted: Array = []
	var accepted_seq := int(snapshot.get("accepted_seq", 0))
	for raw_event in raw_events:
		if not (raw_event is Dictionary):
			return _problem("invalid_event")
		var event: Dictionary = raw_event
		var sequence := int(event.get("seq", 0))
		if sequence <= accepted_seq:
			continue
		if sequence != accepted_seq + 1:
			return _problem("sequence_gap")
		if str(event.get("session_epoch", "")) != expected_epoch:
			return _problem("stale_epoch")
		if not state.accept_sequence(expected_epoch, sequence):
			return _problem("sequence_rejected")
		accepted_seq = sequence
		accepted.append(event)
	return {
		"accepted": true,
		"events": accepted,
		"accepted_seq": accepted_seq,
	}


func _problem(reason: String) -> Dictionary:
	return {
		"accepted": false,
		"events": [],
		"reason": reason,
		"snapshot_required": reason in ["sequence_gap", "stale_epoch"],
	}
