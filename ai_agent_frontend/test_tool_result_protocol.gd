## 工具结果提交前的协议兜底测试：残缺执行结果必须转为可继续对话的 error 结果。
extends SceneTree

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const ClassDBReader = preload("res://addons/ai_agent/context/classdb_reader.gd")
const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")

var _failures := 0
var _checks := 0


func _init() -> void:
	var call := {"id": "call-42", "frame_id": "frame-7", "name": "propose_script_edit"}
	var malformed: Dictionary = AgentDTO.normalize_execution_result(call, {"decision_source": "execute"})
	_check(str(malformed.get("tool_use_id", "")) == "call-42", "fallback keeps tool-use identity")
	_check(str(malformed.get("frame_id", "")) == "frame-7", "fallback keeps frame identity")
	_check(str(malformed.get("status", "")) == "error", "fallback is an error result")
	_check(str(malformed.get("error_code", "")) == "front_tool_result_protocol_invalid", "fallback is typed")
	var complete := {
		"tool_use_id": "call-42",
		"frame_id": "frame-7",
		"status": "applied",
		"result": {"ok": true},
	}
	var normalized_complete := AgentDTO.normalize_execution_result(call, complete)
	_check(normalized_complete == complete, "complete result passes through unchanged")
	(complete["result"] as Dictionary)["write_applied"] = false
	_check(bool((normalized_complete.get("result", {}) as Dictionary).get("write_applied", true)), "normalized result preserves an independent typed payload snapshot")
	var overview := ClassDBReader.query_class_info({"class_name": "Node", "mode": "overview"})
	_check(bool(overview.get("ok", false)), "ClassDB overview succeeds")
	_check(not overview.has("methods"), "overview never enumerates ClassDB methods")
	var rejected := ClassDBReader.query_class_info({"class_name": "Node", "mode": "members", "members": PackedStringArray(["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"])})
	_check(str(rejected.get("error_code", "")) == "class_docs_query_too_large", "oversized class query is typed")
	var class_entry := {"kind": "tool_activity", "payload": {"tool": "read_class_docs", "args": {"class_name": "TileMap"}}}
	_check(TranscriptCopy.tool_header(class_entry) == "ClassInfo TileMap", "ClassInfo title is exact")
	print("tool result protocol checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)
