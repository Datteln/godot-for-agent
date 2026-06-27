@tool
extends Window

signal confirmed(results: Array)
signal rejected(results: Array)

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")

## 即使勾选了"本会话内自动允许"，下列高风险工具/渲染类型仍必须每次手动确认。
const HIGH_RISK_TOOLS := ["run_tests", "run_headless_self_test", "set_project_setting", "batch_rename", "open_scene", "add_autoload", "remove_autoload", "add_input_action", "remove_input_action"]

var tool_executor: Node
var _calls: Array = []
var _checkboxes: Array[CheckBox] = []
var _list: VBoxContainer
var _apply_btn: Button
var _reject_btn: Button
var _always_allow: CheckBox
var _busy := false


func _ready() -> void:
	title = "AI Tool Preview"
	close_requested.connect(_on_reject)

	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(root)

	_list = VBoxContainer.new()
	var scroll := ScrollContainer.new()
	scroll.custom_minimum_size = Vector2(720, 420)
	scroll.add_child(_list)
	root.add_child(scroll)

	_always_allow = CheckBox.new()
	_always_allow.text = "Always allow similar low-risk changes in this session"
	root.add_child(_always_allow)

	var row := HBoxContainer.new()
	root.add_child(row)
	_apply_btn = Button.new()
	_apply_btn.text = "Apply"
	_apply_btn.pressed.connect(_on_apply)
	row.add_child(_apply_btn)
	_reject_btn = Button.new()
	_reject_btn.text = "Reject"
	_reject_btn.pressed.connect(_on_reject)
	row.add_child(_reject_btn)


func show_calls(calls: Array) -> void:
	if visible:
		push_warning("Preview panel is already showing a pending batch.")
		return
	_calls = calls.duplicate(true)
	_checkboxes.clear()
	for child in _list.get_children():
		child.queue_free()
	for call in _calls:
		if not (call is Dictionary):
			continue
		var row := HBoxContainer.new()
		var checkbox := CheckBox.new()
		checkbox.button_pressed = true
		checkbox.text = "Apply"
		_checkboxes.append(checkbox)
		row.add_child(checkbox)
		row.add_child(ToolPreviewRenderer.render_call(call))
		_list.add_child(row)
		_list.add_child(HSeparator.new())
	_configure_session_allow()
	_set_busy(false)
	popup_centered(Vector2i(760, 560))


func _on_apply() -> void:
	if _busy:
		return
	_set_busy(true)
	var results: Array = []
	for index in range(_calls.size()):
		var call = _calls[index]
		if not (call is Dictionary):
			continue
		var apply := index < _checkboxes.size() and _checkboxes[index].button_pressed
		if apply and tool_executor != null:
			var result: Dictionary = await tool_executor.execute(call)
			result = _ensure_tool_result_for_call(call, result)
			result["grant_session_allow"] = _always_allow.button_pressed
			results.append(result)
		else:
			results.append(AgentDTO.rejected_result(call))
	_set_busy(false)
	hide()
	confirmed.emit(results)


func _on_reject() -> void:
	if _busy:
		return
	var results: Array = []
	for call in _calls:
		if call is Dictionary:
			results.append(AgentDTO.rejected_result(call))
	hide()
	rejected.emit(results)


func _ensure_tool_result_for_call(call: Dictionary, result: Dictionary) -> Dictionary:
	for key in ["tool_use_id", "frame_id", "status"]:
		if str(result.get(key, "")).strip_edges() == "":
			return AgentDTO.error_result(
				call,
				"Tool executor returned an invalid result without required metadata.",
				"invalid_front_tool_result"
			)
	return result


func _set_busy(value: bool) -> void:
	_busy = value
	_apply_btn.disabled = value
	_reject_btn.disabled = value


func _configure_session_allow() -> void:
	var can_session_allow := true
	for call in _calls:
		if call is Dictionary:
			var name := str(call.get("name", ""))
			var render_kind := str(call.get("render_kind", ""))
			if HIGH_RISK_TOOLS.has(name) or (render_kind == "run" and name != "run_system_command" and name != "execute_gd_script"):
				can_session_allow = false
				break
	if can_session_allow:
		_always_allow.disabled = false
		_always_allow.tooltip_text = ""
	else:
		_always_allow.button_pressed = false
		_always_allow.disabled = true
		_always_allow.tooltip_text = "Execution tools must be confirmed every time."
