## 日志条目 UI 构建器。
## 持有 theme_colors 和 editor_interface 引用，负责创建所有消息/日志节点并追加到
## message_list；不持有 message_list 自身（避免引用悬挂），由调用方传入。
@tool
extends RefCounted

const MarkdownRenderer = preload("res://addons/ai_agent/ui/markdown_renderer.gd")

var theme_colors: Dictionary
var editor_interface: EditorInterface


# ─── 颜色辅助 ────────────────────────────────────────────────────────────────

func _theme_color(key: String) -> Color:
	var value = theme_colors.get(key)
	if value is Color:
		return value
	match key:
		"text": return Color(0.875, 0.875, 0.875)
		"muted_text", "subtle_text", "marker_text": return Color(0.55, 0.55, 0.55)
		"hover_text": return Color(1, 1, 1)
		"accent_text", "marker_action": return Color(0.34, 0.62, 1.0)
		"success_text": return Color(0.35, 0.82, 0.48)
		"error_text": return Color(0.95, 0.35, 0.35)
		"code_bg": return Color(0.12, 0.12, 0.12)
		"user_panel_bg": return Color(0.15, 0.22, 0.27)
		"user_panel_border": return Color(0.27, 0.38, 0.44)
		"panel_bg": return Color(0.16, 0.16, 0.16)
		"panel_border": return Color(0.25, 0.25, 0.25)
		"error_panel_bg": return Color(0.23, 0.14, 0.14)
		"error_panel_border": return Color(0.50, 0.27, 0.27)
		_: return Color(0.16, 0.16, 0.16)


func _resolve_color(value, fallback_key: String) -> Color:
	if value is Color:
		return value
	if value is String and str(value) != "":
		return Color(str(value))
	return _theme_color(fallback_key)


func _color_tag(color: Color) -> String:
	return "#" + color.to_html(color.a < 1.0)


func _theme_color_tag(key: String) -> String:
	return _color_tag(_theme_color(key))


func _marker_color(marker_text: String) -> Color:
	return _theme_color("marker_action") if marker_text == "●" else _theme_color("marker_text")


func _marker_color_tag(marker_text: String) -> String:
	return _color_tag(_marker_color(marker_text))


# ─── 文本工具 ─────────────────────────────────────────────────────────────────

func first_line(text: String) -> String:
	var lines := text.split("\n")
	return str(lines[0]) if lines.size() > 0 else text


func rest_lines(text: String) -> String:
	var lines := text.split("\n")
	if lines.size() <= 1:
		return ""
	var rest: Array[String] = []
	for index in range(1, lines.size()):
		rest.append(str(lines[index]))
	return "\n".join(rest)


func split_thought_header(fl: String) -> Dictionary:
	var gt := fl.find(">")
	if gt != -1:
		return {"header": fl.substr(0, gt + 1).strip_edges(), "inline": fl.substr(gt + 1).strip_edges()}
	if fl.begins_with("Thought:"):
		return {"header": "Thought:", "inline": fl.substr("Thought:".length()).strip_edges()}
	return {"header": fl + " >", "inline": ""}


func log_entry_kind(entry: String) -> String:
	var fl := first_line(entry)
	if fl.begins_with("Grep "): return "grep"
	if fl.begins_with("Read "): return "read"
	if fl.begins_with("Thought ") or fl.begins_with("Thought:"): return "thought"
	if fl.begins_with("Edit "): return "edit"
	return "text"


func workflow_marker_text(kind: String, mark_text: bool = false) -> String:
	match kind:
		"thought": return "✻"
		"read", "grep", "edit": return "●"
		_: return "○" if mark_text else ""


func _format_edit_stats(detail: String) -> String:
	var stripped := detail.strip_edges()
	if stripped == "":
		return ""
	if stripped.begins_with("+") or stripped.contains(" -"):
		return stripped
	var added := _extract_first_int_after(stripped, "Added ", 0)
	var removed := _extract_first_int_after(stripped, "Removed ", 0)
	return "+%d -%d lines" % [added, removed]


func _extract_first_int_after(text: String, marker: String, fallback: int) -> int:
	var start := text.find(marker)
	if start == -1:
		return fallback
	var digits := ""
	for index in range(start + marker.length(), text.length()):
		var ch := text.substr(index, 1)
		if ch >= "0" and ch <= "9":
			digits += ch
		elif digits != "":
			break
	return int(digits) if digits != "" else fallback


# ─── 日志拆分 ─────────────────────────────────────────────────────────────────

func is_log_action_start(line: String) -> bool:
	return line.begins_with("Grep ") \
		or line.begins_with("Read ") \
		or line.begins_with("Thought ") \
		or line.begins_with("Thought:") \
		or line.begins_with("Edit ")


func normalize_action_message(text: String) -> String:
	var normalized := text.strip_edges()
	if normalized == "":
		return text
	if is_log_action_start(normalized):
		return normalized
	if normalized.contains("Wrote `"):
		return _normalize_written_result(normalized)
	if normalized.contains("**Read**"):
		return "Read %s (lines 1-EOF)" % _extract_parenthesized_action_arg(normalized)
	if normalized.contains("**Edit**") or normalized.contains("**Write**"):
		return "Edit %s" % _extract_parenthesized_action_arg(normalized)
	if normalized.contains("**Grep**") or normalized.contains("**SearchTools**"):
		return "Grep \"%s\" (in project)" % _extract_parenthesized_action_arg(normalized).replace("\"", "\\\"")
	return normalized


func _normalize_written_result(text: String) -> String:
	var start := text.find("Wrote `")
	if start == -1:
		return text
	start += "Wrote `".length()
	var end := text.find("`", start)
	var path := text.substr(start, end - start) if end > start else "<unknown>"
	var line_count := _extract_first_int_after(text, "(", 0)
	return "Edit %s\n+%d -0 lines" % [path, line_count]


func _extract_parenthesized_action_arg(text: String) -> String:
	var start := text.find("(")
	var end := text.rfind(")")
	if start == -1 or end <= start:
		return "<unknown>"
	return text.substr(start + 1, end - start - 1).strip_edges()


func split_log_entries(text: String) -> Array[String]:
	var entries: Array[String] = []
	var current: Array[String] = []
	for raw_line in text.split("\n"):
		var line := str(raw_line)
		var trimmed := line.strip_edges()
		if is_log_action_start(trimmed):
			_flush_log_entry(entries, current)
			current.clear()
			current.append(trimmed)
		elif trimmed == "" and not current.is_empty() and is_log_action_start(str(current[0])):
			_flush_log_entry(entries, current)
			current.clear()
		else:
			current.append(line)
	_flush_log_entry(entries, current)
	return entries


func _flush_log_entry(entries: Array[String], current: Array[String]) -> void:
	if current.is_empty():
		return
	var block := "\n".join(current).strip_edges()
	if block != "":
		entries.append(block)


# ─── UI 节点构建器 ────────────────────────────────────────────────────────────

func apply_mono_font(rich: RichTextLabel) -> void:
	var mono_font: Font = null
	var mono_size := 0
	if editor_interface != null:
		var editor_theme := editor_interface.get_editor_theme()
		if editor_theme != null and editor_theme.has_font("source", "EditorFonts"):
			mono_font = editor_theme.get_font("source", "EditorFonts")
		if editor_theme != null and editor_theme.has_font_size("source_size", "EditorFonts"):
			mono_size = editor_theme.get_font_size("source_size", "EditorFonts")
	if mono_font == null:
		var sys_font := SystemFont.new()
		sys_font.font_names = PackedStringArray(["Consolas", "Menlo", "Monaco", "Courier New", "monospace"])
		mono_font = sys_font
	rich.add_theme_font_override("mono_font", mono_font)
	if mono_size > 0:
		rich.add_theme_font_size_override("mono_font_size", mono_size)


func make_rich_text(text: String, color = null, marker_text: String = "") -> RichTextLabel:
	var rich := RichTextLabel.new()
	rich.bbcode_enabled = true
	rich.selection_enabled = true
	rich.context_menu_enabled = true
	rich.fit_content = true
	rich.scroll_active = false
	rich.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	rich.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	rich.add_theme_stylebox_override("normal", StyleBoxEmpty.new())
	rich.add_theme_color_override("default_color", _resolve_color(color, "text"))
	apply_mono_font(rich)
	var bbcode := MarkdownRenderer.markdown_to_bbcode(text, theme_colors)
	if marker_text != "":
		bbcode = "[color=%s]%s[/color]  %s" % [_marker_color_tag(marker_text), marker_text, bbcode]
	rich.append_text(bbcode)
	return rich


func make_log_rich_text(text: String, color = null, marker_text: String = "", indent := false) -> RichTextLabel:
	var rich := make_rich_text(text, color, marker_text)
	rich.add_theme_constant_override("line_separation", 1)
	if indent:
		var style := StyleBoxEmpty.new()
		style.content_margin_left = 48
		rich.add_theme_stylebox_override("normal", style)
	return rich


func make_workflow_toggle(text: String, color = null) -> Button:
	var toggle := Button.new()
	toggle.flat = true
	toggle.focus_mode = Control.FOCUS_NONE
	toggle.alignment = HORIZONTAL_ALIGNMENT_LEFT
	toggle.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	for state in ["normal", "hover", "pressed", "hover_pressed", "focus", "disabled"]:
		toggle.add_theme_stylebox_override(state, StyleBoxEmpty.new())
	toggle.add_theme_color_override("font_color", _resolve_color(color, "text"))
	toggle.add_theme_color_override("font_hover_color", _theme_color("hover_text"))
	toggle.text = text
	return toggle


func make_panel(bg_color = null, border_color = null) -> PanelContainer:
	var panel := PanelContainer.new()
	var style := StyleBoxFlat.new()
	style.bg_color = _resolve_color(bg_color, "panel_bg")
	style.border_color = _resolve_color(border_color, "panel_border")
	style.set_border_width_all(1)
	style.set_corner_radius_all(6)
	style.set_content_margin(SIDE_LEFT, 10)
	style.set_content_margin(SIDE_RIGHT, 10)
	style.set_content_margin(SIDE_TOP, 8)
	style.set_content_margin(SIDE_BOTTOM, 8)
	panel.add_theme_stylebox_override("panel", style)
	return panel


func make_message_panel(role: String) -> PanelContainer:
	match role:
		"user": return make_panel(_theme_color("user_panel_bg"), _theme_color("user_panel_border"))
		"error": return make_panel(_theme_color("error_panel_bg"), _theme_color("error_panel_border"))
		_: return make_panel(_theme_color("panel_bg"), _theme_color("panel_border"))


func _set_arrow_pivot(arrow: Label) -> void:
	if arrow == null:
		return
	arrow.pivot_offset = arrow.size / 2


func append_collapsible(content: VBoxContainer, toggle: Button, detail: String, marker_text: String = "") -> RichTextLabel:
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_theme_constant_override("separation", 4)

	var arrow := Label.new()
	arrow.text = ">"
	arrow.custom_minimum_size = Vector2(16, 16)
	arrow.size_flags_vertical = Control.SIZE_SHRINK_BEGIN
	arrow.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	arrow.vertical_alignment = VERTICAL_ALIGNMENT_CENTER
	call_deferred("_set_arrow_pivot", arrow)
	arrow.add_theme_color_override("font_color", toggle.get_theme_color("font_color"))

	if marker_text != "":
		toggle.text = marker_text + "  " + toggle.text
	toggle.size_flags_horizontal = Control.SIZE_SHRINK_BEGIN
	row.add_child(toggle)
	row.add_child(arrow)
	content.add_child(row)

	var detail_rich := make_log_rich_text(detail, _theme_color("muted_text"))
	detail_rich.visible = false
	content.add_child(detail_rich)

	toggle.pressed.connect(func():
		detail_rich.visible = not detail_rich.visible
		arrow.rotation_degrees = 90.0 if detail_rich.visible else 0.0
	)
	return detail_rich


# ─── 各类型日志条目追加 ───────────────────────────────────────────────────────

func append_thought_entry(content: VBoxContainer, entry: String, marker_text: String = "") -> void:
	var split := split_thought_header(first_line(entry))
	var header := str(split.get("header", ""))
	if header.ends_with(">"):
		header = header.substr(0, header.length() - 1).strip_edges()
	var inline := str(split.get("inline", ""))
	var rest := rest_lines(entry)
	var detail_parts: Array[String] = []
	if inline != "":
		detail_parts.append(inline)
	if rest != "":
		detail_parts.append(rest)
	var detail := "\n\n".join(detail_parts)
	if detail == "":
		content.add_child(make_log_rich_text(header, _theme_color("muted_text"), marker_text))
		return
	append_collapsible(content, make_workflow_toggle(header, _theme_color("muted_text")), detail, marker_text)


func append_read_entry(content: VBoxContainer, entry: String, marker_text: String = "") -> void:
	content.add_child(make_log_rich_text(first_line(entry), null, marker_text))


func append_grep_entry(content: VBoxContainer, entry: String, marker_text: String = "") -> void:
	var rest := rest_lines(entry)
	if rest == "":
		content.add_child(make_log_rich_text(first_line(entry), null, marker_text))
		return
	var rest_lines_arr := rest.split("\n")
	var summary := str(rest_lines_arr[0]).strip_edges()
	var details: Array[String] = []
	for index in range(1, rest_lines_arr.size()):
		details.append(str(rest_lines_arr[index]))
	var header := first_line(entry)
	if summary != "":
		header += " - " + summary
	if details.is_empty():
		var toggle := make_workflow_toggle(header)
		if marker_text != "":
			toggle.text = marker_text + "  " + toggle.text
		toggle.mouse_filter = Control.MOUSE_FILTER_IGNORE
		content.add_child(toggle)
		return
	append_collapsible(content, make_workflow_toggle(header), "\n".join(details), marker_text)


func append_edit_entry(content: VBoxContainer, entry: String, marker_text: String = "") -> void:
	content.add_child(make_log_rich_text(first_line(entry), null, marker_text))
	var stats := _format_edit_stats(rest_lines(entry))
	if stats != "":
		content.add_child(make_log_rich_text(stats, _theme_color("success_text")))


## 核心方法：拆分并追加一条日志流消息到 message_list。
func append_log_stream_message(message_list: VBoxContainer, text: String, color = null, mark_text: bool = false, indent: bool = false) -> void:
	for entry in split_log_entries(normalize_action_message(text)):
		_append_log_entry(message_list, str(entry), color, mark_text, indent)


func _append_log_entry(message_list: VBoxContainer, entry: String, color, mark_text: bool, indent: bool = false) -> void:
	var kind := log_entry_kind(entry)
	var marker_text := workflow_marker_text(kind, mark_text)
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	match kind:
		"thought":
			append_thought_entry(content, entry, marker_text)
		"read":
			append_read_entry(content, entry, marker_text)
		"grep":
			append_grep_entry(content, entry, marker_text)
		"edit":
			append_edit_entry(content, entry, marker_text)
		_:
			content.add_child(make_log_rich_text(entry, color, marker_text, indent))
	message_list.add_child(content)
