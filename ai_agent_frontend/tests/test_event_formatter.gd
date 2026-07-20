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
	quit()
