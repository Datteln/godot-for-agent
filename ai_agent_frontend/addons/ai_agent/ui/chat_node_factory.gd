@tool
extends RefCounted

var log_renderer: RefCounted
var theme_colors: Dictionary = {}
var rich_text_setup: Callable


func create(data: Dictionary) -> Control:
	var kind := str(data.get("type", "log"))
	var node: Control
	match kind:
		"message":
			node = _create_message(data)
		_:
			node = _create_log(data)
	if node != null:
		node.set_meta("copy_text", _copy_text_for(data))
	return node


func _copy_text_for(data: Dictionary) -> String:
	match str(data.get("type", "log")):
		"log", "message":
			return str(data.get("text", ""))
		_:
			return ""


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
	var normalized: String = log_renderer.normalize_action_message(str(data.get("text", "")))
	var entries: Array[String] = []
	if bool(data.get("single_entry", false)):
		entries.append(normalized)
	else:
		entries.assign(log_renderer.split_log_entries(normalized))
	for entry in entries:
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
