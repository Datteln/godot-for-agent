@tool
extends Node

signal changed(state: Dictionary)

const AgentEventLog = preload("res://addons/ai_agent/state/agent_event_log.gd")

var state := {
	"session_id": "default",
	"session_epoch": "",
	"state": "idle",
	"current_turn_id": "",
	"pending_calls": [],
	"last_event_seq": 0,
	"event_log": [],
	"doctor_warnings": [],
	"recovery_pointer": null,
	"effort": "standard",
	"output_style": "default"
}

var _log := AgentEventLog.new()


func set_value(key: String, value: Variant) -> void:
	state[key] = value
	changed.emit(state.duplicate(true))


func merge(values: Dictionary) -> void:
	for key in values.keys():
		state[key] = values[key]
	changed.emit(state.duplicate(true))


func add_event(event: Dictionary) -> void:
	add_events([event])


func add_events(new_events: Array) -> void:
	var last_seq := int(state.get("last_event_seq", 0))
	var changed_events := false
	for event in new_events:
		if not (event is Dictionary):
			continue
		_log.append(event)
		last_seq = max(last_seq, int(event.get("seq", 0)))
		changed_events = true
	if not changed_events:
		return
	state["event_log"] = _log.events.duplicate(true)
	state["last_event_seq"] = last_seq
	changed.emit(state.duplicate(true))


func reset(session_epoch: String = "", last_event_seq: int = 0) -> void:
	_log.clear()
	state["state"] = "idle"
	state["session_epoch"] = session_epoch
	state["current_turn_id"] = ""
	state["pending_calls"] = []
	state["last_event_seq"] = last_event_seq
	state["event_log"] = []
	state["recovery_pointer"] = null
	changed.emit(state.duplicate(true))
