extends SceneTree

const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")


func _init() -> void:
	var ui_text := {"tool_error_detail": "出错：%s", "tool_unknown_error": "未知错误"}
	var interrupted := {"status": "error", "result": {"error": "用户中断了当前请求"}}
	if EventFormatter.format_tool_result_detail("edit_map", {}, "error", interrupted, ui_text) != "出错：用户中断了当前请求":
		push_error("historical string error was not rendered")
		quit(1)
		return
	var nested := {
		"status": "error",
		"result": {
			"error": {
				"status": "error",
				"error_code": "ground_reference_required",
				"result": {"message": "ground fill requires reference_cell"},
			}
		},
	}
	if EventFormatter.format_tool_result_detail("edit_map", {}, "error", nested, ui_text) != "出错：ground fill requires reference_cell":
		push_error("nested historical error was not rendered")
		quit(1)
		return
	var verify_unavailable := EventFormatter.describe_event({
		"type": "verify_completed",
		"payload": {
			"file_path": "res://player.gd",
			"outcome": {
				"schema_version": 1,
				"status": "unavailable",
				"phase": "semantic",
				"reason_code": "provider_timeout",
				"summary": "Verifier timed out.",
				"issues": [],
				"attempt": 1,
				"max_attempts": 2,
				"retryable": true,
				"recovery_actions": [{"action": "run_deterministic_check", "target": "res://player.gd"}],
			},
		},
	}, {})
	if not verify_unavailable.begins_with("Verify unavailable (provider_timeout):"):
		push_error("unavailable Verify outcome was not rendered")
		quit(1)
		return
	var legacy_verify := EventFormatter.describe_event({
		"type": "verify_completed",
		"payload": {"passed": true, "summary": "legacy"},
	}, {})
	if legacy_verify.contains("Verify passed"):
		push_error("legacy boolean Verify payload was projected as passed")
		quit(1)
		return
	quit()
