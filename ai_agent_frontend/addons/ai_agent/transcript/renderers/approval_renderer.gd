## 审批条目渲染器（任务 3.2 / 决定 6）。
##
## - `pending`（唯一可操作态）显示卡片：动作头部 + 等待确认标记；确认/拒绝
##   控件由确认宿主在审批挂起期间提供，其余任何状态都不出现确认控件；
## - `approved`/`rejected` 一律降级为同一根节点下的一行普通权限结果文本
##   （如 `已确认：修改 res://player.gd`），无卡片边框、按钮或可展开详情；
## - 单行文本只由持久化的 operation_summary/affected_paths/resolution_summary
##   生成；缺失字段显式标注"未提供"，绝不从 UI 状态或原始传输猜测；
## - 历史重载/重连/复用重挂载呈现与实时解决后完全相同的一行文本形态。
@tool
extends RefCounted

const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")

const META_ENTRY_ID := "transcript_entry_id"
const META_PREVIEW := "approval_preview"
const META_STATS := "approval_stats"


func create(entry: Dictionary, ctx: RefCounted, extras: Dictionary = {}) -> Control:
	if str(entry.get("kind", "")) != "approval":
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	root.set_meta(META_ENTRY_ID, str(entry.get("entry_id", "")))
	root.set_meta("transcript_kind", "approval")
	root.set_meta("transcript_ordinal", int(entry.get("ordinal", -1)))
	var preview_value: Variant = extras.get("preview")
	if preview_value is Control:
		root.set_meta(META_PREVIEW, preview_value)
	var stats_value: Variant = extras.get("stats", {})
	root.set_meta(META_STATS, stats_value if stats_value is Dictionary else {})
	_rebuild(root, entry, ctx)
	return root


func update(root: Control, entry: Dictionary, ctx: RefCounted, extras: Dictionary = {}) -> void:
	if str(entry.get("kind", "")) != "approval":
		return
	if extras.has("preview") and extras.get("preview") is Control:
		root.set_meta(META_PREVIEW, extras.get("preview"))
	_rebuild(root, entry, ctx)


func reset(root: Control) -> void:
	_disconnect_buttons(root)
	if root.has_meta(META_PREVIEW):
		var preview_value: Variant = root.get_meta(META_PREVIEW)
		if preview_value is Control and is_instance_valid(preview_value) and preview_value.get_parent() == null:
			preview_value.queue_free()
	for key in [META_PREVIEW, META_STATS, "transcript_ordinal", META_ENTRY_ID, "transcript_kind"]:
		root.remove_meta(key)


func _rebuild(root: Control, entry: Dictionary, ctx: RefCounted) -> void:
	var preview := _detach_preview(root)
	_disconnect_buttons(root)
	for child in root.get_children():
		root.remove_child(child)
		child.queue_free()

	var state := str(entry.get("state", ""))
	var payload: Dictionary = ctx.payload_of(entry)
	var factory: RefCounted = ctx.node_factory

	if TranscriptCopy.is_approval_resolved(state):
		# 解决态：一行非交互文本节点，无卡片边框/按钮/可展开详情。
		var line := TranscriptCopy.approval_result_line(payload)
		var color_key := "success_text" if state == "approved" else "muted_text"
		root.add_child(factory.make_log_rich_text(line, ctx.theme_color(color_key)))
		return

	# pending（可操作态）：卡片展示待审批动作；确认/拒绝控件在确认宿主中。
	var panel: PanelContainer = factory.make_panel(ctx.theme_color("panel_alt_bg"), ctx.theme_color("panel_alt_border"))
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_child(panel)
	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 2)
	panel.add_child(body)
	var header := TranscriptCopy.tool_header(entry)
	body.add_child(factory.make_log_rich_text(
		header + " — " + ctx.ui_or("approval_pending", "等待确认"), null, "✋"
	))
	if preview != null:
		var indent := MarginContainer.new()
		indent.add_theme_constant_override("margin_left", 24)
		indent.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		indent.add_child(preview)
		body.add_child(indent)


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
