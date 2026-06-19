@tool
extends RefCounted

const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")

const HIGH_RISK_TOOLS := ["run_tests", "run_headless_self_test", "set_project_setting", "batch_rename"]

var confirm_box: Control
var _checkboxes: Array[CheckBox] = []
var _previews: Array[Control] = []
var _diff_stats: Array[Dictionary] = []
var _always_allow: CheckBox
var _apply_btn: Button
var _reject_btn: Button
var _busy := false


func show(
	message_list: VBoxContainer,
	calls: Array,
	ui_text: Dictionary,
	theme_colors: Dictionary,
	apply_callback: Callable,
	reject_callback: Callable
) -> void:
	clear_ui()

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var panel := _make_panel(theme_colors, "panel_alt_bg", "panel_alt_border")
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(panel)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 8)
	panel.add_child(body)

	var title := Label.new()
	title.text = str(ui_text.get("confirm_title", "Confirm tool calls"))
	body.add_child(title)

	for call in calls:
		if not (call is Dictionary):
			continue
		var item := HBoxContainer.new()
		item.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		var checkbox := CheckBox.new()
		checkbox.text = str(ui_text.get("apply", "Apply"))
		checkbox.button_pressed = true
		_checkboxes.append(checkbox)
		item.add_child(checkbox)
		var preview := ToolPreviewRenderer.render_call(call, theme_colors)
		preview.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		item.add_child(preview)
		_previews.append(preview)
		if ToolPreviewRenderer.infer_render_kind(call) == "diff":
			_diff_stats.append(ToolPreviewRenderer.diff_stats(call))
		else:
			_diff_stats.append({})
		body.add_child(item)
		body.add_child(HSeparator.new())

	_always_allow = CheckBox.new()
	var has_system_command := calls.any(func(call): return call is Dictionary and str(call.get("name", "")) == "run_system_command")
	_always_allow.text = str(ui_text.get("always_allow_command", "Allow this exact command in this session")) if has_system_command else str(ui_text.get("always_allow", "Always allow similar low-risk changes in this session"))
	body.add_child(_always_allow)
	_configure_session_allow(calls, str(ui_text.get("high_risk_hint", "")))

	var actions := HBoxContainer.new()
	body.add_child(actions)

	_apply_btn = Button.new()
	_apply_btn.text = str(ui_text.get("apply", "Apply"))
	_apply_btn.pressed.connect(apply_callback)
	actions.add_child(_apply_btn)

	_reject_btn = Button.new()
	_reject_btn.text = str(ui_text.get("reject", "Reject"))
	_reject_btn.pressed.connect(reject_callback)
	actions.add_child(_reject_btn)

	confirm_box = row
	message_list.add_child(row)


func clear_ui() -> void:
	if confirm_box != null and is_instance_valid(confirm_box):
		confirm_box.queue_free()
	confirm_box = null
	_checkboxes.clear()
	_previews.clear()
	_diff_stats.clear()
	_busy = false


func set_busy(value: bool) -> void:
	_busy = value
	if _apply_btn != null:
		_apply_btn.disabled = value
	if _reject_btn != null:
		_reject_btn.disabled = value


func is_busy() -> bool:
	return _busy


func should_apply(index: int) -> bool:
	return index < _checkboxes.size() and _checkboxes[index].button_pressed


func preview_for(index: int, is_workflow: bool) -> Control:
	if not is_workflow or index >= _previews.size():
		return null
	return _previews[index]


func diff_stats_for(index: int, is_workflow: bool) -> Dictionary:
	if not is_workflow or index >= _diff_stats.size():
		return {}
	return _diff_stats[index]


func grant_session_allow() -> bool:
	return _always_allow != null and _always_allow.button_pressed


func _configure_session_allow(calls: Array, high_risk_hint: String) -> void:
	if _always_allow == null:
		return
	var can_session_allow := true
	for call in calls:
		if call is Dictionary:
			var name := str(call.get("name", ""))
			var render_kind := str(call.get("render_kind", ""))
			if HIGH_RISK_TOOLS.has(name) or (render_kind == "run" and name != "run_system_command"):
				can_session_allow = false
				break
	if can_session_allow:
		_always_allow.disabled = false
		_always_allow.tooltip_text = ""
	else:
		_always_allow.button_pressed = false
		_always_allow.disabled = true
		_always_allow.tooltip_text = high_risk_hint


func _make_panel(theme_colors: Dictionary, bg_key: String, border_key: String) -> PanelContainer:
	var panel := PanelContainer.new()
	var style := StyleBoxFlat.new()
	style.bg_color = _theme_color(theme_colors, bg_key)
	style.border_color = _theme_color(theme_colors, border_key)
	style.set_border_width_all(1)
	style.set_corner_radius_all(6)
	style.content_margin_left = 10
	style.content_margin_right = 10
	style.content_margin_top = 8
	style.content_margin_bottom = 8
	panel.add_theme_stylebox_override("panel", style)
	return panel


func _theme_color(theme_colors: Dictionary, key: String) -> Color:
	var value = theme_colors.get(key)
	return value if value is Color else Color(0.16, 0.16, 0.16)
