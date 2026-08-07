class_name ChatItemRendererRegistry
extends RefCounted

const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")
const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")
const MAX_MESSAGE_RENDER_CHARS := 90000
const MAX_REASONING_RENDER_CHARS := 30000

## Timeline item 到 Godot Control 的唯一节点工厂。

var log_renderer: RefCounted
var theme_colors: Dictionary = {}
var ui_text: Dictionary = {}


func create_item_node(item: Dictionary) -> Control:
	if log_renderer == null:
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	for raw_block in item.get("content_blocks", []):
		if raw_block is Dictionary:
			_render_block(root, item, raw_block)
	root.set_meta("timeline_item_id", str(item.get("item_id", "")))
	root.set_meta("copy_text", str(item.get("copy_text", "")))
	root.set_meta("timeline_lifecycle", str(item.get("lifecycle", "")))
	return root


func create_tool_preview_node(call: Dictionary) -> Control:
	return ToolPreviewRenderer.render_call(call, theme_colors)


func _render_block(root: VBoxContainer, item: Dictionary, block: Dictionary) -> void:
	match str(block.get("type", "")):
		"markdown", "plain_text":
			_render_text(root, item, _limited_text(str(block.get("text", "")), MAX_MESSAGE_RENDER_CHARS))
		"reasoning":
			_render_reasoning(root, item, block)
		"event":
			_render_event(root, item, block)
		"tool":
			_render_tool(root, item, block)
		"code":
			var language := str(block.get("language", ""))
			_render_text(root, item, "```%s\n%s\n```" % [language, str(block.get("text", ""))])


func _render_text(root: VBoxContainer, item: Dictionary, text: String) -> void:
	var role := str(item.get("role", "system"))
	if role == "user":
		var panel: PanelContainer = log_renderer.make_message_panel(role)
		panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		panel.add_child(log_renderer.make_rich_text(text))
		root.add_child(panel)
		return
	var color: Variant = null
	if role == "error":
		color = theme_colors.get("error_text", null)
	var marker := str(item.get("kind", "")) in ["system", "tool_call", "tool_result"]
	root.add_child(log_renderer.make_log_rich_text(text, color, log_renderer.workflow_marker_text("text", marker), str(item.get("style_token", "")) == "indented"))


func _render_reasoning(root: VBoxContainer, item: Dictionary, block: Dictionary) -> void:
	var header := str(block.get("header", "Thought"))
	var token_count := int(block.get("token_count", 0))
	if token_count > 0:
		header += " · %d tokens" % token_count
	if str(item.get("lifecycle", "")) == "committed":
		header += " ✓"
	var toggle: Button = log_renderer.make_workflow_toggle(header, theme_colors.get("muted_text", Color(0.6, 0.6, 0.6)))
	log_renderer.append_collapsible(root, toggle, _limited_text(str(block.get("text", "")), MAX_REASONING_RENDER_CHARS), "✻")


func _render_event(root: VBoxContainer, item: Dictionary, block: Dictionary) -> void:
	var event := {
		"type": str(block.get("event_type", "")),
		"payload": (block.get("payload", {}) as Dictionary).duplicate(true),
	}
	var text := EventFormatter.describe_event(event, ui_text)
	if text.is_empty():
		text = str((block.get("payload", {}) as Dictionary).get("text", ""))
	if text.is_empty():
		return
	_render_text(root, item, text)


func _render_tool(root: VBoxContainer, item: Dictionary, block: Dictionary) -> void:
	var call: Dictionary = block.get("call", {}) if block.get("call", {}) is Dictionary else {}
	var preview := ToolPreviewRenderer.render_call(call, theme_colors)
	root.add_child(preview)
	var result: Dictionary = block.get("result", {}) if block.get("result", {}) is Dictionary else {}
	var status := str(result.get("status", item.get("status", "")))
	if not status.is_empty():
		var status_color: Variant = theme_colors.get("text", null)
		if status == "error":
			status_color = theme_colors.get("error_text", null)
		elif status == "applied":
			status_color = theme_colors.get("success_text", null)
		root.add_child(log_renderer.make_log_rich_text(status, status_color))


func _limited_text(value: String, limit: int) -> String:
	if limit <= 0 or value.length() <= limit:
		return value
	return value.left(limit) + "\n\n... (display truncated)"
