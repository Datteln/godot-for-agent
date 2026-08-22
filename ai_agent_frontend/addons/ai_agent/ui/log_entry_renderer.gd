## 日志条目 UI 构建器。
## 持有 theme_colors 和 editor_interface 引用，负责创建消息/日志富文本节点；
## 不持有 message_list 自身（避免引用悬挂），由调用方传入。
##
## 注意（任务 4.2）：聊天内容一律由展示稿渲染器按 typed entry 渲染；本文件
## 不再做任何基于显示文本的拆分、`Thought:` 前缀推断或日志动作启发式分类。
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
	return make_rich_text_bbcode(MarkdownRenderer.markdown_to_bbcode(text, theme_colors), color, marker_text)


## 与 make_rich_text 相同，但接收已转换好的 BBCode（调用方保证只转换一次，
## 避免二次转义乱码）。流式渲染器需要持有该 BBCode 字符串做完成态比对。
func make_rich_text_bbcode(bbcode: String, color = null, marker_text: String = "") -> RichTextLabel:
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
	var final_bbcode := bbcode
	if marker_text != "":
		final_bbcode = "[color=%s]%s[/color]  %s" % [_marker_color_tag(marker_text), marker_text, final_bbcode]
	rich.append_text(final_bbcode)
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


func make_log_rich_text_bbcode(bbcode: String, color = null, marker_text: String = "", indent := false) -> RichTextLabel:
	var rich := make_rich_text_bbcode(bbcode, color, marker_text)
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
