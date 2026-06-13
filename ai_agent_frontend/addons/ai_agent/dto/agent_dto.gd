@tool
extends RefCounted


static func tool_result(
	tool_use_id: String,
	frame_id: String,
	status: String,
	result: Variant = null,
	error_code: String = "",
	artifact_refs: Array = [],
	grant_session_allow: bool = false
) -> Dictionary:
	var payload := {
		"tool_use_id": tool_use_id,
		"frame_id": frame_id,
		"turn_id": "",
		"status": status,
		"result": result,
		"artifact_refs": artifact_refs,
		"grant_session_allow": grant_session_allow
	}
	if error_code != "":
		payload["error_code"] = error_code
	return payload


static func error_result(tool_call: Dictionary, message: String, code: String = "front_tool_error") -> Dictionary:
	return tool_result(
		str(tool_call.get("id", "")),
		str(tool_call.get("frame_id", "")),
		"error",
		{"message": message},
		code
	)


static func rejected_result(tool_call: Dictionary) -> Dictionary:
	return tool_result(
		str(tool_call.get("id", "")),
		str(tool_call.get("frame_id", "")),
		"rejected",
		{"message": "User rejected this tool call."},
		"user_rejected"
	)
