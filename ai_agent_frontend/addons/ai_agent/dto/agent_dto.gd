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


## 将执行器返回值规范化为可提交的工具结果，避免缺少关联字段时触发 HTTP 422。
static func normalize_execution_result(tool_call: Dictionary, candidate: Variant) -> Dictionary:
	if candidate is Dictionary:
		var result: Dictionary = candidate
		var status := str(result.get("status", ""))
		if (
			str(result.get("tool_use_id", "")) != ""
			and str(result.get("frame_id", "")) != ""
			and status in ["applied", "rejected", "error"]
		):
			return result
	var received_keys: Array[String] = []
	if candidate is Dictionary:
		for key in (candidate as Dictionary).keys():
			received_keys.append(str(key))
	var fallback := error_result(
		tool_call,
		"The frontend produced an incomplete tool result; no project change is assumed.",
		"front_tool_result_protocol_invalid"
	)
	fallback["result"] = {
		"message": "The frontend produced an incomplete tool result; no project change is assumed.",
		"received_keys": received_keys,
		"tool": str(tool_call.get("name", "")),
	}
	return fallback


static func rejected_result(tool_call: Dictionary) -> Dictionary:
	var payload := tool_result(
		str(tool_call.get("id", "")),
		str(tool_call.get("frame_id", "")),
		"rejected",
		{"message": "User rejected this tool call."},
		"user_rejected"
	)
	payload["decision_source"] = "explicit_reject"
	return payload
