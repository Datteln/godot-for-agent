class_name RecoveryController
extends RefCounted

## Owns reset/new-session recovery identity until the command acknowledgement arrives.

var _previous_state: int = 0
var _previous_session_id := ""
var _pending_session_id := ""


func begin_reset(previous_state: int, previous_session_id: String, pending_session_id := "") -> void:
	_previous_state = previous_state
	_previous_session_id = previous_session_id
	_pending_session_id = pending_session_id


func failure_recovery() -> Dictionary:
	var recovery := {
		"state": _previous_state,
		"previous_session_id": _previous_session_id,
		"pending_session_id": _pending_session_id,
	}
	_pending_session_id = ""
	return recovery


func complete_reset() -> String:
	var session_id := _pending_session_id
	_previous_session_id = ""
	_pending_session_id = ""
	return session_id
