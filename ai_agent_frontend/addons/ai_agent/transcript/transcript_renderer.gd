## 展示稿渲染器：只按条目 kind + typed payload 构建/更新控件（任务 4.5）。
##
## 渲染器不读取原始 HTTP/WebSocket payload，不解析 Thought 文本前缀，不做任何
## 基于显示文本的去重；条目身份与状态完全来自 Store 里的 typed entry。
## 每个 entry_id 对应一个宿主节点，revision 变化时原地重建节点内容。
@tool
extends RefCounted

const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")
const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")

const MAX_ENTRY_RENDER_CHARS := 90000

var log_renderer: RefCounted
var theme_colors: Dictionary = {}
## UI 文案函数：(key: String) -> String
var ui_text: Callable

var _message_list: VBoxContainer
var _nodes: Dictionary = {}   # entry_id -> Control
## entry_id -> bool：Thought 卡片的展开状态，跨 revision 更新保持。
var _thought_expanded: Dictionary = {}
## tool_call_id -> {preview: Control, stats: Dictionary}。
## 工具执行前预渲染的 diff 预览在此登记；条目补丁到达时优先复用，避免在
## 文件已被改写后再从磁盘读取 before_text 造成错误 diff。
var _preview_cache: Dictionary = {}


func attach(message_list: VBoxContainer) -> void:
	_message_list = message_list


## 登记某个 tool_call_id 的执行前预览（含 diff 统计），供条目渲染复用。
func register_preview(tool_call_id: String, preview: Control, stats: Dictionary) -> void:
	if tool_call_id == "" or preview == null:
		return
	_preview_cache[tool_call_id] = {"preview": preview, "stats": stats}


## 清空全部条目节点（快照替换/会话切换时调用）。
func clear_all() -> void:
	for entry_id in _nodes.keys():
		_free_node(entry_id)
	_nodes.clear()
	_thought_expanded.clear()
	for tool_call_id in _preview_cache.keys():
		var cached: Dictionary = _preview_cache[tool_call_id]
		var preview: Control = cached.get("preview")
		if preview != null and is_instance_valid(preview) and preview.get_parent() == null:
			preview.queue_free()
	_preview_cache.clear()


## 按 Store 当前顺序整体重绘。
func render_all(ordered_entries: Array) -> void:
	clear_all()
	for entry in ordered_entries:
		if entry is Dictionary:
			apply_entry(entry, false)


## 创建或原地更新单个条目节点；返回是否发生了可见变化。
func apply_entry(entry: Dictionary, scroll_hint := true) -> bool:
	var entry_id := str(entry.get("entry_id", ""))
	if entry_id == "" or _message_list == null:
		return false
	var new_node := _build_entry_node(entry)
	if new_node == null:
		return false
	var old_node: Control = _nodes.get(entry_id)
	if old_node != null and is_instance_valid(old_node):
		var index := _message_list.get_children().find(old_node)
		_message_list.remove_child(old_node)
		old_node.queue_free()
		if index < 0:
			_message_list.add_child(new_node)
		else:
			_message_list.add_child(new_node)
			_message_list.move_child(new_node, index)
	else:
		_message_list.add_child(new_node)
	_nodes[entry_id] = new_node
	return true


## 释放某条目节点（Store 快照替换后清理孤儿节点用）。
func forget_entry(entry_id: String) -> void:
	_free_node(entry_id)


func _free_node(entry_id: String) -> void:
	var node: Control = _nodes.get(entry_id)
	if node != null:
		_nodes.erase(entry_id)
		if is_instance_valid(node):
			node.queue_free()


# ─── 按 kind 构建节点 ─────────────────────────────────────────────────────────


func _build_entry_node(entry: Dictionary) -> Control:
	var kind := str(entry.get("kind", ""))
	var state := str(entry.get("state", ""))
	var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}
	match kind:
		"user":
			return _build_user_node(payload)
		"assistant":
			return _build_assistant_node(payload, state)
		"thought":
			return _build_thought_node(entry)
		"tool_activity":
			return _build_tool_activity_node(payload, state, entry)
		"approval":
			return _build_approval_node(payload, state)
		"plan":
			return _build_plan_node(payload)
		"progress":
			return _build_progress_node(payload, state)
		"verification":
			return _build_verification_node(payload, state)
		"error":
			return _build_message_panel_node("error", str(payload.get("text", "")))
		"system":
			return _build_message_panel_node("system", str(payload.get("text", "")))
		"log":
			return _build_log_node(payload)
		_:
			return null


func _build_user_node(payload: Dictionary) -> Control:
	return _build_message_panel_node("user", str(payload.get("text", "")))


func _build_message_panel_node(role: String, text: String) -> Control:
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	if role == "user":
		var spacer := Control.new()
		spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		spacer.size_flags_stretch_ratio = 0.35
		row.add_child(spacer)
	var panel: PanelContainer = log_renderer.make_message_panel(role)
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.size_flags_stretch_ratio = 0.65 if role == "user" else 1.0
	panel.custom_minimum_size = Vector2(320, 0) if role == "user" else Vector2(0, 0)
	row.add_child(panel)
	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)
	body.add_child(log_renderer.make_rich_text(_limit_text(text)))
	return row


func _build_assistant_node(payload: Dictionary, state: String) -> Control:
	var text := _limit_text(str(payload.get("text", "")))
	var rich: RichTextLabel = log_renderer.make_log_rich_text(text, null, "", true)
	if state == "streaming":
		# 流式中间态与完成态共用同一节点；仅以光标提示尚未结束。
		rich.append_text(" [color=%s]▍[/color]" % _color_tag("muted_text"))
	return rich


## 构建可折叠 Thought 卡片。折叠/展开状态记录在 `_thought_expanded`，
## 同一 entry_id 的 revision 更新重建节点时沿用该状态（任务 4.6）。
func _build_thought_node(entry: Dictionary) -> Control:
	var entry_id := str(entry.get("entry_id", ""))
	var state := str(entry.get("state", ""))
	var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}

	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	content.mouse_filter = Control.MOUSE_FILTER_PASS

	var header := _thought_header(payload, state)
	var toggle: Button = log_renderer.make_workflow_toggle(header, _theme_color("muted_text"))

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_theme_constant_override("separation", 4)
	row.mouse_filter = Control.MOUSE_FILTER_PASS

	var arrow := Label.new()
	arrow.text = ">"
	arrow.custom_minimum_size = Vector2(16, 16)
	arrow.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
	arrow.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	arrow.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	arrow.mouse_filter = Control.MOUSE_FILTER_PASS
	call_deferred("_set_arrow_pivot", arrow)
	arrow.add_theme_color_override("font_color", toggle.get_theme_color("font_color"))

	toggle.text = "✻  " + toggle.text
	toggle.size_flags_horizontal = Control.SIZE_SHRINK_BEGIN
	row.add_child(toggle)
	row.add_child(arrow)
	content.add_child(row)

	var detail_text := _limit_text(str(payload.get("content", "")))
	var detail_rich: RichTextLabel = log_renderer.make_log_rich_text(detail_text, _theme_color("muted_text"))
	var expanded: bool = bool(_thought_expanded.get(entry_id, false))
	detail_rich.visible = expanded
	arrow.rotation_degrees = 90.0 if expanded else 0.0
	content.add_child(detail_rich)

	toggle.pressed.connect(func():
		detail_rich.visible = not detail_rich.visible
		arrow.rotation_degrees = 90.0 if detail_rich.visible else 0.0
		_thought_expanded[entry_id] = detail_rich.visible
	)
	return content


## Thought 折叠头文案：思考中显示 token 计数，完成后显示耗时。
func _thought_header(payload: Dictionary, state: String) -> String:
	if state == "thinking":
		var token_count := int(payload.get("token_count", 0))
		if token_count > 0:
			return "Thinking %s Tokens" % _format_token_count(token_count)
		return "Thinking"
	var duration_value = payload.get("duration_seconds", 0.0)
	var duration := 0.0
	if duration_value is int or duration_value is float:
		duration = float(duration_value)
	return "Thought for %.2fs" % duration


func _format_token_count(count: int) -> String:
	if count < 1000:
		return str(count)
	return "%d,%03d" % [count / 1000, count % 1000]


func _set_arrow_pivot(arrow: Label) -> void:
	if arrow == null or not is_instance_valid(arrow):
		return
	arrow.pivot_offset = arrow.size / 2


func _build_tool_activity_node(payload: Dictionary, state: String, entry: Dictionary = {}) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var tool := str(payload.get("tool", ""))
	var args: Dictionary = payload.get("args", {}) if payload.get("args", {}) is Dictionary else {}
	var render_kind_value = payload.get("render_kind")
	var call := {
		"id": "",
		"name": tool,
		"input": args,
		"needs_confirm": false,
		"frame_id": "",
		"agent": str(payload.get("agent", "")),
		"render_kind": render_kind_value,
	}
	var header := EventFormatter.format_tool_call_header(call)
	var marker := _tool_marker(tool)
	var tool_call_id := str(entry.get("tool_call_id", ""))
	match state:
		"running":
			content.add_child(log_renderer.make_log_rich_text(header + " …", null, marker))
			_add_preview(content, call, state, tool_call_id)
		"resolved", "failed":
			content.add_child(log_renderer.make_log_rich_text(header, null, marker))
			_add_preview(content, call, state, tool_call_id)
			var status_text := _tool_status_text(payload, state, tool_call_id)
			if status_text != "":
				var status_color := _theme_color("error_text") if state == "failed" else _theme_color("success_text")
				content.add_child(log_renderer.make_log_rich_text(status_text, status_color))
	return content


func _tool_marker(tool: String) -> String:
	if EventFormatter.is_workflow_tool(tool):
		return "●"
	return "○"


func _add_preview(content: VBoxContainer, call: Dictionary, state: String, tool_call_id: String) -> void:
	# 优先复用执行前预渲染的预览（before_text 尚未被改写）；没有登记时仅在
	# running 状态从入参现算——resolved 状态说明文件可能已改写，此时从磁盘
	# 读 before_text 会得到错误 diff，宁可省略预览。
	var cached: Dictionary = _preview_cache.get(tool_call_id, {})
	var preview: Control = cached.get("preview")
	if preview != null and is_instance_valid(preview):
		_preview_cache.erase(tool_call_id)
		var old_parent := preview.get_parent()
		if old_parent != null:
			old_parent.remove_child(preview)
		var indent := MarginContainer.new()
		indent.add_theme_constant_override("margin_left", 24)
		indent.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		indent.add_child(preview)
		content.add_child(indent)
		return
	if state != "running":
		return
	if ToolPreviewRenderer.infer_render_kind(call) != "diff":
		return
	var fresh_preview: Control = ToolPreviewRenderer.render_call(call, theme_colors)
	if fresh_preview == null:
		return
	var indent := MarginContainer.new()
	indent.add_theme_constant_override("margin_left", 24)
	indent.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	indent.add_child(fresh_preview)
	content.add_child(indent)


func _tool_status_text(payload: Dictionary, state: String, tool_call_id: String) -> String:
	if state == "failed":
		return "✗ " + _ui_or("tool_failed", "执行失败")
	var cached: Dictionary = _preview_cache.get(tool_call_id, {})
	var stats: Dictionary = cached.get("stats", {}) if cached.get("stats", {}) is Dictionary else {}
	if not stats.is_empty():
		return "+%d -%d lines" % [int(stats.get("added", 0)), int(stats.get("removed", 0))]
	var summary_value = payload.get("result_summary")
	if summary_value is Dictionary:
		var summary: Dictionary = summary_value
		var kind := str(summary.get("kind", ""))
		if kind == "read":
			return EventFormatter.format_read_event_entry(summary)
		if kind == "grep":
			return EventFormatter.format_grep_event_entry(summary)
		if kind == "edit":
			return "+%d -%d lines" % [int(summary.get("added", 0)), int(summary.get("removed", 0))]
		if summary.has("added") or summary.has("removed"):
			return "+%d -%d lines" % [int(summary.get("added", 0)), int(summary.get("removed", 0))]
		var text := str(summary.get("text", ""))
		if text != "":
			return EventFormatter.truncate_text(text, 240)
	return "✓"


func _build_approval_node(payload: Dictionary, state: String) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var tool := str(payload.get("tool", ""))
	var args: Dictionary = payload.get("args", {}) if payload.get("args", {}) is Dictionary else {}
	var header := EventFormatter.format_tool_call_header({
		"id": "", "name": tool, "input": args, "needs_confirm": true,
		"frame_id": "", "agent": "", "render_kind": payload.get("render_kind"),
	})
	match state:
		"pending":
			content.add_child(log_renderer.make_log_rich_text(
				header + " — " + _ui_or("approval_pending", "等待确认"), null, "✋"))
		"approved":
			content.add_child(log_renderer.make_log_rich_text(header, null, "✋"))
			content.add_child(log_renderer.make_log_rich_text(
				"✓ " + _ui_or("approval_approved", "已应用"), _theme_color("success_text")))
		"rejected":
			content.add_child(log_renderer.make_log_rich_text(header, null, "✋"))
			content.add_child(log_renderer.make_log_rich_text(
				"✗ " + _ui_or("approval_rejected", "已拒绝"), _theme_color("muted_text")))
	return content


func _build_plan_node(payload: Dictionary) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var lines: Array[String] = [_ui_or("plan_created", "Plan created:")]
	var summary := str(payload.get("summary", "")).strip_edges()
	if summary != "":
		lines.append(summary)
	var steps: Array = payload.get("steps", []) if payload.get("steps", []) is Array else []
	for step in steps:
		if step is Dictionary:
			lines.append("• %s" % str(step.get("title", "")))
	content.add_child(log_renderer.make_log_rich_text("\n".join(lines), _theme_color("muted_text"), "", true))
	return content


func _build_progress_node(payload: Dictionary, state: String) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var step_index := int(payload.get("step_index", 0))
	var total_steps := int(payload.get("total_steps", 0))
	var title := str(payload.get("title", ""))
	var summary := str(payload.get("summary", ""))
	var text := ""
	if state == "complete":
		text = "Step %d/%d completed:\n%s" % [step_index, total_steps, summary if summary != "" else title]
	else:
		text = "Step %d/%d started:\n%s" % [step_index, total_steps, title]
	content.add_child(log_renderer.make_log_rich_text(text, _theme_color("muted_text"), "", true))
	return content


func _build_verification_node(payload: Dictionary, state: String) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var file_path := str(payload.get("file_path", ""))
	var phase := str(payload.get("phase", ""))
	var summary := str(payload.get("summary", ""))
	match state:
		"running":
			content.add_child(log_renderer.make_log_rich_text(
				"Verify started:\n%s (%s)" % [file_path, phase], _theme_color("muted_text"), "", true))
		"passed":
			content.add_child(log_renderer.make_log_rich_text(
				"Verify passed:\n%s" % summary, _theme_color("success_text"), "", true))
		"failed":
			content.add_child(log_renderer.make_log_rich_text(
				"Verify found %d issue(s):\n%s" % [int(payload.get("issues_count", 0)), summary],
				_theme_color("error_text"), "", true))
	return content


func _build_log_node(payload: Dictionary) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	content.add_child(log_renderer.make_log_rich_text(
		_limit_text(str(payload.get("text", ""))),
		null,
		"●" if bool(payload.get("marker", false)) else "",
		bool(payload.get("indent", false))
	))
	return content


# ─── 辅助 ─────────────────────────────────────────────────────────────────────


func _limit_text(text: String) -> String:
	if MAX_ENTRY_RENDER_CHARS <= 0 or text.length() <= MAX_ENTRY_RENDER_CHARS:
		return text
	return text.left(MAX_ENTRY_RENDER_CHARS) + "\n\n... (display truncated)"


func _theme_color(key: String) -> Color:
	var value = theme_colors.get(key)
	if value is Color:
		return value
	return Color(0.55, 0.55, 0.55)


func _color_tag(key: String) -> String:
	return "#" + _theme_color(key).to_html(false)


func _ui_or(key: String, fallback: String) -> String:
	if ui_text.is_valid():
		var value = ui_text.call(key)
		if typeof(value) == TYPE_STRING and str(value) != "" and str(value) != key:
			return str(value)
	return fallback
