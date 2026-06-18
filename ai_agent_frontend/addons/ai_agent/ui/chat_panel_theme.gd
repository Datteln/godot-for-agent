@tool
extends RefCounted


static func refresh_theme_colors(owner: Control, editor_interface: EditorInterface, theme_colors: Dictionary) -> void:
	var base := _get_editor_theme_color(owner, editor_interface, "base_color", Color(0.14, 0.14, 0.14))
	var font := _get_editor_theme_color(owner, editor_interface, "font_color", Color(0.875, 0.875, 0.875))
	var accent := _get_editor_theme_color(owner, editor_interface, "accent_color", Color(0.34, 0.62, 1.0))
	var error := _get_editor_theme_color(owner, editor_interface, "error_color", Color(0.95, 0.35, 0.35))
	var success := _get_editor_theme_color(owner, editor_interface, "success_color", Color(0.35, 0.82, 0.48))
	var is_dark := base.get_luminance() < 0.5
	var contrast := Color(1, 1, 1) if is_dark else Color(0, 0, 0)
	var surface := base.lerp(contrast, 0.08 if is_dark else 0.04)
	var surface_alt := base.lerp(contrast, 0.13 if is_dark else 0.07)
	var code_bg := base.lerp(contrast, 0.16 if is_dark else 0.06)
	var muted := font.lerp(base, 0.42)
	var subtle := font.lerp(base, 0.62)
	var panel_border := surface.lerp(font, 0.22)
	var user_bg := base.lerp(accent, 0.32 if is_dark else 0.16)
	var error_bg := base.lerp(error, 0.24 if is_dark else 0.11)

	var new_colors := {
		"text": font,
		"muted_text": muted,
		"subtle_text": subtle,
		"hover_text": font.lerp(accent, 0.28),
		"panel_bg": surface,
		"panel_border": panel_border,
		"panel_alt_bg": surface_alt,
		"panel_alt_border": surface_alt.lerp(font, 0.26),
		"user_panel_bg": user_bg,
		"user_panel_border": user_bg.lerp(accent, 0.55),
		"error_panel_bg": error_bg,
		"error_panel_border": error_bg.lerp(error, 0.55),
		"error_text": error,
		"success_text": success,
		"accent_text": accent,
		"marker_text": subtle,
		"marker_action": accent,
		"code_bg": code_bg,
		"syntax_comment": _get_editor_setting_color(editor_interface, "text_editor/theme/highlighting/comment_color", Color(0.42, 0.72, 0.36) if is_dark else Color(0.25, 0.48, 0.18)),
		"syntax_string": _get_editor_setting_color(editor_interface, "text_editor/theme/highlighting/string_color", Color(0.81, 0.57, 0.47) if is_dark else Color(0.62, 0.24, 0.12)),
		"syntax_number": _get_editor_setting_color(editor_interface, "text_editor/theme/highlighting/number_color", Color(0.71, 0.81, 0.66) if is_dark else Color(0.48, 0.40, 0.08)),
		"syntax_keyword": _get_editor_setting_color(editor_interface, "text_editor/theme/highlighting/keyword_color", Color(0.34, 0.61, 0.84) if is_dark else Color(0.13, 0.36, 0.77)),
	}
	theme_colors.clear()
	theme_colors.merge(new_colors)


static func theme_color(theme_colors: Dictionary, key: String) -> Color:
	var value = theme_colors.get(key, fallback_theme_color(key))
	return value if value is Color else fallback_theme_color(key)


static func fallback_theme_color(key: String) -> Color:
	match key:
		"text": return Color(0.875, 0.875, 0.875)
		"muted_text": return Color(0.62, 0.62, 0.62)
		"subtle_text", "marker_text": return Color(0.50, 0.50, 0.50)
		"hover_text": return Color(1.0, 1.0, 1.0)
		"accent_text", "marker_action": return Color(0.34, 0.62, 1.0)
		"success_text": return Color(0.35, 0.82, 0.48)
		"error_text": return Color(0.95, 0.35, 0.35)
		"panel_bg": return Color(0.16, 0.16, 0.16)
		"panel_border": return Color(0.25, 0.25, 0.25)
		"panel_alt_bg": return Color(0.18, 0.18, 0.18)
		"panel_alt_border": return Color(0.30, 0.30, 0.30)
		"user_panel_bg": return Color(0.15, 0.22, 0.27)
		"user_panel_border": return Color(0.27, 0.38, 0.44)
		"error_panel_bg": return Color(0.23, 0.14, 0.14)
		"error_panel_border": return Color(0.50, 0.27, 0.27)
		"code_bg": return Color(0.12, 0.12, 0.12)
		"syntax_comment": return Color(0.42, 0.72, 0.36)
		"syntax_string": return Color(0.81, 0.57, 0.47)
		"syntax_number": return Color(0.71, 0.81, 0.66)
		"syntax_keyword": return Color(0.34, 0.61, 0.84)
		_: return Color(0.16, 0.16, 0.16)


static func set_button_text_colors(button: Button, font_color: Color, hover_color: Color) -> void:
	button.add_theme_color_override("font_color", font_color)
	button.add_theme_color_override("font_hover_color", hover_color)


static func _get_editor_theme_color(
	owner: Control,
	editor_interface: EditorInterface,
	name: String,
	fallback: Color
) -> Color:
	var editor_theme: Theme = null
	if editor_interface != null:
		editor_theme = editor_interface.get_editor_theme()
	if editor_theme != null and editor_theme.has_color(name, "Editor"):
		return editor_theme.get_color(name, "Editor")
	if owner != null and owner.has_theme_color(name, "Editor"):
		return owner.get_theme_color(name, "Editor")
	return fallback


static func _get_editor_setting_color(editor_interface: EditorInterface, path: String, fallback: Color) -> Color:
	if editor_interface == null:
		return fallback
	var settings := editor_interface.get_editor_settings()
	if settings == null or not settings.has_setting(path):
		return fallback
	var value = settings.get_setting(path)
	return value if value is Color else fallback
