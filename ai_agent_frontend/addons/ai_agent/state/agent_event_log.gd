@tool
extends RefCounted

var events: Array[Dictionary] = []
var max_events := 500


func append(event: Dictionary) -> void:
	events.append(event)
	if events.size() > max_events:
		events = events.slice(events.size() - max_events)


func clear() -> void:
	events.clear()
