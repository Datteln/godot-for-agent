## 把 SSE 事件和工具调用/结果格式化为用户可见字符串。
## 所有方法均为静态；需要 i18n 的方法通过 ui_text 字典（已按当前语言解析好）传入。
@tool
extends Object

const _TOOL_DISPLAY_NAMES := {
	"read_file": "Read", "read_script": "Read",
	"read_class_docs": "ClassInfo", "read_class_info": "ClassInfo", "get_class_info": "ClassInfo",
	"grep_code": "Grep", "search_codebase": "Grep", "list_files": "Grep",
	"write_file": "Write",
	"propose_script_edit": "Edit", "apply_text_edit": "Edit",
	"propose_tests": "Write", "propose_content_file": "Write",
	"run_tests": "Bash", "run_headless_self_test": "Bash", "run_system_command": "Shell",
	"execute_gd_script": "Bash", "git_status": "Bash", "git_diff": "Bash", "export_project": "Bash",
	"delegate": "Task", "delegate_many": "Task",
	"search_tools": "SearchTools",
}


static func tool_display_name(name: String) -> String:
	return str(_TOOL_DISPLAY_NAMES.get(name, name))


static func is_workflow_tool(name: String) -> bool:
	var display := tool_display_name(name)
	return display == "Read" or display == "Grep" or display == "Edit" or display == "Write"


static func truncate_text(text: String, max_len: int) -> String:
	var stripped := text.strip_edges()
	if stripped.length() > max_len:
		return stripped.left(max_len) + "..."
	return stripped


static func count_lines(text: String) -> int:
	if text == "":
		return 0
	return text.split("\n").size()


static func extract_first_int_after(text: String, marker: String, fallback: int) -> int:
	var start := text.find(marker)
	if start == -1:
		return fallback
	var digits := ""
	for index in range(start + marker.length(), text.length()):
		var ch := text.substr(index, 1)
		if ch >= "0" and ch <= "9":
			digits += ch
		elif digits != "":
			break
	return int(digits) if digits != "" else fallback


static func _title_with_body(title: String, body: String) -> String:
	var stripped := body.strip_edges()
	if stripped == "":
		return title
	return "%s\n%s" % [title, stripped]


static func describe_event(event: Dictionary, ui_text: Dictionary) -> String:
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	match str(event.get("type", "")):
		"agent_step", "agent_tool_calls", "tool_calls", "final", "tool_results_received":
			return ""
		"context_usage":
			return ""
		"delegate_start":
			return _format_delegate_start(payload)
		"delegate_result":
			return _title_with_body("Delegate result: %s" % str(payload.get("agent", "delegate")), str(payload.get("summary", "")))
		"cache_hit":
			return _format_cache_hit_event(payload, ui_text)
		"agent_model_fallback":
			return ui_text.get("event_model_fallback", "Model fallback: %s -> %s") % [
				str(payload.get("primary_model", "")),
				str(payload.get("fallback_model", ""))
			]
		"server_tool_start":
			if is_workflow_tool(str(payload.get("tool", ""))):
				return ""
			return ui_text.get("event_tool_start", "%s(%s)") % [
				tool_display_name(str(payload.get("tool", ""))),
				_format_event_args(payload)
			]
		"server_tool_result":
			var tool_label := tool_display_name(str(payload.get("tool", "")))
			if bool(payload.get("is_error", false)):
				return ui_text.get("event_tool_failed", "%s failed") % tool_label
			var workflow_entry := workflow_entry_from_event_result(payload)
			if workflow_entry != "":
				return workflow_entry
			var count = payload.get("result_count")
			if count != null:
				return ui_text.get("event_tool_done_count", "%s done (%d result(s))") % [tool_label, int(count)]
			return ui_text.get("event_tool_done", "%s done") % tool_label
		"user_submitted":
			var with_context := bool(payload.get("has_context", false))
			return ui_text.get("event_user", "Message submitted%s.") % \
				(ui_text.get("event_with_context", " with project context") if with_context else "")
		"error":
			return ui_text.get("event_error", "Error: %s") % str(payload.get("text", ""))
		"reset":
			return ui_text.get("event_reset", "Session was reset.")
		"config_changed":
			var parts: Array = []
			if payload.has("effort"):
				parts.append("effort=%s" % str(payload.get("effort")))
			if payload.has("output_style"):
				parts.append("output_style=%s" % str(payload.get("output_style")))
			return ui_text.get("event_config", "Configuration changed (%s).") % ", ".join(parts)
		"compact_started":
			return ui_text.get("event_compact_started", "Compacting conversation history...")
		"compact_boundary":
			return ui_text.get("event_compact", "Compacted: %s frame(s), %s removed, %s kept, pending: %s") % [
				str(payload.get("compacted_frames", 0)),
				str(payload.get("removed_messages", 0)),
				str(payload.get("keep_recent", 0)),
				str(payload.get("pending_preserved", false))
			]
		"plan_created":
			return format_plan_created_event(payload)
		"plan_step_started":
			var start_title := "Step %d/%d started:" % [
				int(payload.get("step_index", 0)),
				int(payload.get("total_steps", 0))
			]
			var start_body := "%s (%s)" % [
				str(payload.get("title", "")),
				str(payload.get("agent", ""))
			]
			return _title_with_body(start_title, start_body)
		"plan_step_completed":
			var done_title := "Step %d/%d completed:" % [
				int(payload.get("step_index", 0)),
				int(payload.get("total_steps", 0))
			]
			return _title_with_body(done_title, str(payload.get("full_summary", payload.get("summary", ""))))
		"verify_started":
			return _title_with_body("Verify started:", "%s (%s)" % [
				str(payload.get("file_path", "")),
				str(payload.get("phase", ""))
			])
		"verify_completed":
			if bool(payload.get("passed", false)):
				return _title_with_body("Verify passed:", str(payload.get("summary", "")))
			return _title_with_body(
				"Verify found %d issue(s):" % int(payload.get("issues_count", 0)),
				str(payload.get("summary", ""))
			)
		_:
			var key_names: Array[String] = []
			for key in payload.keys():
				key_names.append(str(key))
			return ui_text.get("event_unknown", "Event: %s %s") % [str(event.get("type", "unknown")), "keys=" + ",".join(key_names)]


static func workflow_entry_from_event_result(payload: Dictionary) -> String:
	var raw_summary = payload.get("result_summary", {})
	if not (raw_summary is Dictionary):
		return ""
	var summary: Dictionary = raw_summary
	match str(summary.get("kind", "")):
		"read":
			return format_read_event_entry(summary)
		"grep":
			return format_grep_event_entry(summary)
		"edit":
			return format_edit_event_entry(summary)
		_:
			return ""


static func format_read_event_entry(summary: Dictionary) -> String:
	var path := str(summary.get("path", "<unknown>"))
	var line_start := int(summary.get("line_start", 1))
	var line_end := int(summary.get("line_end", line_start))
	return "Read %s (lines %d-%d)" % [path, line_start, max(line_start, line_end)]


static func format_grep_event_entry(summary: Dictionary) -> String:
	var pattern := str(summary.get("pattern", summary.get("query", ""))).replace("\"", "\\\"")
	var include := str(summary.get("include", "project"))
	var count := int(summary.get("match_count", 0))
	var lines: Array[String] = ["%d match%s" % [count, "" if count == 1 else "es"]]
	var matches: Array = summary.get("matches", []) if summary.get("matches", []) is Array else []
	for item in matches:
		if not (item is Dictionary):
			continue
		var line_val = item.get("line", "")
		var line_str := str(int(float(str(line_val)))) if str(line_val) != "" else ""
		lines.append("%s:%s %s" % [
			str(item.get("path", "")),
			line_str,
			str(item.get("text", "")).strip_edges()
		])
	if bool(summary.get("truncated", false)):
		lines.append("... truncated ...")
	return "Grep \"%s\" (in %s)\n%s" % [pattern, include, "\n".join(lines)]


static func format_edit_event_entry(summary: Dictionary) -> String:
	return "Edit %s\n+%d -%d lines" % [
		str(summary.get("path", "<unknown>")),
		int(summary.get("added", 0)),
		int(summary.get("removed", 0)),
	]


static func format_plan_created_event(payload: Dictionary) -> String:
	var summary := str(payload.get("summary", ""))
	var steps: Array = payload.get("steps", []) if payload.get("steps", []) is Array else []
	var lines: Array[String] = []
	if summary.strip_edges() != "":
		lines.append(summary.strip_edges())
	if not steps.is_empty() and not lines.is_empty():
		lines.append("")
	for step in steps:
		if not (step is Dictionary):
			continue
		lines.append("  %d. %s (%s)" % [
			int(step.get("index", 0)),
			str(step.get("title", "")),
			str(step.get("agent", ""))
		])
		var task := str(step.get("task", ""))
		if task != "":
			lines.append("     %s" % task)
	return _title_with_body("Plan created:", "\n".join(lines))


static func _format_event_args(payload: Dictionary) -> String:
	var raw_args = payload.get("args", {})
	var args: Dictionary = raw_args if raw_args is Dictionary else {}
	var parts: Array[String] = []
	for key in ["path", "target_path", "file_path", "script_path", "resource_path", "scene_path", "command", "kind", "agent", "task", "query"]:
		if not args.has(key):
			continue
		var value := str(args.get(key, "")).strip_edges()
		parts.append("%s=`%s`" % [key, value])
	return ", ".join(parts)


static func _format_delegate_start(payload: Dictionary) -> String:
	var raw_args = payload.get("args", {})
	var args: Dictionary = raw_args if raw_args is Dictionary else {}
	var agent := str(args.get("agent", payload.get("agent", "delegate")))
	var task := str(args.get("task", payload.get("task", ""))).strip_edges()
	var header := "Task(agent=%s)" % agent
	return header if task == "" else header + "\ntask:\n" + task


static func _format_delegate_args(payload: Dictionary) -> String:
	var args_text := _format_event_args(payload)
	if args_text != "":
		return args_text
	return str(payload.get("tool", "delegate"))


## 命中上下文缓存时生成系统消息文本；未命中（cached <= 0）时返回空串以静默。
## 不展示节省比例：百炼的实际折扣因命中类型（隐式/显式）与路由到的具体模型
## 而异，事件 payload 无法反推具体属于哪种，展示一个猜测出来的百分比只会
## 造成误导性的假精度，因此只展示 cached_tokens/total_input_tokens 这两个
## 直接来自端点 usage 的真实数字。
static func _format_cache_hit_event(payload: Dictionary, ui_text: Dictionary) -> String:
	var cached := int(payload.get("cached_tokens", 0))
	if cached <= 0:
		return ""
	var total := int(payload.get("total_input_tokens", 0))
	return ui_text.get("event_cache_hit", "Context cache hit · cached %s / %s tokens") % [
		_format_thousands(cached),
		_format_thousands(total)
	]


## 缓存命中的常驻状态栏摘要（区别于上面滚动进聊天记录的提示，这条不会随对话
## 滚走）。最近一次命中的 cached/total tokens 与命中率，用于 chat_panel 底部
## 状态行常驻展示，不需要用户翻回聊天记录确认当前缓存情况。
## cached <= 0 时返回空串（尚无命中或本轮未走缓存），调用方据此清空指示器。
static func format_context_usage_indicator(payload: Dictionary, ui_text: Dictionary) -> String:
	var used := int(payload.get("used_tokens", 0))
	var limit := int(payload.get("token_limit", 0))
	if used < 0 or limit <= 0:
		return ""
	return ui_text.get("status_context_usage", "cache %s/%s") % [
		_format_compact_tokens(used),
		_format_compact_tokens(limit)
	]


static func _format_compact_tokens(value: int) -> String:
	if value < 1000:
		return str(value)
	var compact := float(value) / 1000.0
	if value % 1000 == 0:
		return "%dk" % int(compact)
	return "%.1fk" % compact


## 把整数格式化为带千分位逗号的字符串（如 3840 -> "3,840"）。
static func _format_thousands(value: int) -> String:
	var digits := str(abs(value))
	var grouped := ""
	var count := 0
	for index in range(digits.length() - 1, -1, -1):
		grouped = digits[index] + grouped
		count += 1
		if count % 3 == 0 and index > 0:
			grouped = "," + grouped
	return ("-" + grouped) if value < 0 else grouped


static func format_tool_call_header(call: Dictionary) -> String:
	var name := str(call.get("name", "unknown"))
	var input: Dictionary = call.get("input", {}) if call.get("input") is Dictionary else {}
	var display := tool_display_name(name)
	var args := format_tool_call_args(name, input)
	var header := display if args == "" else "%s %s" % [display, args]
	var agent := str(call.get("agent", ""))
	if agent != "" and agent != "coordinator":
		header += " · `%s`" % agent
	return header


static func format_tool_call_args(name: String, input: Dictionary) -> String:
	if name == "run_tests" or name == "run_headless_self_test":
		return "kind=%s" % str(input.get("kind", "project"))
	if name == "run_system_command":
		return str(input.get("command", ""))
	for key in ["path", "target_path", "file_path", "script_path", "resource_path", "scene_path", "material_path", "track_path"]:
		if input.has(key):
			return str(input.get(key, ""))
	for key in ["pattern", "query", "include", "command", "agent", "task", "class_name", "node_path", "name", "key", "action", "group", "preset"]:
		if input.has(key):
			return str(input.get(key, ""))
	return ""


static func format_tool_result_detail(name: String, input: Dictionary, status: String, result: Dictionary, ui_text: Dictionary) -> String:
	var inner: Dictionary = result.get("result", {}) if result.get("result") is Dictionary else {}
	if status == "rejected":
		return ui_text.get("tool_rejected", "Rejected")
	if status == "error":
		var message := _tool_error_message(result, str(ui_text.get("tool_unknown_error", "Unknown error")))
		return ui_text.get("tool_error_detail", "Error: %s") % message
	match name:
		"read_file", "read_script":
			var read_lines := count_lines(str(inner.get("content", "")))
			if inner.has("has_more") and bool(inner.get("has_more", false)):
				return "%s (more available)" % (ui_text.get("tool_read_lines", "Read %s lines") % read_lines)
			return ui_text.get("tool_read_lines", "Read %s lines") % read_lines
		"apply_text_edit":
			var edited_path := str(inner.get("path", input.get("path", "")))
			var replaced_count := int(inner.get("replaced_count", 1))
			return "Edited `%s` (%d replacement(s))" % [edited_path, replaced_count]
		"write_file", "propose_script_edit", "propose_tests", "propose_content_file":
			var after_text := str(input.get("content", input.get("after_text", "")))
			var path := str(inner.get("path", input.get("path", input.get("target_path", ""))))
			return ui_text.get("tool_wrote_lines", "Wrote `%s` (%s lines)") % [path, count_lines(after_text)]
		"run_tests", "run_headless_self_test", "run_system_command", "execute_gd_script", "git_status", "git_diff", "export_project":
			var run_status := str(inner.get("status", "unknown"))
			var exit_code = inner.get("exit_code")
			var summary := run_status
			if exit_code != null:
				summary = ui_text.get("tool_run_result", "%s (exit=%s)") % [run_status, str(exit_code)]
			var output := str(inner.get("output", "")).strip_edges()
			if output != "":
				summary += "\n```\n%s\n```" % output
			return summary
		"read_debugger_errors":
			var items: Array = inner.get("items", []) if inner.get("items") is Array else []
			return ui_text.get("tool_items_count", "Returned %s item(s)") % items.size()
		"set_project_setting", "add_autoload", "remove_autoload":
			return ui_text.get("tool_done_path", "Done: `%s`") % str(inner.get("key", input.get("key", input.get("name", ""))))
		"read_project_setting":
			return ui_text.get("tool_done_path", "Done: `%s`") % str(inner.get("key", input.get("key", "")))
		"list_open_scenes":
			var scenes: Array = inner.get("open_scenes", []) if inner.get("open_scenes") is Array else []
			return ui_text.get("tool_items_count", "Returned %s item(s)") % scenes.size()
		"list_autoloads":
			var autoloads: Array = inner.get("autoloads", []) if inner.get("autoloads") is Array else []
			return ui_text.get("tool_items_count", "Returned %s item(s)") % autoloads.size()
		"list_input_actions":
			var actions: Array = inner.get("actions", []) if inner.get("actions") is Array else []
			return ui_text.get("tool_items_count", "Returned %s item(s)") % actions.size()
		"list_export_presets":
			var presets: Array = inner.get("presets", []) if inner.get("presets") is Array else []
			return ui_text.get("tool_items_count", "Returned %s item(s)") % presets.size()
		"list_groups":
			var groups: Array = inner.get("groups", []) if inner.get("groups") is Array else []
			return ui_text.get("tool_items_count", "Returned %s item(s)") % groups.size()
		"add_input_action", "remove_input_action":
			return ui_text.get("tool_done_path", "Done: `%s`") % str(inner.get("action", input.get("action", "")))
		"create_shader_material":
			return ui_text.get("tool_done_path", "Done: `%s`") % str(inner.get("material_path", input.get("material_path", "")))
		"create_animation_track":
			return ui_text.get("tool_done_path", "Done: `%s`") % str(inner.get("track_path", input.get("track_path", "")))
		_:
			if inner.has("path"):
				return ui_text.get("tool_done_path", "Done: `%s`") % str(inner.get("path"))
			return ui_text.get("tool_done", "Done")


static func _tool_error_message(value: Variant, fallback: String = "") -> String:
	if value is Dictionary:
		var data: Dictionary = value
		for key in ["message", "error", "result"]:
			if data.has(key):
				var detail := _tool_error_message(data.get(key))
				if detail != "":
					return detail
		return _tool_error_message(data.get("error_code"), fallback)
	if value == null:
		return fallback
	var text := str(value).strip_edges()
	return text if text != "" else fallback


static func format_log_tool_result(name: String, input: Dictionary, result: Dictionary, fallback: String) -> String:
	var inner: Dictionary = result.get("result", {}) if result.get("result") is Dictionary else {}
	match name:
		"read_file", "read_script":
			var read_path := str(inner.get("path", input.get("path", "<unknown>")))
			var content := str(inner.get("content", ""))
			var read_offset := int(inner.get("offset", 1))
			var read_end := read_offset + count_lines(content) - 1
			return "Read %s (lines %d-%d)" % [read_path, read_offset, max(read_end, read_offset)]
		"apply_text_edit":
			var edit_target := str(inner.get("path", input.get("path", "<unknown>")))
			var old_string := str(input.get("old_string", ""))
			var new_string := str(input.get("new_string", ""))
			var replaced := int(inner.get("replaced_count", 1))
			var edit_added := count_lines(new_string) * replaced
			var edit_removed := count_lines(old_string) * replaced
			return "Edit %s\n+%d -%d lines" % [edit_target, edit_added, edit_removed]
		"write_file", "propose_script_edit", "propose_tests", "propose_content_file":
			var edit_path := str(inner.get("path", input.get("path", input.get("target_path", "<unknown>"))))
			var after_text := str(input.get("content", input.get("after_text", "")))
			var before_text := str(input.get("before_text", input.get("before", "")))
			var added := max(count_lines(after_text) - count_lines(before_text), 0)
			var removed := max(count_lines(before_text) - count_lines(after_text), 0)
			return "Edit %s\n+%d -%d lines" % [edit_path, added, removed]
		_:
			return fallback
