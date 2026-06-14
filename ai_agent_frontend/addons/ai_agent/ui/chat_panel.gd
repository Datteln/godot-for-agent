@tool
extends VBoxContainer

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const ContextCollector = preload("res://addons/ai_agent/context/context_collector.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")
const RecoveryPrompt = preload("res://addons/ai_agent/ui/recovery_prompt.gd")
const ToolExecutor = preload("res://addons/ai_agent/tools/tool_executor.gd")
const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")

enum AgentState { IDLE, WAITING_LLM, WAITING_CONFIRM, EXECUTING, COMPACTING }

const PENDING_TOOL_RESULTS_ERROR := "当前会话仍有待回传的工具结果，不能开始新的用户消息"
const HIGH_RISK_TOOLS := ["run_tests", "run_headless_self_test", "set_project_setting", "batch_rename"]

const UI_TEXT := {
	"zh": {
		"send": "发送",
		"stop": "停止",
		"new_session": "新会话",
		"doctor": "诊断",
		"extensions": "扩展",
		"commands": "命令",
		"memory": "记忆",
		"reset": "重置",
		"input_placeholder": "向 AI Agent 提问...",
		"idle": "空闲",
		"waiting_model": "等待模型响应",
		"waiting_confirm": "等待工具确认",
		"executing": "正在执行工具",
		"compacting": "正在压缩会话历史",
		"tool_calls": "工具调用",
		"tool_auto": "自动执行",
		"tool_confirm": "需要确认",
		"tool_from": "来自",
		"tool_result": "工具结果",
		"tool_ok": "成功",
		"tool_error": "失败",
		"confirm_title": "需要确认的工具调用",
		"apply": "应用",
		"reject": "拒绝",
		"always_allow": "本会话内自动允许相似低风险更改",
		"high_risk_hint": "执行类或高风险工具需要每次手动确认。",
		"interrupted": "已停止当前请求。本地等待队列已清空；如果后端已经开始执行，它可能会在后台完成当前步骤。",
		"new_session_started": "已创建新会话：%s",
		"pending_notice": "上一次工具结果还没有回传。你可以丢弃待回传结果继续当前会话，或重置整个会话。",
		"discard_pending": "丢弃待回传结果",
		"recovered_pending": "已恢复会话 %s，存在待处理回合 %s。继续发送消息前请先处理或重置。",
		"recovered_history": "已恢复会话 %s 的事件历史。",
		"recovery_dismissed": "已忽略恢复信息，会话已重置。",
		"service_manual": "AI 服务未自动启动。请连接 %s，令牌：%s",
		"service_failed": "服务启动失败：%s",
		"service_manual_full": "请手动启动服务。Base URL：%s  Token：%s",
		"event_tool_results": "已收到工具结果（%s）。",
		"event_user": "消息已提交%s。",
		"event_with_context": "，包含项目上下文",
		"event_tool_calls": "模型请求 %s 个工具调用（回合 %s）。",
		"event_final": "已收到最终回复（%s 字符）。",
		"event_error": "错误：%s",
		"event_reset": "会话已重置。",
		"event_config": "配置已更新（%s）。",
		"event_compact": "已压缩会话历史：%s 个 frame，移除 %s 条消息，保留最近 %s 条，保留待处理：%s。",
		"event_unknown": "事件：%s %s",
		"history_restored": "已恢复上次会话记录：%s 条。"
	},
	"en": {
		"send": "Send",
		"stop": "Stop",
		"new_session": "New Chat",
		"doctor": "Doctor",
		"extensions": "Extensions",
		"commands": "Commands",
		"memory": "Memory",
		"reset": "Reset",
		"input_placeholder": "Ask the AI agent...",
		"idle": "Idle",
		"waiting_model": "Waiting for model",
		"waiting_confirm": "Waiting for confirmation",
		"executing": "Executing tools",
		"compacting": "Compacting conversation history",
		"tool_calls": "Tool calls",
		"tool_auto": "auto",
		"tool_confirm": "needs confirmation",
		"tool_from": "from",
		"tool_result": "Tool result",
		"tool_ok": "ok",
		"tool_error": "error",
		"confirm_title": "Confirm tool calls",
		"apply": "Apply",
		"reject": "Reject",
		"always_allow": "Always allow similar low-risk changes in this session",
		"high_risk_hint": "Execution or high-risk tools must be confirmed every time.",
		"interrupted": "Current request stopped. The local queue was cleared; if the backend already started work, it may finish that step in the background.",
		"new_session_started": "New session created: %s",
		"pending_notice": "The previous tool results have not been submitted yet. Discard them to continue this session, or reset the whole session.",
		"discard_pending": "Discard pending results",
		"recovered_pending": "Recovered session %s with a pending turn %s. Resolve it or reset before sending another message.",
		"recovered_history": "Recovered session %s event history.",
		"recovery_dismissed": "Recovery dismissed; session was reset.",
		"service_manual": "AI service was not auto-started. Connect to %s with token: %s",
		"service_failed": "Service failed to start: %s",
		"service_manual_full": "Start the service manually. Base URL: %s  Token: %s",
		"event_tool_results": "Tool results received (%s).",
		"event_user": "Message submitted%s.",
		"event_with_context": " with project context",
		"event_tool_calls": "Model requested %s tool call(s) (turn %s).",
		"event_final": "Final response received (%s chars).",
		"event_error": "Error: %s",
		"event_reset": "Session was reset.",
		"event_config": "Configuration changed (%s).",
		"event_compact": "Compacted conversation history: %s frame(s), %s message(s) removed, %s recent kept, pending preserved: %s.",
		"event_unknown": "Event: %s %s",
		"history_restored": "Restored previous session history: %s item(s)."
	}
}

var editor_interface: EditorInterface
var service: Node
var state_store: Node
var undo_manager: Node

var _http_client: Node
var _collector: Node
var _tool_executor: Node
var _recovery_prompt: ConfirmationDialog

var _scroll: ScrollContainer
var _message_list: VBoxContainer
var _input: LineEdit
var _send_btn: Button
var _stop_btn: Button
var _new_session_btn: Button
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
var _pending_calls: Array = []
var _pending_silent_results: Array = []
var _inline_checkboxes: Array[CheckBox] = []
var _inline_always_allow: CheckBox
var _inline_apply_btn: Button
var _inline_reject_btn: Button
var _inline_confirm_box: Control
var _inline_busy := false


func _ready() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Initializing chat panel.")
	_build_ui()
	_build_children()
	_connect_signals()
	_set_state(AgentState.IDLE)
	_fetch_initial_service_data()


func _build_ui() -> void:
	size_flags_horizontal = Control.SIZE_EXPAND_FILL
	size_flags_vertical = Control.SIZE_EXPAND_FILL

	var toolbar := HBoxContainer.new()
	toolbar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(toolbar)

	_send_btn = Button.new()
	_send_btn.text = _ui("send")
	toolbar.add_child(_send_btn)

	_stop_btn = Button.new()
	_stop_btn.text = _ui("stop")
	_stop_btn.disabled = true
	toolbar.add_child(_stop_btn)

	_new_session_btn = Button.new()
	_new_session_btn.text = _ui("new_session")
	toolbar.add_child(_new_session_btn)

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
	_doctor_btn.text = _ui("doctor")
	toolbar.add_child(_doctor_btn)

	_extensions_btn = Button.new()
	_extensions_btn.text = _ui("extensions")
	toolbar.add_child(_extensions_btn)

	_commands_btn = Button.new()
	_commands_btn.text = _ui("commands")
	toolbar.add_child(_commands_btn)

	_memory_btn = Button.new()
	_memory_btn.text = _ui("memory")
	toolbar.add_child(_memory_btn)

	_reset_btn = Button.new()
	_reset_btn.text = _ui("reset")
	toolbar.add_child(_reset_btn)

	_scroll = ScrollContainer.new()
	_scroll.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(_scroll)

	_message_list = VBoxContainer.new()
	_message_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_message_list.add_theme_constant_override("separation", 10)
	_scroll.add_child(_message_list)

	var bottom := HBoxContainer.new()
	bottom.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(bottom)

	_input = LineEdit.new()
	_input.placeholder_text = _ui("input_placeholder")
	_input.context_menu_enabled = true
	_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bottom.add_child(_input)

	_status = Label.new()
	_status.text = _ui("idle")
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

	_recovery_prompt = RecoveryPrompt.new()
	add_child(_recovery_prompt)


func _connect_signals() -> void:
	_send_btn.pressed.connect(_on_send)
	_stop_btn.pressed.connect(_on_interrupt)
	_new_session_btn.pressed.connect(_on_new_session)
	_input.text_submitted.connect(func(_text: String): _on_send())
	_effort_options.item_selected.connect(_on_effort_selected)
	_style_options.item_selected.connect(_on_style_selected)
	_doctor_btn.pressed.connect(func(): _http_client.fetch_doctor())
	_extensions_btn.pressed.connect(_on_extensions)
	_commands_btn.pressed.connect(func(): _http_client.fetch_commands())
	_memory_btn.pressed.connect(func(): _http_client.fetch_memory())
	_reset_btn.pressed.connect(_on_reset)
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
		FrontendLogger.debug(editor_interface, "ChatPanel", "Ignored send request.", {
			"empty": text == "",
			"state": _status.text
		})
		return
	FrontendLogger.info(editor_interface, "ChatPanel", "Sending user message.", {"chars": text.length()})
	_input.clear()
	_append_message("user", text)
	_set_state(AgentState.WAITING_LLM)
	if undo_manager != null:
		undo_manager.begin_batch("AI: " + text.left(40))
	_http_client.send_user_message(text, _collector.collect("any"))


func _on_response(response: Dictionary) -> void:
	FrontendLogger.debug(editor_interface, "ChatPanel", "Handling response.", {
		"type": str(response.get("type", "data")),
		"keys": response.keys()
	})
	if response.has("python_version"):
		_last_doctor_report = response
		_append_message("system", "Doctor\n\n```json\n%s\n```" % JSON.stringify(response, "\t"))
		if state_store != null:
			state_store.set_value("doctor_warnings", response.get("warnings", []))
		return

	if response.has("output_styles"):
		_update_output_styles(response.get("output_styles", []))
		return

	if response.has("session_id") and response.has("pending_turn_id") and response.has("items"):
		_handle_session_history(response)
		return

	if response.has("items") and response.has("ok"):
		_append_message("system", "Memory\n\n```json\n%s\n```" % JSON.stringify(response, "\t"))
		return

	if response.has("type") and response.get("type") == "data":
		_append_message("system", "Data\n\n```json\n%s\n```" % JSON.stringify(response.get("value", null), "\t"))
		return

	if response.has("ok") and response.has("text"):
		_append_message("system", str(response.get("text", "")))
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
	FrontendLogger.info(editor_interface, "ChatPanel", "Handling tool calls.", {"count": calls.size()})

	if _state == AgentState.WAITING_CONFIRM:
		FrontendLogger.warn(editor_interface, "ChatPanel", "Ignoring tool_calls while a previous batch is still pending confirmation.", {"count": calls.size()})
		return

	_append_message("system", _format_tool_calls(calls))
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
			var result: Dictionary = await _tool_executor.execute(call)
			results.append(result)
			_append_tool_result(call, result)

	if not confirm.is_empty():
		FrontendLogger.info(editor_interface, "ChatPanel", "Waiting for inline tool confirmation.", {"count": confirm.size()})
		_pending_calls = confirm.duplicate(true)
		_pending_silent_results = results.duplicate(true)
		_show_inline_confirmation(_pending_calls)
		_set_state(AgentState.WAITING_CONFIRM)
	else:
		_set_state(AgentState.WAITING_LLM)
		_http_client.send_tool_results(results)


func _on_decision(results: Array) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Submitting tool decision.", {"result_count": results.size()})
	if _state != AgentState.WAITING_CONFIRM and _state != AgentState.EXECUTING:
		FrontendLogger.warn(editor_interface, "ChatPanel", "Ignoring duplicate tool decision.", {"result_count": results.size()})
		return
	_clear_inline_confirmation()
	if state_store != null:
		state_store.set_value("pending_calls", [])
	_set_state(AgentState.WAITING_LLM)
	_http_client.send_tool_results(results)


func _handle_final(response: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Received final response.", {
		"chars": str(response.get("text", "")).length()
	})
	_append_message("assistant", str(response.get("text", "")))
	if undo_manager != null:
		undo_manager.commit_batch()
	_set_state(AgentState.IDLE)
	_http_client.current_turn_id = ""
	if state_store != null:
		state_store.set_value("current_turn_id", "")
		state_store.set_value("pending_calls", [])


func _handle_session_history(response: Dictionary) -> void:
	var items: Array = response.get("items", [])
	var session_id := str(response.get("session_id", ""))
	FrontendLogger.info(editor_interface, "ChatPanel", "Restoring session history.", {
		"session_id": session_id,
		"count": items.size()
	})
	_clear_messages()
	if state_store != null:
		state_store.set_value("session_id", session_id)
	var pending_turn_id = response.get("pending_turn_id")
	if pending_turn_id != null:
		_http_client.current_turn_id = str(pending_turn_id)
		if state_store != null:
			state_store.set_value("current_turn_id", _http_client.current_turn_id)
	for item in items:
		if not (item is Dictionary):
			continue
		var role := str(item.get("role", "system"))
		var text := str(item.get("text", ""))
		if text.strip_edges() == "":
			continue
		_append_message(role, text)
	if pending_turn_id != null:
		_append_message("system", _ui("recovered_pending") % [session_id, str(pending_turn_id)])
	if not items.is_empty():
		_append_message("system", _ui("history_restored") % str(items.size()))


func _on_error(message: String) -> void:
	FrontendLogger.error(editor_interface, "ChatPanel", "Agent error.", {"message": message})
	_append_message("error", message)
	if undo_manager != null:
		undo_manager.abort_batch()
	if state_store != null:
		state_store.set_value("pending_calls", [])
	_set_state(AgentState.IDLE)
	if message == PENDING_TOOL_RESULTS_ERROR or message.contains("工具结果") or message.contains("tool result"):
		_show_pending_results_notice()


func _show_pending_results_notice() -> void:
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var panel := _make_panel("#262626", "#4a4a4a")
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(panel)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)

	var label := _make_rich_text(_ui("pending_notice"), "#dddddd")
	body.add_child(label)

	var actions := HBoxContainer.new()
	body.add_child(actions)

	var discard_btn := Button.new()
	discard_btn.text = _ui("discard_pending")
	discard_btn.pressed.connect(_discard_pending_results)
	actions.add_child(discard_btn)

	var reset_btn := Button.new()
	reset_btn.text = _ui("reset")
	reset_btn.pressed.connect(_on_reset)
	actions.add_child(reset_btn)

	_message_list.add_child(row)
	_scroll_to_bottom()


func _discard_pending_results() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Discarding pending tool results.")
	_http_client.discard_pending()
	_append_message("system", _ui("discard_pending"))


func _on_reset() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Reset requested.", {"state": _status.text})
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	_clear_messages()
	_http_client.reset_session()
	if state_store != null:
		state_store.reset()
	_set_state(AgentState.IDLE)


func _on_interrupt() -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Interrupt requested.", {"state": _status.text})
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _http_client != null:
		_http_client.interrupt_current()
	if state_store != null:
		state_store.set_value("pending_calls", [])
		state_store.set_value("current_turn_id", "")
	_set_state(AgentState.IDLE)
	_append_message("system", _ui("interrupted"))


func _on_new_session() -> void:
	var session_id := "session_%d" % int(Time.get_unix_time_from_system())
	FrontendLogger.info(editor_interface, "ChatPanel", "New session requested.", {"session_id": session_id})
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", session_id)
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _http_client != null:
		_http_client.interrupt_current()
		_http_client.reset_session()
	if state_store != null:
		state_store.reset()
		state_store.set_value("session_id", session_id)
	_clear_messages()
	_set_state(AgentState.IDLE)
	_append_message("system", _ui("new_session_started") % session_id)


func _on_recovery_accepted(pointer: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Recovery accepted.", {
		"session_id": str(pointer.get("session_id", "")),
		"pending_turn_id": str(pointer.get("pending_turn_id", ""))
	})
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", str(pointer.get("session_id", "default")))
	_http_client.resume_from_pointer(pointer)
	_http_client.fetch_session_history()
	if state_store != null:
		state_store.merge({
			"session_id": str(pointer.get("session_id", "default")),
			"recovery_pointer": pointer,
			"last_event_seq": int(pointer.get("last_event_seq", 0)),
			"current_turn_id": _http_client.current_turn_id
		})
	_http_client.poll_events()


func _on_recovery_rejected() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Recovery rejected.")
	_http_client.reset_session()
	if state_store != null:
		state_store.set_value("recovery_pointer", null)
	_append_message("system", _ui("recovery_dismissed"))


func _on_service_started(base_url: String) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Service started signal received.", {"base_url": base_url})
	_fetch_initial_service_data()
	if service != null and not service.is_running():
		_append_message("system", _ui("service_manual") % [base_url, str(service.token)])


func _on_service_failed(message: String) -> void:
	FrontendLogger.error(editor_interface, "ChatPanel", "Service failed signal received.", {"message": message})
	_append_message("error", _ui("service_failed") % message)
	if service != null and str(service.token) != "":
		_append_message("system", _ui("service_manual_full") % [str(service.base_url), str(service.token)])


func _fetch_initial_service_data() -> void:
	if _http_client == null:
		return
	var root := ""
	if service != null:
		root = str(service.base_url)
	if root.strip_edges().is_empty():
		if bool(ConfigMigrations.get_value(editor_interface, "ai_agent/auto_start_service")):
			return
		root = str(ConfigMigrations.get_value(editor_interface, "ai_agent/service_url"))
	if root.strip_edges().is_empty():
		return
	FrontendLogger.debug(editor_interface, "ChatPanel", "Fetching initial service data.", {"base_url": root})
	_http_client.fetch_session_history()
	_http_client.fetch_recovery_pointer()
	_http_client.fetch_output_styles()


func _on_events(events: Array) -> void:
	FrontendLogger.debug(editor_interface, "ChatPanel", "Handling events.", {"count": events.size()})
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
			return _ui("event_tool_results") % str(payload.get("count", 0))
		"user_submitted":
			var with_context := bool(payload.get("has_context", false))
			return _ui("event_user") % (_ui("event_with_context") if with_context else "")
		"tool_calls":
			return _ui("event_tool_calls") % [str(payload.get("count", 0)), str(payload.get("turn_id", ""))]
		"final":
			return _ui("event_final") % str(payload.get("text_length", 0))
		"error":
			return _ui("event_error") % str(payload.get("text", ""))
		"reset":
			return _ui("event_reset")
		"config_changed":
			var parts: Array = []
			if payload.has("effort"):
				parts.append("effort=%s" % str(payload.get("effort")))
			if payload.has("output_style"):
				parts.append("output_style=%s" % str(payload.get("output_style")))
			return _ui("event_config") % ", ".join(parts)
		"compact_boundary":
			return _ui("event_compact") % [
				str(payload.get("compacted_frames", 0)),
				str(payload.get("removed_messages", 0)),
				str(payload.get("keep_recent", 0)),
				str(payload.get("pending_preserved", false))
			]
		_:
			return _ui("event_unknown") % [str(event.get("type", "unknown")), JSON.stringify(payload)]


func _on_extensions() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Extensions requested.")
	if _last_doctor_report.is_empty():
		_http_client.fetch_doctor()
	else:
		var payload := {
			"skills": _last_doctor_report.get("skills", []),
			"warnings": _last_doctor_report.get("warnings", [])
		}
		_append_message("system", "Extensions\n\n```json\n%s\n```" % JSON.stringify(payload, "\t"))


func _on_effort_selected(index: int) -> void:
	var effort := _effort_options.get_item_text(index)
	FrontendLogger.info(editor_interface, "ChatPanel", "Effort selected.", {"effort": effort})
	ConfigMigrations.set_value(editor_interface, "ai_agent/effort", effort)
	_http_client.run_command("set_effort", {"effort": effort})
	if state_store != null:
		state_store.set_value("effort", effort)


func _on_style_selected(index: int) -> void:
	var style := _style_options.get_item_text(index)
	FrontendLogger.info(editor_interface, "ChatPanel", "Output style selected.", {"output_style": style})
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


func _show_inline_confirmation(calls: Array) -> void:
	_clear_inline_confirmation()
	_inline_checkboxes.clear()

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var panel := _make_panel("#242424", "#5c5c5c")
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(panel)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 8)
	panel.add_child(body)

	var title := Label.new()
	title.text = _ui("confirm_title")
	body.add_child(title)

	for call in calls:
		if not (call is Dictionary):
			continue
		var item := HBoxContainer.new()
		item.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		var checkbox := CheckBox.new()
		checkbox.text = _ui("apply")
		checkbox.button_pressed = true
		_inline_checkboxes.append(checkbox)
		item.add_child(checkbox)
		var preview := ToolPreviewRenderer.render_call(call)
		preview.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		item.add_child(preview)
		body.add_child(item)
		body.add_child(HSeparator.new())

	_inline_always_allow = CheckBox.new()
	_inline_always_allow.text = _ui("always_allow")
	body.add_child(_inline_always_allow)
	_configure_session_allow()

	var actions := HBoxContainer.new()
	body.add_child(actions)

	_inline_apply_btn = Button.new()
	_inline_apply_btn.text = _ui("apply")
	_inline_apply_btn.pressed.connect(_on_inline_apply)
	actions.add_child(_inline_apply_btn)

	_inline_reject_btn = Button.new()
	_inline_reject_btn.text = _ui("reject")
	_inline_reject_btn.pressed.connect(_on_inline_reject)
	actions.add_child(_inline_reject_btn)

	_inline_confirm_box = row
	_message_list.add_child(row)
	_scroll_to_bottom()


func _on_inline_apply() -> void:
	if _inline_busy:
		return
	_set_inline_busy(true)
	var results := _pending_silent_results.duplicate(true)
	for index in range(_pending_calls.size()):
		var call = _pending_calls[index]
		if not (call is Dictionary):
			continue
		var should_apply := index < _inline_checkboxes.size() and _inline_checkboxes[index].button_pressed
		if should_apply:
			_set_state(AgentState.EXECUTING)
			var result: Dictionary = await _tool_executor.execute(call)
			result["grant_session_allow"] = _inline_always_allow != null and _inline_always_allow.button_pressed
			results.append(result)
			_append_tool_result(call, result)
		else:
			results.append(AgentDTO.rejected_result(call))
	_set_inline_busy(false)
	_on_decision(results)


func _on_inline_reject() -> void:
	if _inline_busy:
		return
	var results := _pending_silent_results.duplicate(true)
	for call in _pending_calls:
		if call is Dictionary:
			results.append(AgentDTO.rejected_result(call))
	_append_message("system", _ui("reject"))
	_on_decision(results)


func _clear_inline_confirmation() -> void:
	if _inline_confirm_box != null and is_instance_valid(_inline_confirm_box):
		_inline_confirm_box.queue_free()
	_inline_confirm_box = null
	_inline_checkboxes.clear()
	_pending_calls.clear()
	_pending_silent_results.clear()
	_inline_busy = false


func _set_inline_busy(value: bool) -> void:
	_inline_busy = value
	if _inline_apply_btn != null:
		_inline_apply_btn.disabled = value
	if _inline_reject_btn != null:
		_inline_reject_btn.disabled = value


func _configure_session_allow() -> void:
	if _inline_always_allow == null:
		return
	var can_session_allow := true
	for call in _pending_calls:
		if call is Dictionary:
			var name := str(call.get("name", ""))
			var render_kind := str(call.get("render_kind", ""))
			if HIGH_RISK_TOOLS.has(name) or render_kind == "run":
				can_session_allow = false
				break
	if can_session_allow:
		_inline_always_allow.disabled = false
		_inline_always_allow.tooltip_text = ""
	else:
		_inline_always_allow.button_pressed = false
		_inline_always_allow.disabled = true
		_inline_always_allow.tooltip_text = _ui("high_risk_hint")


func _set_state(value: int) -> void:
	var previous_state := _state
	_state = value
	_send_btn.disabled = value != AgentState.IDLE
	_stop_btn.disabled = value == AgentState.IDLE
	_new_session_btn.disabled = value == AgentState.EXECUTING
	match value:
		AgentState.IDLE:
			_status.text = _ui("idle")
		AgentState.WAITING_LLM:
			_status.text = _ui("waiting_model")
		AgentState.WAITING_CONFIRM:
			_status.text = _ui("waiting_confirm")
		AgentState.EXECUTING:
			_status.text = _ui("executing")
		AgentState.COMPACTING:
			_status.text = _ui("compacting")
	if state_store != null:
		state_store.set_value("state", _status.text)
	if previous_state != value:
		FrontendLogger.debug(editor_interface, "ChatPanel", "State changed.", {
			"from": previous_state,
			"to": value,
			"text": _status.text
		})


func _append_message(role: String, text: String) -> void:
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	if role == "user":
		var spacer := Control.new()
		spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		row.add_child(spacer)

	var panel := _make_message_panel(role)
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL if role != "user" else 0
	row.add_child(panel)

	if role != "user":
		var right_spacer := Control.new()
		right_spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		right_spacer.custom_minimum_size = Vector2(40, 0)
		row.add_child(right_spacer)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)

	var title := Label.new()
	title.text = role
	title.add_theme_color_override("font_color", _role_color(role))
	body.add_child(title)

	var rich := _make_rich_text(text, "#dddddd")
	body.add_child(rich)

	_message_list.add_child(row)
	_scroll_to_bottom()


func _append_tool_result(call: Dictionary, result: Dictionary) -> void:
	var name := str(call.get("name", "unknown"))
	var status := str(result.get("status", ""))
	var label := _ui("tool_ok") if status == "ok" or status == "success" else status
	if status == "error":
		label = _ui("tool_error")
	var body := "%s: `%s` - %s" % [_ui("tool_result"), name, label]
	if result.has("result"):
		body += "\n\n```json\n%s\n```" % JSON.stringify(result.get("result"), "\t")
	if result.has("error_code"):
		body += "\n\n`%s`" % str(result.get("error_code"))
	_append_message("system", body)


func _format_tool_calls(calls: Array) -> String:
	var lines := [_ui("tool_calls")]
	for call in calls:
		if not (call is Dictionary):
			continue
		var name := str(call.get("name", "unknown"))
		var agent := str(call.get("agent", ""))
		var needs_confirm := bool(call.get("needs_confirm", false))
		var suffix := _ui("tool_confirm") if needs_confirm else _ui("tool_auto")
		var from_text := " %s %s" % [_ui("tool_from"), agent] if agent != "" else ""
		lines.append("- `%s`%s - %s" % [name, from_text, suffix])
	return "\n".join(lines)


func _make_message_panel(role: String) -> PanelContainer:
	match role:
		"user":
			return _make_panel("#263744", "#45606f")
		"assistant":
			return _make_panel("#202020", "#3d3d3d")
		"error":
			return _make_panel("#3b2424", "#804444")
		_:
			return _make_panel("#252525", "#3f3f3f")


func _make_panel(bg_color: String, border_color: String) -> PanelContainer:
	var panel := PanelContainer.new()
	var style := StyleBoxFlat.new()
	style.bg_color = Color(bg_color)
	style.border_color = Color(border_color)
	style.set_border_width_all(1)
	style.set_corner_radius_all(6)
	style.set_content_margin(SIDE_LEFT, 10)
	style.set_content_margin(SIDE_RIGHT, 10)
	style.set_content_margin(SIDE_TOP, 8)
	style.set_content_margin(SIDE_BOTTOM, 8)
	panel.add_theme_stylebox_override("panel", style)
	return panel


func _make_rich_text(text: String, color: String) -> RichTextLabel:
	var rich := RichTextLabel.new()
	rich.bbcode_enabled = true
	rich.selection_enabled = true
	rich.context_menu_enabled = true
	rich.fit_content = true
	rich.scroll_active = false
	rich.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	rich.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	rich.add_theme_color_override("default_color", Color(color))
	rich.append_text(_markdown_to_bbcode(text))
	return rich


func _role_color(role: String) -> Color:
	match role:
		"user":
			return Color("#9bdcff")
		"assistant":
			return Color("#b8f7c6")
		"error":
			return Color("#ff9b9b")
		_:
			return Color("#dddddd")


func _clear_messages() -> void:
	for child in _message_list.get_children():
		child.queue_free()


func _scroll_to_bottom() -> void:
	call_deferred("_scroll_to_bottom_deferred")


func _scroll_to_bottom_deferred() -> void:
	if _scroll == null:
		return
	var bar := _scroll.get_v_scroll_bar()
	_scroll.scroll_vertical = int(bar.max_value)


func _ui(key: String) -> String:
	var lang := "zh"
	if editor_interface != null:
		lang = str(ConfigMigrations.get_value(editor_interface, "ai_agent/ui_language"))
	if not UI_TEXT.has(lang):
		lang = "zh"
	var table: Dictionary = UI_TEXT.get(lang, UI_TEXT["zh"])
	return str(table.get(key, key))


func _markdown_to_bbcode(text: String) -> String:
	var result: Array[String] = []
	var in_code := false
	for raw_line in text.split("\n"):
		var line := str(raw_line)
		if line.begins_with("```"):
			in_code = not in_code
			result.append("[code]" if in_code else "[/code]")
			continue
		if in_code:
			result.append(_escape_bbcode(line))
			continue
		result.append(_markdown_line_to_bbcode(line))
	return "\n".join(result)


func _markdown_line_to_bbcode(line: String) -> String:
	var escaped := _escape_bbcode(line)
	if line.begins_with("### "):
		return "[b]" + _escape_bbcode(line.substr(4)) + "[/b]"
	if line.begins_with("## "):
		return "[font_size=18][b]" + _escape_bbcode(line.substr(3)) + "[/b][/font_size]"
	if line.begins_with("# "):
		return "[font_size=20][b]" + _escape_bbcode(line.substr(2)) + "[/b][/font_size]"
	if line.begins_with("- "):
		escaped = "• " + _escape_bbcode(line.substr(2))
	escaped = _replace_inline_code(escaped)
	escaped = _replace_bold(escaped)
	return escaped


func _replace_inline_code(text: String) -> String:
	var parts := text.split("`")
	if parts.size() < 3:
		return text
	var result := ""
	for index in range(parts.size()):
		result += str(parts[index])
		if index < parts.size() - 1:
			result += "[code]" if index % 2 == 0 else "[/code]"
	return result


func _replace_bold(text: String) -> String:
	var parts := text.split("**")
	if parts.size() < 3:
		return text
	var result := ""
	for index in range(parts.size()):
		result += str(parts[index])
		if index < parts.size() - 1:
			result += "[b]" if index % 2 == 0 else "[/b]"
	return result


func _escape_bbcode(text: String) -> String:
	return text.replace("[", "[lb]").replace("]", "[rb]")
