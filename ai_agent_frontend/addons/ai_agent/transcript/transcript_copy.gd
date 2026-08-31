## 规范复制文本适配器（任务 1.2 / 决定 3）。
##
## 复制值永远来自条目的持久化 payload：用户/助手取 `payload.text`，Thought 取
## `payload.content`，审批解决态取由 `operation_summary`/`affected_paths`/
## `resolution_summary` 生成的一行权限结果文本。复制不受展示截断/预览状态影响，
## 也不读取 RichTextLabel 的 BBCode、节点名称或 UI metadata。
## 纯文本构建函数同时被复制与渲染复用，保证"所见即可复制"。
@tool
extends RefCounted

const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")

## 字段客观不存在时的显式标注；绝不从 UI 状态或原始传输猜测。
const UNAVAILABLE := "未提供"

## 受影响路径白名单键（兼容旧条目的 typed args 回退）。
const PATH_KEYS := ["path", "target_path", "file_path", "script_path", "resource_path", "scene_path", "material_path", "track_path", "directory"]


static func payload_of(entry: Dictionary) -> Dictionary:
	var payload: Variant = entry.get("payload", {})
	return payload if payload is Dictionary else {}


## 条目的规范复制文本：与展示一致的可读文本，不含任何渲染标记。
static func canonical_text(entry: Dictionary) -> String:
	var kind := str(entry.get("kind", ""))
	var state := str(entry.get("state", ""))
	var payload := payload_of(entry)
	match kind:
		"user", "assistant", "system", "log":
			return str(payload.get("text", ""))
		"thought":
			return str(payload.get("content", ""))
		"error":
			return error_plain_text(payload)
		"approval":
			if is_approval_resolved(state):
				return approval_result_line(payload)
			return approval_pending_line(payload)
		"tool_activity":
			return tool_plain_text(entry)
		"plan":
			return plan_plain_text(payload)
		"progress":
			return progress_plain_text(payload, state)
		"verification":
			return verification_plain_text(payload, state)
		_:
			return ""


static func has_copy(entry: Dictionary) -> bool:
	return canonical_text(entry) != ""


static func is_approval_resolved(state: String) -> bool:
	return state == "approved" or state == "rejected" or state == "error"


## 解决态审批的一行权限结果文本，例如 `已确认：修改 res://player.gd`。
## 优先使用持久化的 operation_summary/affected_paths/resolution_summary；
## 旧条目缺失时仅回退到同为持久化 typed 字段的 tool/args/decision，
## 仍不可得时显式标注"未提供"。
static func approval_result_line(payload: Dictionary) -> String:
	if str(payload.get("decision", "")) == "error":
		var error_summary := str(payload.get("error_summary", "")).strip_edges()
		if error_summary != "":
			return "%s：%s — %s" % [
				approval_resolution_text(payload),
				approval_operation_text(payload),
				error_summary,
			]
	return "%s：%s %s" % [
		approval_resolution_text(payload),
		approval_operation_text(payload),
		approval_paths_text(payload),
	]


static func approval_pending_line(payload: Dictionary) -> String:
	return "%s：%s %s" % [
		"等待确认",
		approval_operation_text(payload),
		approval_paths_text(payload),
	]


static func approval_resolution_text(payload: Dictionary) -> String:
	var resolution := str(payload.get("resolution_summary", "")).strip_edges()
	if resolution != "":
		return resolution
	match str(payload.get("decision", "")):
		"approved":
			return "已确认"
		"rejected":
			return "已拒绝"
		"error":
			var code := str(payload.get("error_code", "")).strip_edges()
			return "执行失败（%s）" % code if code != "" else "执行失败"
		_:
			return UNAVAILABLE


static func approval_operation_text(payload: Dictionary) -> String:
	var operation := str(payload.get("operation_summary", "")).strip_edges()
	if operation != "":
		return operation
	var tool := str(payload.get("tool", "")).strip_edges()
	if tool != "":
		return tool
	return UNAVAILABLE


static func approval_paths_text(payload: Dictionary) -> String:
	var paths_value: Variant = payload.get("affected_paths", [])
	var raw_paths: Array = paths_value if paths_value is Array else []
	if raw_paths.is_empty():
		# 旧条目回退：仍只读取持久化 typed args，不从 UI/原始传输猜测。
		var args_value: Variant = payload.get("args", {})
		var args: Dictionary = args_value if args_value is Dictionary else {}
		for key in PATH_KEYS:
			if str(args.get(key, "")).strip_edges() != "":
				raw_paths.append(args.get(key))
	var items: Array[String] = []
	for path_value in raw_paths:
		var path_text := str(path_value).strip_edges()
		if path_text != "" and not items.has(path_text):
			items.append(path_text)
	if items.is_empty():
		return UNAVAILABLE
	if items.size() <= 3:
		return " ".join(items)
	# 路径过多：稳定的紧凑列表 + 数量摘要。
	return "%s 等 %d 个路径" % [" ".join(items.slice(0, 2)), items.size()]


## 工具条目的紧凑纯文本表示（头部 + 状态行）。
static func tool_plain_text(entry: Dictionary, diff_stats: Dictionary = {}) -> String:
	var lines: Array[String] = [tool_header(entry)]
	var status := tool_status_text(entry, diff_stats)
	if status != "":
		lines.append(status)
	var warning := tool_modification_warning(entry)
	if warning != "":
		lines.append(warning)
	return "\n".join(lines)


static func tool_header(entry: Dictionary) -> String:
	var payload := payload_of(entry)
	if str(payload.get("tool", "")) == "read_class_docs":
		var args: Dictionary = payload.get("args", {}) if payload.get("args", {}) is Dictionary else {}
		return "ClassInfo " + str(args.get("class_name", ""))
	var args_value: Variant = payload.get("args", {})
	var call := {
		"id": str(entry.get("tool_call_id", "")),
		"name": str(payload.get("tool", "")),
		"input": args_value if args_value is Dictionary else {},
		"needs_confirm": false,
		"frame_id": "",
		"agent": str(payload.get("agent", "")),
		"render_kind": payload.get("render_kind"),
	}
	return EventFormatter.format_tool_call_header(call)


static func tool_status_text(entry: Dictionary, diff_stats: Dictionary = {}) -> String:
	var state := str(entry.get("state", ""))
	var payload := payload_of(entry)
	if state == "failed":
		return "✗ 执行失败"
	if state != "resolved":
		return ""
	if str(payload.get("outcome_status", "")) == "rejected":
		return "已拒绝"
	if not diff_stats.is_empty():
		return "+%d -%d lines" % [int(diff_stats.get("added", 0)), int(diff_stats.get("removed", 0))]
	var summary_value: Variant = payload.get("result_summary")
	if summary_value is Dictionary:
		var summary: Dictionary = summary_value
		var kind := str(summary.get("kind", ""))
		if kind == "read":
			return EventFormatter.format_read_event_entry(summary)
		if kind == "grep":
			return EventFormatter.format_grep_event_entry(summary)
		if kind == "edit" or summary.has("added") or summary.has("removed"):
			return "+%d -%d lines" % [int(summary.get("added", 0)), int(summary.get("removed", 0))]
		var text := str(summary.get("text", ""))
		if text != "":
			return EventFormatter.truncate_text(text, 240)
	return "✓"


## 失败工具条目报告"文件可能已被修改"时的警示文本；无报告则为空。
static func tool_modification_warning(entry: Dictionary) -> String:
	if str(entry.get("state", "")) != "failed":
		return ""
	var payload := payload_of(entry)
	var summary_value: Variant = payload.get("result_summary")
	if summary_value is Dictionary:
		var summary: Dictionary = summary_value
		if bool(summary.get("possible_modifications", false)):
			return "注意：部分文件可能已被修改，本次操作未成功"
		var message := str(summary.get("message", "")) + str(summary.get("text", ""))
		if message.contains("可能已被修改") or message.contains("may already have changed"):
			return "注意：部分文件可能已被修改，本次操作未成功"
	return ""


static func plan_plain_text(payload: Dictionary) -> String:
	var lines: Array[String] = ["Plan created:"]
	var summary := str(payload.get("summary", "")).strip_edges()
	if summary != "":
		lines.append(summary)
	var steps_value: Variant = payload.get("steps", [])
	var steps: Array = steps_value if steps_value is Array else []
	for step in steps:
		if step is Dictionary:
			lines.append("• %s" % str(step.get("title", "")))
	return "\n".join(lines)


static func progress_plain_text(payload: Dictionary, state: String) -> String:
	var step_index := int(payload.get("step_index", 0))
	var total_steps := int(payload.get("total_steps", 0))
	var title := str(payload.get("title", ""))
	var summary := str(payload.get("summary", ""))
	if state == "complete":
		return "Step %d/%d completed:\n%s" % [step_index, total_steps, summary if summary != "" else title]
	return "Step %d/%d started:\n%s" % [step_index, total_steps, title]


static func verification_plain_text(payload: Dictionary, state: String) -> String:
	var file_path := str(payload.get("file_path", ""))
	var phase := str(payload.get("phase", ""))
	var summary := str(payload.get("summary", ""))
	match state:
		"running":
			return "Verify started:\n%s (%s)" % [file_path, phase]
		"passed":
			return "Verify passed:\n%s" % summary
		"failed":
			return "Verify found %d issue(s):\n%s" % [int(payload.get("issues_count", 0)), summary]
		_:
			return ""


## 错误条目的完整可读文本：操作上下文 + 原因 + 已知修改状态。
static func error_plain_text(payload: Dictionary) -> String:
	var lines: Array[String] = []
	var context := str(payload.get("context", "")).strip_edges()
	if context != "":
		lines.append("操作：%s" % context)
	lines.append(str(payload.get("text", "")))
	var modification := str(payload.get("modification_status", "")).strip_edges()
	if modification != "":
		lines.append("修改状态：%s" % modification)
	return "\n".join(lines)
