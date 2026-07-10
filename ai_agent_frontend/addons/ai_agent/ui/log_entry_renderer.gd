## 日志条目 UI 构建器。
## 持有 theme_colors 和 editor_interface 引用，负责创建所有消息/日志节点并追加到
## message_list；不持有 message_list 自身（避免引用悬挂），由调用方传入。
@tool
extends RefCounted

const MarkdownRenderer = preload("res://addons/ai_agent/ui/markdown_renderer.gd")

var theme_colors: Dictionary
var editor_interface: EditorInterface
var rich_text_setup: Callable


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


func is_workflow_summary_start(line: String) -> bool:
	if line.begins_with("Plan created"):
		return true
	if line.begins_with("Delegate results") or line.begins_with("Delegate result:"):
		return true
	if line.begins_with("Step ") and line.contains("/") and (
		line.contains(" started:") or line.contains(" completed:") or line.contains(" done:")
	):
		return true
	if line.begins_with("Executing step "):
		return true
	if line.begins_with("Verify started:") or line.begins_with("Verify passed:") or line.begins_with("Verify found "):
		return true
	return false


func log_entry_kind(entry: String) -> String:
	var fl := first_line(entry)
	if fl.begins_with("Grep "): return "grep"
	if fl.begins_with("Read "): return "read"
	if fl.begins_with("Thought ") or fl.begins_with("Thought:"): return "thought"
	if fl.begins_with("Edit "): return "edit"
	if is_workflow_summary_start(fl): return "workflow_summary"
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
		or line.begins_with("Edit ") \
		or is_workflow_summary_start(line)


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
		elif trimmed == "" and not current.is_empty() \
				and is_log_action_start(str(current[0])) \
				and not is_workflow_summary_start(str(current[0])):
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
	# RichTextLabel 默认 mouse_filter 是 STOP（因为支持选中/链接点击），这会让鼠标
	# 悬停在任意一条消息文本上时，滚轮事件被吸收在这里、传不到外层 ScrollContainer。
	# 改成 PASS：消息本身仍能响应选中/右键菜单，同时滚轮继续向上传递。
	rich.mouse_filter = Control.MOUSE_FILTER_PASS
	apply_mono_font(rich)
	var bbcode := MarkdownRenderer.markdown_to_bbcode(text, theme_colors)
	if marker_text != "":
		bbcode = "[color=%s]%s[/color]  %s" % [_marker_color_tag(marker_text), marker_text, bbcode]
	rich.append_text(bbcode)
	# Keep the source text available for the message-level copy fallback.
	rich.set_meta("copy_text", text)
	if rich_text_setup.is_valid():
		rich_text_setup.call(rich)
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
	# 同上：Button 默认 mouse_filter=STOP，悬停在 "Thought for Xs" 这类常驻可点击
	# 标题上时会拦住滚轮事件，导致 Thought 进行中无法上滑浏览历史消息。改成 PASS
	# 保留点击展开/折叠功能，同时让滚轮事件继续传给 ScrollContainer。
	toggle.mouse_filter = Control.MOUSE_FILTER_PASS
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

## 历史会话回放专用：把后端重建的 "Thought for Xs\n<完整思考正文>" 历史条目渲染成
## 与实时流式接收时同样的可折叠 "✻ Thought for Xs" 组件。
## 之所以不走 `append_log_stream_message` -> `split_log_entries` 的通用拆分逻辑：
## 思考正文是模型自由生成的自然语言，可能凑巧有某一行以 "Read "/"Edit "/"Grep "/
## "Thought:" 开头，会被那套按"工具日志动作前缀"设计的启发式拆分逻辑误判为
## 新日志条目的开始，把同一段思考拦腰截断、甚至把后半段丢给 Read/Edit 等条目
## 类型而丢弃多余内容——表现就是"历史里 Thought 内容缺失/被截断"。这里把整段
## detail 原样塞进同一个折叠组件，不做任何按行重新分类。
func append_history_thought_entry(message_list: VBoxContainer, header: String, detail: String) -> void:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	if detail.strip_edges() == "":
		content.add_child(make_log_rich_text(header, _theme_color("muted_text"), "✻"))
	else:
		append_collapsible(content, make_workflow_toggle(header, _theme_color("muted_text")), detail, "✻")
	message_list.add_child(content)


## 结构化历史文本专用。与通用文本路径不同，marker=true 明确表示行动条目，
## 因此使用与 Read/Edit 相同的蓝色实心标记，不再退化成灰色状态圆圈。
func append_history_text_entry(message_list: VBoxContainer, text: String, marker: bool, indent: bool) -> void:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	content.add_child(make_log_rich_text(text, null, "●" if marker else "", indent))
	message_list.add_child(content)


func append_history_code_entry(message_list: VBoxContainer, text: String, language: String, indent: bool) -> void:
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)
	var rich := make_log_rich_text("", null, "", indent)
	var highlighted: Array[String] = []
	for line in text.split("\n"):
		highlighted.append(MarkdownRenderer.highlight_code_line(str(line), language, theme_colors))
	rich.append_text("[bgcolor=%s][code]%s[/code][/bgcolor]" % [
		_theme_color_tag("code_bg"), "\n".join(highlighted)
	])
	content.add_child(rich)
	message_list.add_child(content)


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


func append_workflow_summary_entry(content: VBoxContainer, entry: String, marker_text: String = "") -> void:
	var header_text := first_line(entry)
	# `Delegate result(s):` 是子 agent（如 programming-agent）执行完成后的摘要块；
	# 标题行原来不带缩进，只有正文缩进 48px，导致整块的视觉宽度仍是满宽，跟其它
	# 缩进 48px 的中间文本消息不一致。这里让标题也一起缩进，使整块宽度统一。
	var header_indent := header_text.begins_with("Delegate result")
	content.add_child(make_log_rich_text(header_text, _theme_color("muted_text"), marker_text, header_indent))
	var body := rest_lines(entry).strip_edges()
	if body != "":
		content.add_child(make_log_rich_text(body, null, "", true))


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
		"workflow_summary":
			append_workflow_summary_entry(content, entry, marker_text)
		_:
			content.add_child(make_log_rich_text(entry, color, marker_text, indent))
	message_list.add_child(content)
