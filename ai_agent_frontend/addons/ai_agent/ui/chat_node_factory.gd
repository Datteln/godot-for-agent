@tool
extends RefCounted

const MarkdownRenderer = preload("res://addons/ai_agent/ui/markdown_renderer.gd")

var log_renderer: RefCounted
var theme_colors: Dictionary = {}
var rich_text_setup: Callable


func create(data: Dictionary) -> Control:
	var kind := str(data.get("type", "log"))
	match kind:
		"message":
			return _create_message(data)
		"history_thought":
			return _create_history_thought(data)
		"history_text":
			return _create_history_text(data)
		"history_code":
			return _create_history_code(data)
		_:
			return _create_log(data)


func _create_message(data: Dictionary) -> Control:
	var role := str(data.get("role", "system"))
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
	body.add_child(log_renderer.make_rich_text(str(data.get("text", "")), data.get("color", null)))
	return row


func _create_log(data: Dictionary) -> Control:
	var container := VBoxContainer.new()
	container.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	container.add_theme_constant_override("separation", 2)
	for entry in log_renderer.split_log_entries(log_renderer.normalize_action_message(str(data.get("text", "")))):
		_append_log_entry(container, str(entry), data)
	return container


func _append_log_entry(container: VBoxContainer, entry: String, data: Dictionary) -> void:
	var kind: String = log_renderer.log_entry_kind(entry)
	var marker: String = log_renderer.workflow_marker_text(kind, bool(data.get("mark_text", false)))
	match kind:
		"thought":
			log_renderer.append_thought_entry(container, entry, marker)
		"read":
			log_renderer.append_read_entry(container, entry, marker)
		"grep":
			log_renderer.append_grep_entry(container, entry, marker)
		"edit":
			log_renderer.append_edit_entry(container, entry, marker)
		"workflow_summary":
			log_renderer.append_workflow_summary_entry(container, entry, marker)
		_:
			container.add_child(log_renderer.make_log_rich_text(entry, data.get("color", null), marker, bool(data.get("indent", false))))


func _create_history_thought(data: Dictionary) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var header := str(data.get("header", "Thought"))
	var detail := str(data.get("detail", ""))
	if detail.strip_edges() == "":
		content.add_child(log_renderer.make_log_rich_text(header, _theme_color("muted_text"), "*"))
	else:
		var toggle: Button = log_renderer.make_workflow_toggle(header, _theme_color("muted_text"))
		log_renderer.append_collapsible(content, toggle, detail, "*")
	return content


func _create_history_text(data: Dictionary) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	content.add_child(log_renderer.make_log_rich_text(
		str(data.get("text", "")),
		null,
		"*" if bool(data.get("marker", false)) else "",
		bool(data.get("indent", false))
	))
	return content


func _create_history_code(data: Dictionary) -> Control:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var rich: RichTextLabel = log_renderer.make_log_rich_text("", null, "", bool(data.get("indent", true)))
	var highlighted: Array[String] = []
	for line in str(data.get("text", "")).split("\n"):
		highlighted.append(MarkdownRenderer.highlight_code_line(str(line), str(data.get("language", "")), theme_colors))
	rich.append_text("[bgcolor=%s][code]%s[/code][/bgcolor]" % [_theme_color_tag("code_bg"), "\n".join(highlighted)])
	content.add_child(rich)
	return content


func _theme_color(name: String) -> Color:
	var value = theme_colors.get(name, Color.WHITE)
	return value if value is Color else Color.WHITE


func _theme_color_tag(name: String) -> String:
	return _theme_color(name).to_html(false)
