class_name SessionTurnStateReducer
extends RefCounted

## 只归约非展示 Session/turn 状态；本类不依赖任何 UI 类型。

var state: RefCounted


func reduce(event: Dictionary) -> Dictionary:
	if state == null:
		return {"ok": false, "reason": "state_unavailable"}
	var event_type := str(event.get("type", ""))
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	match event_type:
		"tool_calls", "agent_tool_calls":
			var turn_id := str(payload.get("turn_id", ""))
			if not turn_id.is_empty():
				state.adopt_turn(turn_id)
		"final", "error":
			state.complete_turn()
		"socket_reconnecting":
			state.set_reconnecting(true)
		"socket_resumed":
			state.set_reconnecting(false)
	return {"ok": true, "snapshot": state.snapshot()}
