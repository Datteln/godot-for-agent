@tool
extends VBoxContainer

const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const ContextCollector = preload("res://addons/ai_agent/context/context_collector.gd")
const ToolExecutor = preload("res://addons/ai_agent/tools/tool_executor.gd")
const PreviewConfirmPanel = preload("res://addons/ai_agent/ui/preview_confirm_panel.gd")
const DoctorPanel = preload("res://addons/ai_agent/ui/doctor_panel.gd")
const ExtensionPanel = preload("res://addons/ai_agent/ui/extension_panel.gd")
const MemoryPanel = preload("res://addons/ai_agent/ui/memory_panel.gd")
const CommandPalette = preload("res://addons/ai_agent/ui/command_palette.gd")
const RecoveryPrompt = preload("res://addons/ai_agent/ui/recovery_prompt.gd")
const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")

enum AgentState { IDLE, WAITING_LLM, WAITING_CONFIRM, EXECUTING, COMPACTING }

var editor_interface: EditorInterface
var service: Node
var state_store: Node
var undo_manager: Node

var _http_client: Node
var _collector: Node
var _tool_executor: Node
var _preview_panel: Window
var _doctor_panel: Window
var _extension_panel: Window
var _memory_panel: Window
var _command_palette: Window
var _recovery_prompt: ConfirmationDialog

var _messages: RichTextLabel
var _input: LineEdit
var _send_btn: Button
var _status: Label
var _doctor_btn: Button
var _extensions_btn: Button
var _commands_btn: Button
var _memory_btn: Button
var _reset_btn: Button
var _effort_options: OptionButton
var _style_options: OptionButton

var _state := AgentState.IDLE
var _last_doctor_report: Dictionary = {}
var _pending_confirm_cb: Callable
var _pending_reject_cb: Callable


func _ready() -> void:
	_build_ui()
	_build_children()
	_connect_signals()
	_set_state(AgentState.IDLE)
	_http_client.fetch_recovery_pointer()
	_http_client.fetch_output_styles()


func _build_ui() -> void:
	size_flags_horizontal = Control.SIZE_EXPAND_FILL
	size_flags_vertical = Control.SIZE_EXPAND_FILL

	var toolbar := HBoxContainer.new()
	add_child(toolbar)

	_send_btn = Button.new()
	_send_btn.text = "Send"
	toolbar.add_child(_send_btn)

	_effort_options = OptionButton.new()
	for effort in ["quick", "standard", "deep", "verify", "advisor"]:
		_effort_options.add_item(effort)
	toolbar.add_child(_effort_options)
	_sync_effort_selection()

	_style_options = OptionButton.new()
	for style in ["default", "concise", "review"]:
		_style_options.add_item(style)
	toolbar.add_child(_style_options)

	_doctor_btn = Button.new()
	_doctor_btn.text = "Doctor"
	toolbar.add_child(_doctor_btn)

	_extensions_btn = Button.new()
	_extensions_btn.text = "Extensions"
	toolbar.add_child(_extensions_btn)

	_commands_btn = Button.new()
	_commands_btn.text = "Commands"
	toolbar.add_child(_commands_btn)

	_memory_btn = Button.new()
	_memory_btn.text = "Memory"
	toolbar.add_child(_memory_btn)

	_reset_btn = Button.new()
	_reset_btn.text = "Reset"
	toolbar.add_child(_reset_btn)

	_messages = RichTextLabel.new()
	_messages.bbcode_enabled = true
	_messages.scroll_following = true
	_messages.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(_messages)

	var bottom := HBoxContainer.new()
	add_child(bottom)
	_input = LineEdit.new()
	_input.placeholder_text = "Ask the AI agent..."
	_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bottom.add_child(_input)

	_status = Label.new()
	_status.text = "Idle"
	add_child(_status)


func _build_children() -> void:
	_http_client = AgentHttpClient.new()
	_http_client.editor_interface = editor_interface
	_http_client.service = service
	add_child(_http_client)

	_collector = ContextCollector.new()
	_collector.editor_interface = editor_interface
	add_child(_collector)

	_tool_executor = ToolExecutor.new()
	_tool_executor.editor_interface = editor_interface
	_tool_executor.undo_manager = undo_manager
	add_child(_tool_executor)

	_preview_panel = PreviewConfirmPanel.new()
	_preview_panel.tool_executor = _tool_executor
	add_child(_preview_panel)

	_doctor_panel = DoctorPanel.new()
	add_child(_doctor_panel)

	_extension_panel = ExtensionPanel.new()
	add_child(_extension_panel)

	_memory_panel = MemoryPanel.new()
	add_child(_memory_panel)

	_command_palette = CommandPalette.new()
	add_child(_command_palette)

	_recovery_prompt = RecoveryPrompt.new()
	add_child(_recovery_prompt)


func _connect_signals() -> void:
	_send_btn.pressed.connect(_on_send)
	_input.text_submitted.connect(func(_text: String): _on_send())
	_effort_options.item_selected.connect(_on_effort_selected)
	_style_options.item_selected.connect(_on_style_selected)
	_doctor_btn.pressed.connect(func(): _http_client.fetch_doctor())
	_extensions_btn.pressed.connect(_on_extensions)
	_commands_btn.pressed.connect(func(): _command_palette.open())
	_memory_btn.pressed.connect(func(): _http_client.fetch_memory())
	_reset_btn.pressed.connect(_on_reset)
	_command_palette.run_requested.connect(func(name: String, args: Dictionary): _http_client.run_command(name, args))
	_memory_panel.save_requested.connect(func(text: String): _http_client.save_memory(text))
	_memory_panel.delete_requested.connect(func(id: String): _http_client.delete_memory(id))
	_memory_panel.clear_requested.connect(func(): _http_client.clear_memory())
	_http_client.response_received.connect(_on_response)
	_http_client.events_received.connect(_on_events)
	_http_client.error_occurred.connect(_on_error)
	_recovery_prompt.accepted_recovery.connect(_on_recovery_accepted)
	_recovery_prompt.rejected_recovery.connect(_on_recovery_rejected)
	if service != null:
		service.service_started.connect(_on_service_started)
		service.service_failed.connect(_on_service_failed)


func _on_send() -> void:
	var text := _input.text.strip_edges()
	if text == "" or _state != AgentState.IDLE:
		return
	_input.clear()
	_append_message("user", text)
	_set_state(AgentState.WAITING_LLM)
	if undo_manager != null:
		undo_manager.begin_batch("AI: " + text.left(40))
	_http_client.send_user_message(text, _collector.collect("any"))


func _on_response(response: Dictionary) -> void:
	if response.has("python_version"):
		_last_doctor_report = response
		_doctor_panel.show_report(response)
		if state_store != null:
			state_store.set_value("doctor_warnings", response.get("warnings", []))
		return

	if response.has("output_styles"):
		_update_output_styles(response.get("output_styles", []))
		return

	if response.has("items") and response.has("ok"):
		_memory_panel.show_memory_response(response)
		return

	if response.has("exists"):
		if response.get("exists", false):
			var pointer: Dictionary = response.get("pointer", {})
			if bool(ConfigMigrations.get_value(editor_interface, "ai_agent/show_recovery_prompt")):
				_recovery_prompt.show_pointer(pointer)
		return

	match str(response.get("type", "")):
		"tool_calls":
			_handle_tool_calls(response)
		"final":
			_handle_final(response)
		"error":
			_on_error(str(response.get("text", "Unknown error")))
		_:
			_append_message("system", JSON.stringify(response, "\t"))


func _handle_tool_calls(response: Dictionary) -> void:
	var calls: Array = response.get("calls", [])
	var silent: Array = []
	var confirm: Array = []
	for call in calls:
		if call is Dictionary and bool(call.get("needs_confirm", false)):
			confirm.append(call)
		else:
			silent.append(call)

	if state_store != null:
		state_store.set_value("current_turn_id", _http_client.current_turn_id)
		state_store.set_value("pending_calls", confirm)

	var results: Array = []
	for call in silent:
		if call is Dictionary:
			_set_state(AgentState.EXECUTING)
			results.append(await _tool_executor.execute(call))

	if not confirm.is_empty():
		_set_state(AgentState.WAITING_CONFIRM)
		_pending_confirm_cb = func(confirm_results: Array): _on_decision(results + confirm_results)
		_pending_reject_cb = func(reject_results: Array): _on_decision(results + reject_results)
		_preview_panel.confirmed.connect(_pending_confirm_cb, CONNECT_ONE_SHOT)
		_preview_panel.rejected.connect(_pending_reject_cb, CONNECT_ONE_SHOT)
		_preview_panel.show_calls(confirm)
	else:
		_set_state(AgentState.WAITING_LLM)
		_http_client.send_tool_results(results)


func _on_decision(results: Array) -> void:
	if _pending_confirm_cb.is_valid() and _preview_panel.confirmed.is_connected(_pending_confirm_cb):
		_preview_panel.confirmed.disconnect(_pending_confirm_cb)
	if _pending_reject_cb.is_valid() and _preview_panel.rejected.is_connected(_pending_reject_cb):
		_preview_panel.rejected.disconnect(_pending_reject_cb)
	if state_store != null:
		state_store.set_value("pending_calls", [])
	_set_state(AgentState.WAITING_LLM)
	_http_client.send_tool_results(results)


func _handle_final(response: Dictionary) -> void:
	_append_message("assistant", str(response.get("text", "")))
	if undo_manager != null:
		undo_manager.commit_batch()
	_set_state(AgentState.IDLE)
	_http_client.current_turn_id = ""
	if state_store != null:
		state_store.set_value("current_turn_id", "")
		state_store.set_value("pending_calls", [])


func _on_error(message: String) -> void:
	_append_message("error", message)
	if undo_manager != null:
		undo_manager.abort_batch()
	if state_store != null:
		state_store.set_value("pending_calls", [])
	_set_state(AgentState.IDLE)


func _on_reset() -> void:
	if _state == AgentState.WAITING_CONFIRM:
		if _pending_confirm_cb.is_valid() and _preview_panel.confirmed.is_connected(_pending_confirm_cb):
			_preview_panel.confirmed.disconnect(_pending_confirm_cb)
		if _pending_reject_cb.is_valid() and _preview_panel.rejected.is_connected(_pending_reject_cb):
			_preview_panel.rejected.disconnect(_pending_reject_cb)
		if _preview_panel.visible:
			_preview_panel.hide()
	if undo_manager != null:
		undo_manager.abort_batch()
	_messages.clear()
	_http_client.reset_session()
	if state_store != null:
		state_store.reset()
	_set_state(AgentState.IDLE)


func _on_recovery_accepted(pointer: Dictionary) -> void:
	_http_client.resume_from_pointer(pointer)
	if state_store != null:
		state_store.merge({
			"session_id": str(pointer.get("session_id", "default")),
			"recovery_pointer": pointer,
			"last_event_seq": int(pointer.get("last_event_seq", 0)),
			"current_turn_id": _http_client.current_turn_id
		})
	if _http_client.current_turn_id != "":
		_append_message("system", "Recovered session %s with a pending turn (%s). Send a message to continue, or press Reset to discard it." % [str(pointer.get("session_id", "")), _http_client.current_turn_id])
	else:
		_append_message("system", "Recovered session %s event history." % str(pointer.get("session_id", "")))
	_http_client.poll_events()


func _on_recovery_rejected() -> void:
	_http_client.reset_session()
	if state_store != null:
		state_store.set_value("recovery_pointer", null)
	_append_message("system", "Recovery dismissed; session was reset.")


func _on_service_started(base_url: String) -> void:
	if service != null and not service.is_running():
		_append_message("system", "AI service was not auto-started. Connect to %s with token: %s" % [base_url, str(service.token)])


func _on_service_failed(message: String) -> void:
	_append_message("error", "Service failed to start: " + message)
	if service != null and str(service.token) != "":
		_append_message("system", "Start the service manually. Base URL: %s  Token: %s" % [str(service.base_url), str(service.token)])


func _on_events(events: Array) -> void:
	for event in events:
		if not (event is Dictionary):
			continue
		if state_store != null:
			state_store.add_event(event)
		var event_type := str(event.get("type", ""))
		var previous_state := _state
		var is_compacting := event_type == "compact_boundary" and previous_state != AgentState.IDLE
		if is_compacting:
			_set_state(AgentState.COMPACTING)
		_append_message("system", _describe_event(event))
		if is_compacting:
			_set_state(previous_state)


func _describe_event(event: Dictionary) -> String:
	var payload: Dictionary = event.get("payload", {})
	match str(event.get("type", "")):
		"tool_results_received":
			return "Tool results received (%s)." % str(payload.get("count", 0))
		"user_submitted":
			var with_context := bool(payload.get("has_context", false))
			return "Message submitted%s." % (" with project context" if with_context else "")
		"tool_calls":
			return "Model requested %s tool call(s) (turn %s)." % [str(payload.get("count", 0)), str(payload.get("turn_id", ""))]
		"final":
			return "Final response received (%s chars)." % str(payload.get("text_length", 0))
		"error":
			return "Error: %s" % str(payload.get("text", ""))
		"reset":
			return "Session was reset."
		"config_changed":
			var parts: Array = []
			if payload.has("effort"):
				parts.append("effort=%s" % str(payload.get("effort")))
			if payload.has("output_style"):
				parts.append("output_style=%s" % str(payload.get("output_style")))
			return "Configuration changed (%s)." % ", ".join(parts)
		"compact_boundary":
			return "Compacted conversation history: %s frame(s), %s message(s) removed, %s recent kept, pending preserved: %s." % [
				str(payload.get("compacted_frames", 0)),
				str(payload.get("removed_messages", 0)),
				str(payload.get("keep_recent", 0)),
				str(payload.get("pending_preserved", false))
			]
		_:
			return "Event: %s %s" % [str(event.get("type", "unknown")), JSON.stringify(payload)]


func _on_extensions() -> void:
	if _last_doctor_report.is_empty():
		_http_client.fetch_doctor()
	else:
		_extension_panel.show_from_doctor(_last_doctor_report)


func _on_effort_selected(index: int) -> void:
	var effort := _effort_options.get_item_text(index)
	ConfigMigrations.set_value(editor_interface, "ai_agent/effort", effort)
	_http_client.run_command("set_effort", {"effort": effort})
	if state_store != null:
		state_store.set_value("effort", effort)


func _on_style_selected(index: int) -> void:
	var style := _style_options.get_item_text(index)
	ConfigMigrations.set_value(editor_interface, "ai_agent/output_style", style)
	_http_client.run_command("set_output_style", {"output_style": style})
	if state_store != null:
		state_store.set_value("output_style", style)


func _sync_effort_selection() -> void:
	if editor_interface == null:
		return
	var current := str(ConfigMigrations.get_value(editor_interface, "ai_agent/effort"))
	for index in range(_effort_options.get_item_count()):
		if _effort_options.get_item_text(index) == current:
			_effort_options.select(index)
			return


func _update_output_styles(styles: Array) -> void:
	var current := str(ConfigMigrations.get_value(editor_interface, "ai_agent/output_style"))
	_style_options.clear()
	var selected := 0
	for style in styles:
		if style is Dictionary and bool(style.get("enabled", true)):
			var name := str(style.get("name", "default"))
			_style_options.add_item(name)
			if name == current:
				selected = _style_options.get_item_count() - 1
	if _style_options.get_item_count() == 0:
		_style_options.add_item("default")
	_style_options.select(selected)


func _set_state(value: int) -> void:
	_state = value
	_send_btn.disabled = value != AgentState.IDLE
	match value:
		AgentState.IDLE:
			_status.text = "Idle"
		AgentState.WAITING_LLM:
			_status.text = "Waiting for model"
		AgentState.WAITING_CONFIRM:
			_status.text = "Waiting for confirmation"
		AgentState.EXECUTING:
			_status.text = "Executing tools"
		AgentState.COMPACTING:
			_status.text = "Compacting conversation history"
	if state_store != null:
		state_store.set_value("state", _status.text)


func _append_message(role: String, text: String) -> void:
	var color := {
		"user": "#9bdcff",
		"assistant": "#b8f7c6",
		"system": "#dddddd",
		"error": "#ff9b9b"
	}.get(role, "#ffffff")
	_messages.append_text("[color=%s][b]%s[/b][/color]\n%s\n\n" % [color, role, text])
