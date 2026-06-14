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

# Markdown 渲染：代码块背景色与语法高亮配色（深色主题）。
const _CODE_BLOCK_BG := "#1e1e1e"
const _SYNTAX_COMMENT_COLOR := "#6a9955"
const _SYNTAX_STRING_COLOR := "#ce9178"
const _SYNTAX_NUMBER_COLOR := "#b5cea8"
const _SYNTAX_KEYWORD_COLOR := "#569cd6"

# 代码块语言标记别名归一化（如 ```gd``` -> gdscript）。
const _CODE_LANG_ALIASES := {
	"gd": "gdscript",
	"py": "python",
	"js": "javascript",
	"ts": "typescript",
	"cs": "csharp",
	"yml": "yaml",
	"sh": "bash",
	"shell": "bash",
}

# 各语言行注释前缀，用于语法高亮时识别注释（不含的语言不做注释着色）。
const _CODE_LINE_COMMENT := {
	"gdscript": "#", "python": "#", "bash": "#", "yaml": "#", "toml": "#", "ini": "#", "cfg": "#",
	"c": "//", "cpp": "//", "csharp": "//", "java": "//",
	"javascript": "//", "typescript": "//", "go": "//", "rust": "//",
}

# 各语言关键字高亮列表。
const _CODE_KEYWORDS := {
	"gdscript": ["func", "var", "const", "if", "elif", "else", "for", "while", "return", "class",
		"extends", "class_name", "enum", "match", "pass", "break", "continue", "signal", "static",
		"await", "preload", "load", "true", "false", "null", "self", "and", "or", "not", "in", "is",
		"as", "super", "tool"],
	"python": ["def", "class", "if", "elif", "else", "for", "while", "return", "import", "from",
		"as", "with", "try", "except", "finally", "raise", "pass", "break", "continue", "lambda",
		"yield", "async", "await", "global", "nonlocal", "not", "and", "or", "in", "is", "None",
		"True", "False", "self"],
	"json": ["true", "false", "null"],
}

# 目录树/连线图常用的 Unicode 制表符，出现在未加代码围栏的段落中时，
# 该段落会被当作等宽代码块整体渲染（否则连线在比例字体下无法对齐）。
const _TREE_LINE_CHARS := ["├", "└", "│", "─", "┌", "┐", "┘", "┬", "┴", "┤", "┼", "╭", "╮", "╯", "╰"]

# 工具名 -> Claude Code 风格的展示名（"⏺ ToolName(args)"）。未列出的工具直接
# 使用原始工具名。
const _TOOL_DISPLAY_NAMES := {
	"read_file": "Read",
	"read_script": "Read",
	"read_class_docs": "ClassInfo",
	"read_class_info": "ClassInfo",
	"get_class_info": "ClassInfo",
	"write_file": "Write",
	"propose_script_edit": "Edit",
	"apply_text_edit": "Edit",
	"propose_tests": "Write",
	"propose_content_file": "Write",
	"run_tests": "Bash",
	"run_headless_self_test": "Bash",
	"delegate": "Task",
	"delegate_many": "Task",
	"search_tools": "SearchTools",
}

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
		"confirm_title": "需要确认的工具调用",
		"apply": "应用",
		"reject": "拒绝",
		"always_allow": "本会话内自动允许相似低风险更改",
		"high_risk_hint": "执行类或高风险工具需要每次手动确认。",
		"interrupted": "已停止当前请求，晚到的响应和事件不会继续显示。",
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
		"history_restored": "已恢复上次会话记录：%s 条。",
		"event_agent_step": "思考中：`%s`（第 %s 轮，可用工具 %s 个）。",
		"event_agent_tools": "`%s` 请求工具：%s。",
		"event_delegate": "⏺ **Task**(%s)",
		"event_tool_start": "⏺ **%s**(%s)",
		"event_tool_done": "⎿ %s 完成",
		"event_tool_failed": "⎿ %s 出错",
		"thinking_show": "▸ 显示思考过程",
		"thinking_hide": "▾ 隐藏思考过程",
		"tool_read_lines": "读取了 %s 行",
		"tool_wrote_lines": "已写入 `%s`（%s 行）",
		"tool_run_result": "%s（exit=%s）",
		"tool_items_count": "返回 %s 条结果",
		"tool_done": "完成",
		"tool_done_path": "完成：`%s`",
		"tool_rejected": "已拒绝",
		"tool_error_detail": "出错：%s",
		"tool_unknown_error": "未知错误"
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
		"confirm_title": "Confirm tool calls",
		"apply": "Apply",
		"reject": "Reject",
		"always_allow": "Always allow similar low-risk changes in this session",
		"high_risk_hint": "Execution or high-risk tools must be confirmed every time.",
		"interrupted": "Current request stopped. Late responses and events will not be shown.",
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
		"history_restored": "Restored previous session history: %s item(s).",
		"event_agent_step": "Thinking: `%s` (loop %s, %s visible tool(s)).",
		"event_agent_tools": "`%s` requested tools: %s.",
		"event_delegate": "⏺ **Task**(%s)",
		"event_tool_start": "⏺ **%s**(%s)",
		"event_tool_done": "⎿ %s done",
		"event_tool_failed": "⎿ %s failed",
		"thinking_show": "▸ Show thinking",
		"thinking_hide": "▾ Hide thinking",
		"tool_read_lines": "Read %s lines",
		"tool_wrote_lines": "Wrote `%s` (%s lines)",
		"tool_run_result": "%s (exit=%s)",
		"tool_items_count": "Returned %s item(s)",
		"tool_done": "Done",
		"tool_done_path": "Done: `%s`",
		"tool_rejected": "Rejected",
		"tool_error_detail": "Error: %s",
		"tool_unknown_error": "Unknown error"
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
var _interrupted_locally := false

var _stream_key := ""
var _stream_row: Control
var _stream_reasoning_toggle: Button
var _stream_reasoning_rich: RichTextLabel
var _stream_reasoning_text := ""
var _stream_content_rich: RichTextLabel
var _stream_content_text := ""
var _rendered_assistant_keys := {}


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
	_interrupted_locally = false
	_finish_streaming()
	_input.clear()
	_append_message("user", text)
	_append_message("system", _ui("waiting_model"))
	_set_state(AgentState.WAITING_LLM)
	if undo_manager != null:
		undo_manager.begin_batch("AI: " + text.left(40))
	_http_client.send_user_message(text, _collector.collect("any"))


func _on_response(response: Dictionary) -> void:
	if _interrupted_locally and str(response.get("type", "")) in ["tool_calls", "final", "error"]:
		FrontendLogger.info(editor_interface, "ChatPanel", "Suppressed response after interrupt.", {
			"type": str(response.get("type", ""))
		})
		return
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

	# 本轮流式输出只是模型在调用工具前的中间想法，不是最终回复，丢弃其气泡避免与
	# 后续轮次的最终回复重复展示。
	_discard_stream_message()
	var silent: Array = []
	var confirm: Array = []
	for call in calls:
		if call is Dictionary and bool(call.get("needs_confirm", false)):
			confirm.append(call)
		else:
			silent.append(call)

	# 立即展示每个工具调用的 "⏺ ToolName(args)" 标题行，无论是否需要确认，
	# 让用户能实时看到模型调用了什么智能体/工具、读取/编辑了什么文件。
	for call in confirm:
		if call is Dictionary:
			_append_message("system", _format_tool_call_header(call))

	if state_store != null:
		state_store.set_value("current_turn_id", _http_client.current_turn_id)
		state_store.set_value("pending_calls", confirm)

	var results: Array = []
	for call in silent:
		if call is Dictionary:
			if _interrupted_locally:
				return
			_append_message("system", _format_tool_call_header(call))
			_set_state(AgentState.EXECUTING)
			var result: Dictionary = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
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
	if _interrupted_locally:
		FrontendLogger.info(editor_interface, "ChatPanel", "Suppressed tool decision after interrupt.")
		return
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
	var text := str(response.get("text", ""))
	var assistant_key := _message_fingerprint(text)
	if _rendered_assistant_keys.has(assistant_key):
		_discard_stream_message()
	elif _stream_content_rich != null and is_instance_valid(_stream_content_rich) and _stream_content_text.strip_edges() != "":
		_stream_content_rich.clear()
		_stream_content_rich.append_text(_markdown_to_bbcode(text))
		_rendered_assistant_keys[assistant_key] = true
		_scroll_to_bottom()
	else:
		_append_message("assistant", text)
	_finish_streaming()
	if undo_manager != null:
		undo_manager.commit_batch()
	_set_state(AgentState.IDLE)
	_http_client.current_turn_id = ""
	if state_store != null:
		state_store.set_value("current_turn_id", "")
		state_store.set_value("pending_calls", [])


func _handle_session_history(response: Dictionary) -> void:
	if _state != AgentState.IDLE:
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored session history while a turn is active.", {
			"state": _status.text
		})
		return
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
	_finish_streaming()
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
	_interrupted_locally = false
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
	_interrupted_locally = true
	_clear_inline_confirmation()
	_finish_streaming()
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
	_interrupted_locally = false
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
	if _interrupted_locally:
		FrontendLogger.debug(editor_interface, "ChatPanel", "Suppressed events after interrupt.", {"count": events.size()})
		return
	FrontendLogger.debug(editor_interface, "ChatPanel", "Handling events.", {"count": events.size()})
	for event in events:
		if not (event is Dictionary):
			continue
		if state_store != null:
			state_store.add_event(event)
		var event_type := str(event.get("type", ""))
		if event_type == "agent_reasoning_delta":
			_on_reasoning_delta(event)
			continue
		if event_type == "agent_text_delta":
			_on_text_delta(event)
			continue
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
		"agent_step":
			return _ui("event_agent_step") % [
				str(payload.get("agent", "")),
				str(payload.get("loop", "")),
				str(payload.get("visible_tools", 0))
			]
		"agent_tool_calls":
			return _ui("event_agent_tools") % [
				str(payload.get("agent", "")),
				", ".join(_string_array(payload.get("tools", [])))
			]
		"delegate_start":
			return _ui("event_delegate") % _format_delegate_args(payload)
		"server_tool_start":
			return _ui("event_tool_start") % [
				_tool_display_name(str(payload.get("tool", ""))),
				_format_event_args(payload)
			]
		"server_tool_result":
			var key := "event_tool_failed" if bool(payload.get("is_error", false)) else "event_tool_done"
			return _ui(key) % _tool_display_name(str(payload.get("tool", "")))
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


func _string_array(value: Variant) -> Array[String]:
	var result: Array[String] = []
	if value is Array:
		for item in value:
			result.append(str(item))
	return result


func _format_event_args(payload: Dictionary) -> String:
	var raw_args = payload.get("args", {})
	var args: Dictionary = raw_args if raw_args is Dictionary else {}
	var parts: Array[String] = []
	for key in ["path", "target_path", "file_path", "script_path", "resource_path", "scene_path", "command", "kind", "agent", "task", "query"]:
		if not args.has(key):
			continue
		var value := str(args.get(key, "")).strip_edges()
		if value.length() > 90:
			value = value.left(90) + "..."
		parts.append("%s=`%s`" % [key, value])
	return ", ".join(parts)


## `delegate`/`delegate_many` 事件的参数展示：优先复用 `_format_event_args`
## 提取到的 `agent`/`task`，`delegate_many` 的 `tasks` 数组不在该提取范围内，
## 此时回退为展示工具名本身。
func _format_delegate_args(payload: Dictionary) -> String:
	var args_text := _format_event_args(payload)
	if args_text != "":
		return args_text
	return str(payload.get("tool", "delegate"))


## 工具名 -> Claude Code 风格展示名（"⏺ ToolName(args)"）；未在
## `_TOOL_DISPLAY_NAMES` 中列出的工具直接显示原始工具名。
func _tool_display_name(name: String) -> String:
	return str(_TOOL_DISPLAY_NAMES.get(name, name))


## 截断过长文本并追加省略号，用于"⏺/⎿"行中的参数与结果摘要。
func _truncate_text(text: String, max_len: int) -> String:
	var stripped := text.strip_edges()
	if stripped.length() > max_len:
		return stripped.left(max_len) + "..."
	return stripped


## 统计文本行数（空字符串视为 0 行）。
func _count_lines(text: String) -> int:
	if text == "":
		return 0
	return text.split("\n").size()


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
			if _interrupted_locally:
				return
			_set_state(AgentState.EXECUTING)
			var result: Dictionary = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			result["grant_session_allow"] = _inline_always_allow != null and _inline_always_allow.button_pressed
			results.append(result)
			_append_tool_result(call, result)
		else:
			var rejected := AgentDTO.rejected_result(call)
			results.append(rejected)
			_append_tool_result(call, rejected)
	_set_inline_busy(false)
	_on_decision(results)


func _on_inline_reject() -> void:
	if _inline_busy:
		return
	var results := _pending_silent_results.duplicate(true)
	for call in _pending_calls:
		if call is Dictionary:
			var rejected := AgentDTO.rejected_result(call)
			results.append(rejected)
			_append_tool_result(call, rejected)
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


func _on_reasoning_delta(event: Dictionary) -> void:
	var payload: Dictionary = event.get("payload", {})
	var text := str(payload.get("text", ""))
	_ensure_stream_message("%s:%s" % [str(payload.get("frame_id", "")), str(payload.get("loop", ""))])
	_stream_reasoning_text = text
	if text.strip_edges() == "":
		return
	if _stream_reasoning_toggle != null and is_instance_valid(_stream_reasoning_toggle):
		_stream_reasoning_toggle.visible = true
	if _stream_reasoning_rich != null and is_instance_valid(_stream_reasoning_rich):
		_stream_reasoning_rich.clear()
		_stream_reasoning_rich.append_text(_markdown_to_bbcode(text))
	_scroll_to_bottom()


func _on_text_delta(event: Dictionary) -> void:
	var payload: Dictionary = event.get("payload", {})
	var text := str(payload.get("text", ""))
	_ensure_stream_message("%s:%s" % [str(payload.get("frame_id", "")), str(payload.get("loop", ""))])
	_stream_content_text = text
	if _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		_stream_content_rich.clear()
		_stream_content_rich.append_text(_markdown_to_bbcode(text))
	_scroll_to_bottom()


## 确保存在一个用于流式展示的消息气泡，包含可折叠的"思考过程"与正文区域。
## `key` 通常为 `frame_id:loop`；与当前流式消息的 key 不同时会丢弃上一条流式
## 消息气泡（例如委派子帧或"边想边调用工具"轮次的中间输出，不应作为独立
## 消息保留，否则会和后续轮次/父帧的最终回复重复展示）。
func _ensure_stream_message(key: String) -> void:
	if _stream_key == key and _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		return
	_discard_stream_message()
	_stream_key = key

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var panel := _make_message_panel("assistant")
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(panel)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)

	var toggle := Button.new()
	toggle.text = _ui("thinking_show")
	toggle.visible = false
	toggle.focus_mode = Control.FOCUS_NONE
	body.add_child(toggle)

	var reasoning_rich := _make_rich_text("", "#9a9a9a")
	reasoning_rich.visible = false
	body.add_child(reasoning_rich)

	toggle.pressed.connect(func():
		reasoning_rich.visible = not reasoning_rich.visible
		toggle.text = _ui("thinking_hide") if reasoning_rich.visible else _ui("thinking_show")
		_scroll_to_bottom()
	)

	var content_rich := _make_rich_text("", "#dddddd")
	body.add_child(content_rich)

	_message_list.add_child(row)
	_stream_row = row
	_stream_reasoning_toggle = toggle
	_stream_reasoning_rich = reasoning_rich
	_stream_content_rich = content_rich
	_scroll_to_bottom()


## 结束当前流式消息：仅清空引用，已渲染的气泡保留在历史记录中。
func _finish_streaming() -> void:
	_stream_key = ""
	_stream_row = null
	_stream_reasoning_toggle = null
	_stream_reasoning_rich = null
	_stream_reasoning_text = ""
	_stream_content_rich = null
	_stream_content_text = ""


## 丢弃当前流式消息气泡：用于本轮 LLM 输出仅是"边想边调用工具"的中间内容，
## 不是最终回复（避免和后续轮次的最终回复重复展示）。
func _discard_stream_message() -> void:
	if _stream_row != null and is_instance_valid(_stream_row):
		_stream_row.queue_free()
	_finish_streaming()


func _append_message(role: String, text: String, color: String = "#dddddd") -> void:
	if role == "assistant":
		var assistant_key := _message_fingerprint(text)
		if _rendered_assistant_keys.has(assistant_key):
			FrontendLogger.debug(editor_interface, "ChatPanel", "Skipped duplicate assistant message.", {
				"chars": text.length()
			})
			return
		_rendered_assistant_keys[assistant_key] = true

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	if role == "user":
		var spacer := Control.new()
		spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		spacer.size_flags_stretch_ratio = 0.35
		row.add_child(spacer)

	var panel := _make_message_panel(role)
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.size_flags_stretch_ratio = 0.65 if role == "user" else 1.0
	panel.custom_minimum_size = Vector2(320, 0) if role == "user" else Vector2(0, 0)
	row.add_child(panel)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)

	var rich := _make_rich_text(text, color)
	body.add_child(rich)

	_message_list.add_child(row)
	_scroll_to_bottom()


## 生成单个工具调用的 "⏺ ToolName(args)" 标题行，在执行/确认之前展示，
## 让用户能实时看到模型正在调用什么智能体/工具、读取或编辑了什么文件。
func _message_fingerprint(text: String) -> String:
	return " ".join(text.strip_edges().split())


func _format_tool_call_header(call: Dictionary) -> String:
	var name := str(call.get("name", "unknown"))
	var input: Dictionary = call.get("input", {}) if call.get("input") is Dictionary else {}
	var header := "⏺ **%s**(%s)" % [_tool_display_name(name), _format_tool_call_args(name, input)]
	var agent := str(call.get("agent", ""))
	if agent != "" and agent != "coordinator":
		header += " · `%s`" % agent
	return header


## 提取单个工具调用 "(...)" 部分的关键参数：路径类参数优先，其次是命令/智能体/
## 任务/查询等描述性参数；按需截断避免一行过长。
func _format_tool_call_args(name: String, input: Dictionary) -> String:
	if name == "run_tests" or name == "run_headless_self_test":
		return "kind=%s" % str(input.get("kind", "project"))
	for key in ["path", "target_path", "file_path", "script_path", "resource_path", "scene_path"]:
		if input.has(key):
			return str(input.get(key, ""))
	for key in ["command", "agent", "task", "query", "class_name", "node_path", "name"]:
		if input.has(key):
			return _truncate_text(str(input.get(key, "")), 60)
	return ""


## 生成单个工具调用结果的 "⎿ ..." 摘要行：读取类工具显示行数，写入/编辑类
## 工具显示路径与行数，测试/命令类工具显示状态与截断输出，出错/被拒绝的调用
## 显示对应说明。
func _format_tool_result_detail(name: String, input: Dictionary, status: String, result: Dictionary) -> String:
	var inner: Dictionary = result.get("result", {}) if result.get("result") is Dictionary else {}
	if status == "rejected":
		return "⎿ %s" % _ui("tool_rejected")
	if status == "error":
		var message := str(inner.get("message", result.get("error_code", _ui("tool_unknown_error"))))
		return "⎿ %s" % (_ui("tool_error_detail") % message)
	match name:
		"read_file", "read_script":
			return "⎿ %s" % (_ui("tool_read_lines") % _count_lines(str(inner.get("content", ""))))
		"write_file", "propose_script_edit", "apply_text_edit", "propose_tests", "propose_content_file":
			var after_text := str(input.get("content", input.get("after_text", "")))
			var path := str(inner.get("path", input.get("path", input.get("target_path", ""))))
			return "⎿ %s" % (_ui("tool_wrote_lines") % [path, _count_lines(after_text)])
		"run_tests", "run_headless_self_test":
			var run_status := str(inner.get("status", "unknown"))
			var exit_code = inner.get("exit_code")
			var summary := run_status
			if exit_code != null:
				summary = _ui("tool_run_result") % [run_status, str(exit_code)]
			var output := str(inner.get("output", "")).strip_edges()
			if output != "":
				summary += "\n```\n%s\n```" % _truncate_text(output, 800)
			return "⎿ %s" % summary
		"read_debugger_errors":
			var items: Array = inner.get("items", []) if inner.get("items") is Array else []
			return "⎿ %s" % (_ui("tool_items_count") % items.size())
		_:
			if inner.has("path"):
				return "⎿ %s" % (_ui("tool_done_path") % str(inner.get("path")))
			return "⎿ %s" % _ui("tool_done")


## 在 "⎿" 结果行之后追加一条结果消息；失败/被拒绝的调用使用醒目颜色。
func _append_tool_result(call: Dictionary, result: Dictionary) -> void:
	var name := str(call.get("name", "unknown"))
	var status := str(result.get("status", ""))
	var input: Dictionary = call.get("input", {}) if call.get("input") is Dictionary else {}
	var detail := _format_tool_result_detail(name, input, status, result)
	var color := "#e08080" if status == "error" else "#dddddd"
	_append_message("system", detail, color)


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
	_apply_mono_font(rich)
	rich.append_text(_markdown_to_bbcode(text))
	return rich


## 为 `[code]`（含行内代码与代码块）配置等宽字体，优先复用编辑器自带的源码字体。
func _apply_mono_font(rich: RichTextLabel) -> void:
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


func _clear_messages() -> void:
	_finish_streaming()
	_rendered_assistant_keys.clear()
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
	var code_lang := ""
	var lines := text.split("\n")
	var tree_ranges := _find_tree_block_ranges(lines)
	var tree_range_index := 0
	var index := 0
	while index < lines.size():
		var line := str(lines[index])
		if line.begins_with("```"):
			if in_code:
				in_code = false
				code_lang = ""
				result.append("[/code][/bgcolor]")
			else:
				in_code = true
				code_lang = _normalize_code_lang(line.substr(3))
				result.append("[bgcolor=%s][code]" % _CODE_BLOCK_BG)
			index += 1
			continue
		if in_code:
			result.append(_highlight_code_line(line, code_lang))
			index += 1
			continue
		while tree_range_index < tree_ranges.size() and int(tree_ranges[tree_range_index].y) <= index:
			tree_range_index += 1
		if tree_range_index < tree_ranges.size() and int(tree_ranges[tree_range_index].x) == index:
			var tree_range: Vector2i = tree_ranges[tree_range_index]
			result.append("[bgcolor=%s][code]" % _CODE_BLOCK_BG)
			for tree_index in range(tree_range.x, tree_range.y):
				result.append(_escape_bbcode(str(lines[tree_index])))
			result.append("[/code][/bgcolor]")
			index = tree_range.y
			tree_range_index += 1
			continue
		if _looks_like_table_start(lines, index):
			var table_lines: Array[String] = []
			while index < lines.size() and str(lines[index]).contains("|"):
				table_lines.append(str(lines[index]))
				index += 1
			result.append(_render_markdown_table(table_lines))
			continue
		result.append(_markdown_line_to_bbcode(line))
		index += 1
	if in_code:
		result.append("[/code][/bgcolor]")
	return "\n".join(result)


## 将代码块的语言标记归一化（如 ```gd``` -> gdscript），未知/缺省语言返回空串。
func _normalize_code_lang(raw: String) -> String:
	var token := raw.strip_edges().split(" ")[0].to_lower()
	if _CODE_LANG_ALIASES.has(token):
		return str(_CODE_LANG_ALIASES[token])
	return token


## 渲染 markdown 表格为真实的 `[table]`/`[cell]` BBCode 网格。
func _render_markdown_table(table_lines: Array[String]) -> String:
	if table_lines.size() < 2:
		return _escape_bbcode("\n".join(table_lines))
	var header_cells := _split_table_row(table_lines[0])
	if header_cells.is_empty():
		return _escape_bbcode("\n".join(table_lines))
	var column_count := header_cells.size()
	var bbcode := "[table=%d]" % column_count
	for cell in header_cells:
		bbcode += "[cell][b]%s[/b][/cell]" % _format_table_cell(str(cell))
	for row_index in range(2, table_lines.size()):
		var cells := _split_table_row(table_lines[row_index])
		for col_index in range(column_count):
			var cell_text := str(cells[col_index]) if col_index < cells.size() else ""
			bbcode += "[cell]%s[/cell]" % _format_table_cell(cell_text)
	bbcode += "[/table]"
	return bbcode


func _format_table_cell(cell: String) -> String:
	var escaped := _escape_bbcode(cell.strip_edges())
	escaped = _replace_inline_code(escaped)
	escaped = _replace_bold(escaped)
	return escaped


func _split_table_row(line: String) -> PackedStringArray:
	var trimmed := line.strip_edges()
	if trimmed.begins_with("|"):
		trimmed = trimmed.substr(1)
	if trimmed.ends_with("|"):
		trimmed = trimmed.substr(0, trimmed.length() - 1)
	return trimmed.split("|")


func _markdown_line_to_bbcode(line: String) -> String:
	var escaped := _escape_bbcode(line)
	var stripped := line.strip_edges()
	if stripped == "---" or stripped == "***" or stripped == "___":
		return "[color=#666666]────────────────────────[/color]"
	if line.begins_with("### "):
		return "[b]" + _escape_bbcode(line.substr(4)) + "[/b]"
	if line.begins_with("## "):
		return "[font_size=18][b]" + _escape_bbcode(line.substr(3)) + "[/b][/font_size]"
	if line.begins_with("# "):
		return "[font_size=20][b]" + _escape_bbcode(line.substr(2)) + "[/b][/font_size]"
	if line.begins_with("- "):
		escaped = "• " + _escape_bbcode(line.substr(2))
	elif _begins_with_ordered_list(line):
		escaped = _escape_bbcode(line)
	escaped = _replace_inline_code(escaped)
	escaped = _replace_bold(escaped)
	return escaped


func _looks_like_table_start(lines: PackedStringArray, index: int) -> bool:
	if index + 1 >= lines.size():
		return false
	var line := str(lines[index])
	var next := str(lines[index + 1])
	return line.contains("|") and _is_markdown_table_separator(next)


func _is_markdown_table_separator(line: String) -> bool:
	var stripped := line.strip_edges()
	if not stripped.contains("|") or not stripped.contains("-"):
		return false
	var allowed := "|-: "
	for index in range(stripped.length()):
		var character := stripped.substr(index, 1)
		if not allowed.contains(character):
			return false
	return true


## 判断一行是否包含目录树/连线图常用的连线字符（Unicode 制表符或其 ASCII 变体）。
func _looks_like_tree_line(line: String) -> bool:
	for character in _TREE_LINE_CHARS:
		if line.contains(character):
			return true
	return line.contains("+-- ") or line.contains("|-- ") or line.contains("`-- ")


## 预扫描整段 markdown 文本，找出包含连线字符且未被代码围栏包裹的段落
## （以空行或代码围栏分隔），返回这些段落的 [起始行, 结束行) 区间。
## 这类段落（如"文件树"）整体会被当作等宽代码块渲染，否则连线在比例
## 字体下无法对齐。
func _find_tree_block_ranges(lines: PackedStringArray) -> Array:
	var ranges: Array = []
	var in_code := false
	var paragraph_start := -1
	var paragraph_has_tree := false
	for index in range(lines.size()):
		var line := str(lines[index])
		if line.begins_with("```"):
			if paragraph_start >= 0:
				if paragraph_has_tree:
					ranges.append(Vector2i(paragraph_start, index))
				paragraph_start = -1
				paragraph_has_tree = false
			in_code = not in_code
			continue
		if in_code:
			continue
		if line.strip_edges() == "":
			if paragraph_start >= 0:
				if paragraph_has_tree:
					ranges.append(Vector2i(paragraph_start, index))
				paragraph_start = -1
				paragraph_has_tree = false
			continue
		if paragraph_start < 0:
			paragraph_start = index
		if _looks_like_tree_line(line):
			paragraph_has_tree = true
	if paragraph_start >= 0 and paragraph_has_tree:
		ranges.append(Vector2i(paragraph_start, lines.size()))
	return ranges


func _begins_with_ordered_list(line: String) -> bool:
	var dot_index := line.find(". ")
	if dot_index <= 0 or dot_index > 4:
		return false
	for index in range(dot_index):
		var code := line.unicode_at(index)
		if code < 48 or code > 57:
			return false
	return true


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


## 对代码块中的一行做轻量语法高亮：先按语言的行注释前缀分离注释，
## 再对剩余部分做字符串/数字/关键字着色，其余文本仅做 BBCode 转义。
func _highlight_code_line(line: String, lang: String) -> String:
	var comment_prefix: String = _CODE_LINE_COMMENT.get(lang, "")
	if comment_prefix != "":
		var comment_index := _find_comment_index(line, comment_prefix)
		if comment_index >= 0:
			var code_part := line.substr(0, comment_index)
			var comment_part := line.substr(comment_index)
			return _highlight_code_segment(code_part, lang) \
				+ "[color=%s]%s[/color]" % [_SYNTAX_COMMENT_COLOR, _escape_bbcode(comment_part)]
	return _highlight_code_segment(line, lang)


## 查找行注释前缀在代码行中的起始位置；若前缀出现在字符串字面量内则忽略，返回 -1。
func _find_comment_index(line: String, prefix: String) -> int:
	var in_string := ""
	var index := 0
	while index < line.length():
		var character := line.substr(index, 1)
		if in_string != "":
			if character == "\\":
				index += 2
				continue
			if character == in_string:
				in_string = ""
			index += 1
			continue
		if character == "\"" or character == "'":
			in_string = character
			index += 1
			continue
		if line.substr(index, prefix.length()) == prefix:
			return index
		index += 1
	return -1


## 对一段不含注释的代码文本做字符串/数字/关键字着色，其余字符做 BBCode 转义。
func _highlight_code_segment(text: String, lang: String) -> String:
	var keywords: Array = _CODE_KEYWORDS.get(lang, [])
	var result := ""
	var index := 0
	var length := text.length()
	while index < length:
		var code := text.unicode_at(index)
		var character := text.substr(index, 1)
		if character == "\"" or character == "'":
			var quote := character
			var end := index + 1
			while end < length:
				var next_char := text.substr(end, 1)
				if next_char == "\\":
					end += 2
					continue
				end += 1
				if next_char == quote:
					break
			var literal := text.substr(index, end - index)
			result += "[color=%s]%s[/color]" % [_SYNTAX_STRING_COLOR, _escape_bbcode(literal)]
			index = end
			continue
		if (code >= 65 and code <= 90) or (code >= 97 and code <= 122) or code == 95:
			var end := index
			while end < length:
				var next_code := text.unicode_at(end)
				var is_word := (next_code >= 65 and next_code <= 90) \
					or (next_code >= 97 and next_code <= 122) \
					or (next_code >= 48 and next_code <= 57) \
					or next_code == 95
				if not is_word:
					break
				end += 1
			var word := text.substr(index, end - index)
			if keywords.has(word):
				result += "[color=%s]%s[/color]" % [_SYNTAX_KEYWORD_COLOR, _escape_bbcode(word)]
			else:
				result += _escape_bbcode(word)
			index = end
			continue
		if code >= 48 and code <= 57:
			var end := index
			while end < length:
				var next_char := text.substr(end, 1)
				if (next_char.unicode_at(0) >= 48 and next_char.unicode_at(0) <= 57) or next_char == ".":
					end += 1
				else:
					break
			result += "[color=%s]%s[/color]" % [_SYNTAX_NUMBER_COLOR, _escape_bbcode(text.substr(index, end - index))]
			index = end
			continue
		result += _escape_bbcode(character)
		index += 1
	return result
