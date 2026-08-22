## 错误条目渲染器（任务 3.3）。
##
## 展示失败的操作/任务上下文、用户可读原因与已知修改状态；重试按钮仅在
## 持久化 payload 明确声明 `retryable=true` 时出现，绝不因文案或 UI 推断。
@tool
extends RefCounted

const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")


func create(entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> Control:
	if str(entry.get("kind", "")) != "error":
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	root.set_meta("transcript_entry_id", str(entry.get("entry_id", "")))
	root.set_meta("transcript_kind", "error")
	root.set_meta("transcript_ordinal", int(entry.get("ordinal", -1)))
	_rebuild(root, entry, ctx)
	return root


func update(root: Control, entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> void:
	if str(entry.get("kind", "")) != "error":
		return
	_rebuild(root, entry, ctx)


func reset(root: Control) -> void:
	_disconnect_buttons(root)
	for key in ["transcript_ordinal", "transcript_entry_id", "transcript_kind"]:
		root.remove_meta(key)


func _rebuild(root: Control, entry: Dictionary, ctx: RefCounted) -> void:
	_disconnect_buttons(root)
	for child in root.get_children():
		root.remove_child(child)
		child.queue_free()
	var payload: Dictionary = ctx.payload_of(entry)
	var factory: RefCounted = ctx.node_factory
	var panel: PanelContainer = factory.make_message_panel("error")
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_child(panel)
	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 2)
	panel.add_child(body)
	var context := str(payload.get("context", "")).strip_edges()
	if context != "":
		body.add_child(factory.make_log_rich_text("操作：%s" % context, ctx.theme_color("muted_text")))
	body.add_child(factory.make_log_rich_text(str(payload.get("text", "")), ctx.theme_color("error_text")))
	var modification := str(payload.get("modification_status", "")).strip_edges()
	if modification != "":
		body.add_child(factory.make_log_rich_text("修改状态：%s" % modification, ctx.theme_color("error_text")))
	if payload.get("retryable") == true:
		var retry_btn := Button.new()
		retry_btn.text = ctx.ui_or("retry", "重试")
		retry_btn.focus_mode = Control.FOCUS_NONE
		var entry_id := str(entry.get("entry_id", ""))
		retry_btn.pressed.connect(_on_retry.bind(entry_id, ctx))
		body.add_child(retry_btn)


func _on_retry(entry_id: String, ctx: RefCounted) -> void:
	if ctx.retry_entry.is_valid():
		ctx.retry_entry.call(entry_id)


func _disconnect_buttons(node: Node) -> void:
	for child in node.get_children():
		if child is Button:
			var connections: Array = child.pressed.get_connections()
			for connection in connections:
				child.pressed.disconnect(connection.get("callable"))
		_disconnect_buttons(child)
