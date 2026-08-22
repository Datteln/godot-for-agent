## Thought 条目渲染器（任务 2.4 / 决定 4）。
##
## - 只渲染权威展示稿明确标记的 `kind=thought` 条目；绝不从正文前缀或原始
##   传输推断；
## - `thinking` 状态显示 `Thinking {token_count} Tokens >`，`complete` 状态显示
##   `Thought for {duration_seconds}s >`；到达思考 token 预算不产生新状态；
## - 摘要始终可点击，点击只切换本条目的持久化内容展开/折叠；展开状态是本地
##   视图状态（节点 meta），revision 更新保留，卸载/重挂载后回到折叠；
## - 展开内容同样受单条展示预算约束：超出时先预览，用户点击后才完整渲染；
## - 复制与普通正文一致：展开后选中复制，或右键“复制全文”取持久化 `payload.content`。
@tool
extends RefCounted

const MarkdownRenderer = preload("res://addons/ai_agent/ui/markdown_renderer.gd")

const META_ENTRY_ID := "transcript_entry_id"
const META_EXPANDED := "tr_expanded"
const META_DETAIL_FULL := "tr_detail_full"
const META_HOST := "tr_host"
const META_TOGGLE := "tr_toggle"
const META_ARROW := "tr_arrow"
const META_LAST_CONTENT := "tr_last_content"
const META_STATE := "tr_state"
const META_D_RENDERED := "tr_rendered"
const META_D_BBCODE := "tr_bbcode"
const META_D_RICH := "tr_rich"


func create(entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> Control:
	if str(entry.get("kind", "")) != "thought":
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	root.mouse_filter = Control.MOUSE_FILTER_PASS
	root.set_meta(META_ENTRY_ID, str(entry.get("entry_id", "")))
	root.set_meta("transcript_kind", "thought")
	root.set_meta("transcript_ordinal", int(entry.get("ordinal", -1)))
	root.set_meta(META_EXPANDED, false)
	root.set_meta(META_DETAIL_FULL, false)

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_theme_constant_override("separation", 4)
	row.mouse_filter = Control.MOUSE_FILTER_PASS

	var factory: RefCounted = ctx.node_factory
	var toggle: Button = factory.make_workflow_toggle("", ctx.theme_color("muted_text"))
	toggle.size_flags_horizontal = Control.SIZE_SHRINK_BEGIN
	row.add_child(toggle)

	var arrow := Label.new()
	arrow.text = ">"
	arrow.custom_minimum_size = Vector2(16, 16)
	arrow.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
	arrow.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	arrow.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	arrow.mouse_filter = Control.MOUSE_FILTER_PASS
	arrow.add_theme_color_override("font_color", toggle.get_theme_color("font_color"))
	# 旋转轴心必须居中（首帧布局后才有尺寸，故延迟设置）；缺轴心时 90° 旋转
	# 会以左上角为支点，展开瞬间 ">" 会朝摘要文字方向跳动。
	call_deferred("_set_arrow_pivot", arrow)
	row.add_child(arrow)

	root.add_child(row)

	var host := VBoxContainer.new()
	host.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	host.add_theme_constant_override("separation", 2)
	# 注意：展开/折叠只切换 host 子节点的 visible；host 自身必须保持可见，
	# 否则展开时内容永远不显示。
	root.add_child(host)

	root.set_meta(META_TOGGLE, toggle)
	root.set_meta(META_ARROW, arrow)
	root.set_meta(META_HOST, host)
	toggle.pressed.connect(_on_toggle.bind(root))

	_apply_entry_state(root, entry, ctx)
	return root


func update(root: Control, entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> void:
	if str(entry.get("kind", "")) != "thought":
		return
	# 终态单向：已完成 Thought 不得被更晚修订的 thinking 补丁退回（决定 4）。
	if str(root.get_meta(META_STATE, "")) == "complete" and str(entry.get("state", "")) == "thinking":
		return
	_apply_entry_state(root, entry, ctx)


func reset(root: Control) -> void:
	_disconnect_buttons(root)
	for key in [META_EXPANDED, META_DETAIL_FULL, META_HOST, META_TOGGLE, META_ARROW, META_LAST_CONTENT, META_STATE, META_D_RENDERED, META_D_BBCODE, META_D_RICH, "transcript_ordinal", META_ENTRY_ID, "transcript_kind"]:
		root.remove_meta(key)


# ─── 状态应用 ────────────────────────────────────────────────────────────────


func _apply_entry_state(root: Control, entry: Dictionary, ctx: RefCounted) -> void:
	var state := str(entry.get("state", ""))
	var payload: Dictionary = ctx.payload_of(entry)
	var content := str(payload.get("content", ""))
	root.set_meta(META_STATE, state)
	root.set_meta(META_LAST_CONTENT, content)

	var toggle := _toggle_of(root)
	if toggle != null:
		toggle.text = "✻  " + _summary_text(payload, state)

	var expanded := bool(root.get_meta(META_EXPANDED, false))
	var host := _host_of(root)
	if host != null:
		# 内容始终按预算渲染（折叠只是隐藏），保证历史/重载挂载即含完整持久化内容。
		_render_detail(root, content, state, ctx)
	_apply_detail_visibility(root)
	var arrow := _arrow_of(root)
	if arrow != null:
		arrow.rotation_degrees = 90.0 if expanded else 0.0


## 摘要文案：思考中显示累计 token 数，完成后显示耗时；预算边界不改文案。
func _summary_text(payload: Dictionary, state: String) -> String:
	if state == "thinking":
		var token_count := int(payload.get("token_count", 0))
		if token_count > 0:
			return "Thinking %s Tokens" % _format_token_count(token_count)
		return "Thinking"
	var duration_value: Variant = payload.get("duration_seconds", 0.0)
	var duration := 0.0
	if duration_value is int or duration_value is float:
		duration = float(duration_value)
	return "Thought for %.2fs" % duration


func _format_token_count(count: int) -> String:
	if count < 1000:
		return str(count)
	return "%d,%03d" % [count / 1000, count % 1000]


func _render_detail(root: Control, content: String, state: String, ctx: RefCounted) -> void:
	var host := _host_of(root)
	if host == null:
		return
	var rich := _detail_rich_of(root)
	var rendered := str(root.get_meta(META_D_RENDERED, ""))
	var budget := int(ctx.display_budget_chars)
	var detail_full := bool(root.get_meta(META_DETAIL_FULL, false)) or budget <= 0 or content.length() <= budget

	if detail_full:
		if rich != null and rendered != "" and content.begins_with(rendered):
			if content == rendered:
				# 内容不变（典型为 thinking→complete）：不重建；仅当分块转换与
				# 整体转换不一致（边界切断语法）时重建自愈。
				if state == "complete" and not _detail_streamed_matches(root, content, ctx):
					_rebuild_detail(root, host, content, ctx)
				return
			# 纯追加：思考流式更新不重建控件（展开浏览时不闪烁）。
			var delta_bb := MarkdownRenderer.markdown_to_bbcode(content.substr(rendered.length()), ctx.theme_colors)
			rich.append_text(delta_bb)
			root.set_meta(META_D_RENDERED, content)
			root.set_meta(META_D_BBCODE, str(root.get_meta(META_D_BBCODE, "")) + delta_bb)
			return
		_rebuild_detail(root, host, content, ctx)
		return

	# 详情预览模式：预览窗口未变化时无可见变化，不重建。
	if rich != null and rendered != "" and content.left(budget) == rendered:
		return
	_rebuild_detail(root, host, content, ctx)


## 详情完整重建：仅用于挂载、自愈、展开完整动作或内容替换。
func _rebuild_detail(root: Control, host: VBoxContainer, content: String, ctx: RefCounted) -> void:
	_clear_children(host)
	var factory: RefCounted = ctx.node_factory
	var budget := int(ctx.display_budget_chars)
	var full := bool(root.get_meta(META_DETAIL_FULL, false))
	if budget > 0 and content.length() > budget and not full:
		var bbcode := MarkdownRenderer.markdown_to_bbcode(content.left(budget), ctx.theme_colors)
		var rich: RichTextLabel = factory.make_log_rich_text_bbcode(bbcode, ctx.theme_color("muted_text"))
		host.add_child(rich)
		root.set_meta(META_D_RICH, rich)
		root.set_meta(META_D_RENDERED, content.left(budget))
		root.set_meta(META_D_BBCODE, bbcode)
		host.add_child(_make_detail_actions(root, ctx, content.length(), budget))
		return
	var bbcode := MarkdownRenderer.markdown_to_bbcode(content, ctx.theme_colors)
	var rich: RichTextLabel = factory.make_log_rich_text_bbcode(bbcode, ctx.theme_color("muted_text"))
	host.add_child(rich)
	root.set_meta(META_D_RICH, rich)
	root.set_meta(META_D_RENDERED, content)
	root.set_meta(META_D_BBCODE, bbcode)


func _detail_streamed_matches(root: Control, content: String, ctx: RefCounted) -> bool:
	var streamed := str(root.get_meta(META_D_BBCODE, ""))
	if streamed == "":
		return true
	return streamed == MarkdownRenderer.markdown_to_bbcode(content, ctx.theme_colors)


func _detail_rich_of(root: Control) -> RichTextLabel:
	if root.has_meta(META_D_RICH):
		var value: Variant = root.get_meta(META_D_RICH)
		if value is RichTextLabel and is_instance_valid(value):
			return value
	return null


## 延迟把箭头旋转轴心设为居中；轴心缺失时旋转会使箭头位置跳动。
func _set_arrow_pivot(arrow: Label) -> void:
	if arrow == null or not is_instance_valid(arrow):
		return
	arrow.pivot_offset = arrow.size / 2


func _make_detail_actions(root: Control, ctx: RefCounted, total_chars: int, budget: int) -> Control:
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
	show_btn.pressed.connect(_on_detail_full.bind(root, ctx))
	box.add_child(show_btn)
	return box


func _on_detail_full(root: Control, ctx: RefCounted) -> void:
	if root == null or not is_instance_valid(root):
		return
	root.set_meta(META_DETAIL_FULL, true)
	var content := str(root.get_meta(META_LAST_CONTENT, ""))
	_render_detail(root, content, str(root.get_meta(META_STATE, "")), ctx)


# ─── 交互 ────────────────────────────────────────────────────────────────────


func _on_toggle(root: Control) -> void:
	if root == null or not is_instance_valid(root):
		return
	var expanded := not bool(root.get_meta(META_EXPANDED, false))
	root.set_meta(META_EXPANDED, expanded)
	_apply_detail_visibility(root)
	var arrow := _arrow_of(root)
	if arrow != null:
		arrow.rotation_degrees = 90.0 if expanded else 0.0


## 展开/折叠只切换本条目持久化内容子节点的可见性。
func _apply_detail_visibility(root: Control) -> void:
	var expanded := bool(root.get_meta(META_EXPANDED, false))
	var host := _host_of(root)
	if host == null:
		return
	for child in host.get_children():
		if child is Control:
			(child as Control).visible = expanded


# ─── 辅助 ────────────────────────────────────────────────────────────────────


func _toggle_of(root: Control) -> Button:
	if root.has_meta(META_TOGGLE):
		var value: Variant = root.get_meta(META_TOGGLE)
		if value is Button and is_instance_valid(value):
			return value
	return null


func _arrow_of(root: Control) -> Label:
	if root.has_meta(META_ARROW):
		var value: Variant = root.get_meta(META_ARROW)
		if value is Label and is_instance_valid(value):
			return value
	return null


func _host_of(root: Control) -> VBoxContainer:
	if root.has_meta(META_HOST):
		var value: Variant = root.get_meta(META_HOST)
		if value is VBoxContainer:
			return value
	return null


func _clear_children(node: Control) -> void:
	_disconnect_buttons(node)
	for child in node.get_children():
		node.remove_child(child)
		child.queue_free()


func _disconnect_buttons(node: Node) -> void:
	for child in node.get_children():
		if child is Button:
			var connections: Array = child.pressed.get_connections()
			for connection in connections:
				child.pressed.disconnect(connection.get("callable"))
		_disconnect_buttons(child)
