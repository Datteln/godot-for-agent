## 用户/助手文本条目的安全 Markdown 渲染器（任务 2.1 / 2.2 / 5.1）。
##
## - 只消费 `kind=user`/`kind=assistant` 的持久化 `payload.text`；
## - 支持的可读 Markdown 子集经 MarkdownRenderer 转 BBCode，畸形语法保持可读、
##   不产生可执行行为；
## - 超过展示预算的条目初次挂载只渲染预览 + "显示完整内容"动作，完整富文本
##   仅在用户明确点击后创建；复制始终取得完整规范文本；
## - 流式更新走增量追加（引擎自然保留已有选区）；内容不变的完成修订不重建
##   控件（无闪烁），仅当分块转换与整体转换不一致时一次性重建自愈；完整重建
##   只发生在历史挂载、展示模式切换、内容被替换或自愈时。流式光标是独立
##   Label，摘除它不触碰正文。
@tool
extends RefCounted

const MarkdownRenderer = preload("res://addons/ai_agent/ui/markdown_renderer.gd")

const META_ENTRY_ID := "transcript_entry_id"
const META_HOST := "tmr_host"
const META_RICH := "tmr_rich"
const META_CURSOR := "tmr_cursor"
const META_ROLE := "tmr_role"
const META_FULL_MODE := "tmr_full_mode"
const META_DISPLAY_COMPLETE := "tmr_display_complete"
const META_RENDERED := "tmr_rendered"
## 累计 BBCode（分块转换结果）：完成时与整体转换比对，决定是否需要自愈重建。
const META_BBCODE := "tmr_bbcode"


func create(entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> Control:
	var kind := str(entry.get("kind", ""))
	if kind != "user" and kind != "assistant":
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	root.set_meta(META_ENTRY_ID, str(entry.get("entry_id", "")))
	root.set_meta("transcript_kind", kind)
	root.set_meta("transcript_ordinal", int(entry.get("ordinal", -1)))
	root.set_meta(META_ROLE, kind)
	root.set_meta(META_DISPLAY_COMPLETE, false)
	root.set_meta(META_RENDERED, "")
	root.set_meta(META_BBCODE, "")

	var host := VBoxContainer.new()
	host.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	host.add_theme_constant_override("separation", 2)
	root.set_meta(META_HOST, host)

	if kind == "user":
		var row := HBoxContainer.new()
		row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		var spacer := Control.new()
		spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		spacer.size_flags_stretch_ratio = 0.35
		row.add_child(spacer)
		var factory: RefCounted = ctx.node_factory
		var panel: PanelContainer = factory.make_message_panel("user")
		panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		panel.size_flags_stretch_ratio = 0.65
		panel.custom_minimum_size = Vector2(320, 0)
		row.add_child(panel)
		panel.add_child(host)
		root.add_child(row)
	else:
		root.add_child(host)

	# 初次挂载决定展示模式：超过预算 → 预览；否则整条直接完整渲染。
	var text := str(ctx.payload_of(entry).get("text", ""))
	root.set_meta(META_FULL_MODE, not _is_oversized(text, ctx))
	_render_content(root, entry, ctx)
	return root


func update(root: Control, entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> void:
	if str(entry.get("kind", "")) != str(root.get_meta("transcript_kind", "")):
		return
	var old_rich := _rich_of(root)
	var had_focus := old_rich != null and is_instance_valid(old_rich) and old_rich.has_focus()
	_render_content(root, entry, ctx)
	if had_focus:
		var new_rich := _rich_of(root)
		if new_rich != null and is_instance_valid(new_rich):
			new_rich.grab_focus()


func reset(root: Control) -> void:
	var host := _host_of(root)
	if host != null:
		_disconnect_buttons(host)
		var rich := _rich_of(root)
		if rich != null and is_instance_valid(rich):
			rich.deselect()
	for key in [META_HOST, META_RICH, META_CURSOR, META_ROLE, META_FULL_MODE, META_DISPLAY_COMPLETE, META_RENDERED, META_BBCODE, "transcript_ordinal", META_ENTRY_ID, "transcript_kind"]:
		root.remove_meta(key)


# ─── 内容渲染 ────────────────────────────────────────────────────────────────


func _render_content(root: Control, entry: Dictionary, ctx: RefCounted) -> void:
	var host := _host_of(root)
	if host == null:
		return
	var payload: Dictionary = ctx.payload_of(entry)
	var text := str(payload.get("text", ""))
	var state := str(entry.get("state", ""))
	var full_mode := bool(root.get_meta(META_FULL_MODE, true))
	var rendered := str(root.get_meta(META_RENDERED, ""))
	var rich := _rich_of(root)

	if full_mode:
		if rich != null and rendered != "" and text.begins_with(rendered):
			if text == rendered:
				# 内容不变（典型为完成修订）：不重建，避免输出完成时闪烁。
				# 仅当流式分块转换与整体转换不一致（边界切断语法）时重建自愈。
				if state == "complete" and not _streamed_matches(root, text, ctx):
					_rebuild_full(root, host, text, state, ctx)
				else:
					_update_cursor(root, host, state, ctx)
				return
			# 纯追加：只 append 增量，富文本里已有选区由引擎自然保留。
			var delta_bb := MarkdownRenderer.markdown_to_bbcode(text.substr(rendered.length()), ctx.theme_colors)
			rich.append_text(delta_bb)
			root.set_meta(META_RENDERED, text)
			root.set_meta(META_BBCODE, str(root.get_meta(META_BBCODE, "")) + delta_bb)
			_update_cursor(root, host, state, ctx)
			return
		_rebuild_full(root, host, text, state, ctx)
		return

	# 预览模式：预览窗口未变化时无可见变化，不重建。
	var budget := int(ctx.display_budget_chars)
	if rich != null and rendered != "" and text.left(budget) == rendered:
		return
	_rebuild_preview(root, host, text, budget, ctx)


## 完整重建：仅用于挂载（历史加载/重挂载）、自愈、展示模式切换或内容替换。
func _rebuild_full(root: Control, host: VBoxContainer, text: String, state: String, ctx: RefCounted) -> void:
	_clear_host(host)
	var factory: RefCounted = ctx.node_factory
	var bbcode := MarkdownRenderer.markdown_to_bbcode(text, ctx.theme_colors)
	var rich: RichTextLabel = factory.make_rich_text_bbcode(bbcode)
	host.add_child(rich)
	root.set_meta(META_RICH, rich)
	root.set_meta(META_RENDERED, text)
	root.set_meta(META_BBCODE, bbcode)
	root.set_meta(META_CURSOR, null)
	_update_cursor(root, host, state, ctx)


func _rebuild_preview(root: Control, host: VBoxContainer, text: String, budget: int, ctx: RefCounted) -> void:
	_clear_host(host)
	var factory: RefCounted = ctx.node_factory
	var bbcode := MarkdownRenderer.markdown_to_bbcode(text.left(budget), ctx.theme_colors)
	var rich: RichTextLabel = factory.make_rich_text_bbcode(bbcode)
	host.add_child(rich)
	root.set_meta(META_RICH, rich)
	root.set_meta(META_RENDERED, text.left(budget))
	root.set_meta(META_BBCODE, bbcode)
	root.set_meta(META_CURSOR, null)
	host.add_child(_make_preview_actions(root, ctx, text.length(), budget))


## 完成态自愈比对：流式分块转换结果与整体转换一致则无需重建。
func _streamed_matches(root: Control, text: String, ctx: RefCounted) -> bool:
	var streamed := str(root.get_meta(META_BBCODE, ""))
	if streamed == "":
		return true
	return streamed == MarkdownRenderer.markdown_to_bbcode(text, ctx.theme_colors)


## 流式光标是独立 Label（不进富文本），追加增量与摘除光标都不会触碰正文。
func _update_cursor(root: Control, host: VBoxContainer, state: String, ctx: RefCounted) -> void:
	var cursor_value: Variant = root.get_meta(META_CURSOR) if root.has_meta(META_CURSOR) else null
	var cursor: Label = cursor_value if cursor_value is Label else null
	if state == "streaming":
		if cursor == null or not is_instance_valid(cursor):
			cursor = Label.new()
			cursor.text = "▍"
			cursor.add_theme_color_override("font_color", ctx.theme_color("muted_text"))
			host.add_child(cursor)
			root.set_meta(META_CURSOR, cursor)
		cursor.visible = true
	elif cursor != null and is_instance_valid(cursor):
		cursor.visible = false


func _make_preview_actions(root: Control, ctx: RefCounted, total_chars: int, budget: int) -> Control:
	var box := VBoxContainer.new()
	box.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	box.add_theme_constant_override("separation", 2)
	var note := Label.new()
	note.text = ctx.ui_or("content_truncated_note", "内容过长（共 %d 字），当前显示前 %d 字；复制仍取得完整内容。") % [total_chars, budget]
	note.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	note.add_theme_color_override("font_color", ctx.theme_color("muted_text"))
	box.add_child(note)
	var show_btn := Button.new()
	show_btn.text = ctx.ui_or("show_full_content", "显示完整内容")
	show_btn.flat = true
	show_btn.focus_mode = Control.FOCUS_NONE
	show_btn.alignment = HORIZONTAL_ALIGNMENT_LEFT
	show_btn.mouse_filter = Control.MOUSE_FILTER_PASS
	show_btn.add_theme_color_override("font_color", ctx.theme_color("accent_text"))
	show_btn.add_theme_color_override("font_hover_color", ctx.theme_color("hover_text"))
	show_btn.pressed.connect(_on_display_complete.bind(root, ctx))
	box.add_child(show_btn)
	return box


func _on_display_complete(root: Control, ctx: RefCounted) -> void:
	if root == null or not is_instance_valid(root):
		return
	root.set_meta(META_DISPLAY_COMPLETE, true)
	root.set_meta(META_FULL_MODE, true)
	root.set_meta(META_RENDERED, "")
	var entry: Dictionary = ctx.entry_for(str(root.get_meta(META_ENTRY_ID, "")))
	if entry.is_empty():
		return
	_render_content(root, entry, ctx)


func _is_oversized(text: String, ctx: RefCounted) -> bool:
	var budget := int(ctx.display_budget_chars)
	return budget > 0 and text.length() > budget


# ─── 辅助 ────────────────────────────────────────────────────────────────────


func _host_of(root: Control) -> VBoxContainer:
	if root.has_meta(META_HOST):
		var host: Variant = root.get_meta(META_HOST)
		if host is VBoxContainer:
			return host
	return null


func _rich_of(root: Control) -> RichTextLabel:
	if root.has_meta(META_RICH):
		var rich: Variant = root.get_meta(META_RICH)
		if rich is RichTextLabel and is_instance_valid(rich):
			return rich
	return null


func _clear_host(host: VBoxContainer) -> void:
	_disconnect_buttons(host)
	for child in host.get_children():
		host.remove_child(child)
		child.queue_free()


func _disconnect_buttons(node: Node) -> void:
	for child in node.get_children():
		if child is Button:
			var connections: Array = child.pressed.get_connections()
			for connection in connections:
				child.pressed.disconnect(connection.get("callable"))
		_disconnect_buttons(child)
