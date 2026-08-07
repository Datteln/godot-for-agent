@tool
extends RefCounted

signal changed(snapshot: Dictionary)
signal reset_adopted(previous_epoch: String, new_epoch: String)

var _session_id := ""
var _session_epoch := ""
var _active_turn_id := ""
var _accepted_seq := 0
var _acknowledged_seq := 0
var _resetting := false
var _reconnecting := false
var _suppressing := false


func configure(session_id: String, session_epoch: String, initial_seq: int = 0) -> void:
	_session_id = session_id
	_session_epoch = session_epoch
	_active_turn_id = ""
	_accepted_seq = max(initial_seq, 0)
	_acknowledged_seq = _accepted_seq
	_resetting = false
	_reconnecting = false
	_suppressing = false
	_emit_changed()


func begin_submission() -> bool:
	if not can_submit():
		return false
	_active_turn_id = "pending"
	_emit_changed()
	return true


func adopt_turn(turn_id: String) -> void:
	_active_turn_id = turn_id
	_emit_changed()


func complete_turn() -> void:
	_active_turn_id = ""
	_emit_changed()


func begin_reset() -> bool:
	if _resetting:
		return false
	_resetting = true
	_suppressing = true
	_emit_changed()
	return true


func adopt_reset(new_session_id: String, new_epoch: String, last_event_seq: int) -> void:
	var previous_epoch := _session_epoch
	_session_id = new_session_id
	_session_epoch = new_epoch
	_active_turn_id = ""
	_accepted_seq = max(last_event_seq, 0)
	_acknowledged_seq = _accepted_seq
	_resetting = false
	_reconnecting = true
	_suppressing = false
	reset_adopted.emit(previous_epoch, new_epoch)
	_emit_changed()


func accept_sequence(epoch: String, sequence: int) -> bool:
	if _resetting or _suppressing or epoch != _session_epoch:
		return false
	if sequence != _accepted_seq + 1:
		return false
	_accepted_seq = sequence
	_emit_changed()
	return true


func acknowledge(sequence: int) -> bool:
	if sequence < _acknowledged_seq or sequence > _accepted_seq:
		return false
	_acknowledged_seq = sequence
	_emit_changed()
	return true


func set_reconnecting(value: bool) -> void:
	_reconnecting = value
	_emit_changed()


func set_suppressing(value: bool) -> void:
	_suppressing = value
	_emit_changed()


func can_submit() -> bool:
	return not _resetting and not _suppressing and _active_turn_id.is_empty()


func snapshot() -> Dictionary:
	return {
		"session_id": _session_id,
		"session_epoch": _session_epoch,
		"active_turn_id": _active_turn_id,
		"accepted_seq": _accepted_seq,
		"acknowledged_seq": _acknowledged_seq,
		"resetting": _resetting,
		"reconnecting": _reconnecting,
		"suppressing": _suppressing,
		"can_submit": can_submit(),
	}


func _emit_changed() -> void:
	changed.emit(snapshot())
