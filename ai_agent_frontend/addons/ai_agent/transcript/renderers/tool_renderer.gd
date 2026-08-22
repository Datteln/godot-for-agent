## 工具活动条目渲染器（任务 3.1 / 决定 6）。
##
## - 默认紧凑：动作头部 + 状态/摘要一行；大 payload 与原始结果默认折叠，
##   仅在用户展开"详情"时渲染；
## - 执行前登记的 diff 预览（before_text 未被改写时渲染）在此复用；
## - running → resolved/failed 原地更新，终态在历史重载后保持；
## - 失败且报告"文件可能已被修改"时显示警示，绝不把任务呈现为成功。
@tool
extends RefCounted

const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")
const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")

const META_ENTRY_ID := "transcript_entry_id"
const META_PREVIEW := "tool_preview"
const META_STATS := "tool_stats"
const META_DETAIL_EXPANDED := "tool_detail_expanded"
const META_DETAIL_FULL := "tool_detail_full"


func create(entry: Dictionary, ctx: RefCounted, extras: Dictionary = {}) -> Control:
	if str(entry.get("kind", "")) != "tool_activity":
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	root.set_meta(META_ENTRY_ID, str(entry.get("entry_id", "")))
	root.set_meta("transcript_kind", "tool_activity")
	root.set_meta("transcript_ordinal", int(entry.get("ordinal", -1)))
	root.set_meta(META_DETAIL_EXPANDED, false)
	root.set_meta(META_DETAIL_FULL, false)
	var preview_value: Variant = extras.get("preview")
	if preview_value is Control:
		root.set_meta(META_PREVIEW, preview_value)
	var stats_value: Variant = extras.get("stats", {})
	root.set_meta(META_STATS, stats_value if stats_value is Dictionary else {})
	_rebuild(root, entry, ctx)
	return root


func update(root: Control, entry: Dictionary, ctx: RefCounted, extras: Dictionary = {}) -> void:
	if str(entry.get("kind", "")) != "tool_activity":
		return
	if extras.has("preview") and extras.get("preview") is Control:
		root.set_meta(META_PREVIEW, extras.get("preview"))
	if extras.has("stats") and extras.get("stats") is Dictionary and not (extras.get("stats") as Dictionary).is_empty():
		root.set_meta(META_STATS, extras.get("stats"))
	_rebuild(root, entry, ctx)


func reset(root: Control) -> void:
	_disconnect_buttons(root)
	if root.has_meta(META_PREVIEW):
		var preview_value: Variant = root.get_meta(META_PREVIEW)
		if preview_value is Control and is_instance_valid(preview_value) and preview_value.get_parent() == null:
			preview_value.queue_free()
	for key in [META_PREVIEW, META_STATS, META_DETAIL_EXPANDED, META_DETAIL_FULL, "transcript_ordinal", META_ENTRY_ID, "transcript_kind"]:
		root.remove_meta(key)


# ─── 构建 ────────────────────────────────────────────────────────────────────


func _rebuild(root: Control, entry: Dictionary, ctx: RefCounted) -> void:
	# 预览节点在重建前先摘下，避免随旧子节点一起释放。
	var preview := _detach_preview(root)
	_disconnect_buttons(root)
	for child in root.get_children():
		root.remove_child(child)
		child.queue_free()

	var state := str(entry.get("state", ""))
	var payload: Dictionary = ctx.payload_of(entry)
	var tool := str(payload.get("tool", ""))
	var stats: Dictionary = {}
	if root.has_meta(META_STATS):
		var stats_value2: Variant = root.get_meta(META_STATS)
		if stats_value2 is Dictionary:
			stats = stats_value2
	var header := TranscriptCopy.tool_header(entry)
	var marker := "●" if EventFormatter.is_workflow_tool(tool) else "○"
	var factory: RefCounted = ctx.node_factory

	match state:
		"running":
			root.add_child(factory.make_log_rich_text(header + " …", null, marker))
		"resolved", "failed":
			root.add_child(factory.make_log_rich_text(header, null, marker))
			var status := TranscriptCopy.tool_status_text(entry, stats)
			if status != "":
				var status_color: Color = ctx.theme_color("error_text") if state == "failed" else ctx.theme_color("success_text")
				root.add_child(factory.make_log_rich_text(status, status_color))
			var warning := TranscriptCopy.tool_modification_warning(entry)
			if warning != "":
				root.add_child(factory.make_log_rich_text("⚠ " + warning, ctx.theme_color("error_text")))
		_:
			root.add_child(factory.make_log_rich_text(header, null, marker))

	if preview != null:
		var indent := MarginContainer.new()
		indent.add_theme_constant_override("margin_left", 24)
		indent.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		indent.add_child(preview)
		root.add_child(indent)

	var detail := _detail_text(entry)
	if detail != "":
		_build_detail_toggle(root, detail, ctx)


## 原始结果详情：默认折叠，仅保留紧凑摘要；展开动作才渲染详情。
func _detail_text(entry: Dictionary) -> String:
	var payload := ctx_payload(entry)
	var summary_value: Variant = payload.get("result_summary")
	if not (summary_value is Dictionary):
		return ""
	var summary: Dictionary = summary_value
	var text := str(summary.get("text", summary.get("message", "")))
	if text.strip_edges().length() >= 80:
		return text
	return ""


func ctx_payload(entry: Dictionary) -> Dictionary:
	var payload: Variant = entry.get("payload", {})
	return payload if payload is Dictionary else {}


func _build_detail_toggle(root: Control, detail: String, ctx: RefCounted) -> void:
	var expanded := bool(root.get_meta(META_DETAIL_EXPANDED, false))
	var factory: RefCounted = ctx.node_factory
	var toggle: Button = factory.make_workflow_toggle(ctx.ui_or("tool_details", "详情"), ctx.theme_color("muted_text"))
	toggle.size_flags_horizontal = Control.SIZE_SHRINK_BEGIN
	root.add_child(toggle)
	var host := VBoxContainer.new()
	host.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	host.visible = expanded
	root.add_child(host)
	_render_detail(host, root, detail, ctx)
	toggle.pressed.connect(_on_toggle_detail.bind(root, host, ctx))


func _on_toggle_detail(root: Control, host: VBoxContainer, ctx: RefCounted) -> void:
	if root == null or not is_instance_valid(root):
		return
	var expanded := not bool(root.get_meta(META_DETAIL_EXPANDED, false))
	root.set_meta(META_DETAIL_EXPANDED, expanded)
	if host != null and is_instance_valid(host):
		host.visible = expanded


func _render_detail(host: VBoxContainer, root: Control, detail: String, ctx: RefCounted) -> void:
	_disconnect_buttons(host)
	for child in host.get_children():
		host.remove_child(child)
		child.queue_free()
	var factory: RefCounted = ctx.node_factory
	var budget := int(ctx.display_budget_chars)
	var full := bool(root.get_meta(META_DETAIL_FULL, false))
	if budget > 0 and detail.length() > budget and not full:
		host.add_child(factory.make_log_rich_text(detail.left(budget), ctx.theme_color("muted_text"), "", true))
		var show_btn := Button.new()
		show_btn.text = ctx.ui_or("show_full_content", "显示完整内容")
		show_btn.flat = true
		show_btn.focus_mode = Control.FOCUS_NONE
		show_btn.alignment = HORIZONTAL_ALIGNMENT_LEFT
		show_btn.mouse_filter = Control.MOUSE_FILTER_PASS
		show_btn.add_theme_color_override("font_color", ctx.theme_color("accent_text"))
		show_btn.pressed.connect(_on_detail_full.bind(root, host, detail, ctx))
		host.add_child(show_btn)
		return
	host.add_child(factory.make_log_rich_text(detail, ctx.theme_color("muted_text"), "", true))


func _on_detail_full(root: Control, host: VBoxContainer, detail: String, ctx: RefCounted) -> void:
	if root == null or not is_instance_valid(root):
		return
	root.set_meta(META_DETAIL_FULL, true)
	_render_detail(host, root, detail, ctx)


func _detach_preview(root: Control) -> Control:
	# Godot 的 get_meta 即使带默认值也会对缺失键报错，必须先 has_meta。
	if not root.has_meta(META_PREVIEW):
		return null
	var preview_value: Variant = root.get_meta(META_PREVIEW)
	if not (preview_value is Control):
		return null
	var preview: Control = preview_value
	if not is_instance_valid(preview):
		root.remove_meta(META_PREVIEW)
		return null
	if preview.get_parent() != null:
		preview.get_parent().remove_child(preview)
	return preview


func _disconnect_buttons(node: Node) -> void:
	for child in node.get_children():
		if child is Button:
			var connections: Array = child.pressed.get_connections()
			for connection in connections:
				child.pressed.disconnect(connection.get("callable"))
		_disconnect_buttons(child)
