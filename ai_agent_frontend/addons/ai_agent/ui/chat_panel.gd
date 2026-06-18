@tool
extends VBoxContainer

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const ContextCollector = preload("res://addons/ai_agent/context/context_collector.gd")
const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")
const ChatPanelText = preload("res://addons/ai_agent/ui/chat_panel_text.gd")
const ChatPanelTheme = preload("res://addons/ai_agent/ui/chat_panel_theme.gd")
const InlineToolConfirmation = preload("res://addons/ai_agent/ui/inline_tool_confirmation.gd")
const LogEntryRenderer = preload("res://addons/ai_agent/ui/log_entry_renderer.gd")
const MarkdownRenderer = preload("res://addons/ai_agent/ui/markdown_renderer.gd")
const RecoveryPrompt = preload("res://addons/ai_agent/ui/recovery_prompt.gd")
const ToolExecutor = preload("res://addons/ai_agent/tools/tool_executor.gd")
const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")

enum AgentState { IDLE, WAITING_LLM, WAITING_CONFIRM, EXECUTING, COMPACTING }

const PENDING_TOOL_RESULTS_ERROR := "当前会话仍有待回传的工具结果，不能开始新的用户消息"
const STREAM_RENDER_INTERVAL_MS := 120
const REASONING_RENDER_INTERVAL_MS := 250
const EVENT_DRAIN_BATCH_SIZE := 24
const EVENT_DRAIN_TIME_BUDGET_MS := 6
const MAX_MESSAGE_LIST_CHILDREN := 240
const MAX_LIVE_RENDER_CHARS := 60000
const MAX_MESSAGE_RENDER_CHARS := 90000
const MAX_REASONING_RENDER_CHARS := 30000
## Plan/Verify 的展示性事件：通常没有活跃 LLM 文本流陪同到达，需要强制滚动一次，
## 否则容易在 ScrollContainer 重新计算高度期间被误判为"用户已上滑"而停止跟随。
const _MILESTONE_EVENT_TYPES := {
	"plan_created": true,
	"plan_step_started": true,
	"plan_step_completed": true,
	"verify_started": true,
	"verify_completed": true,
}

var editor_interface: EditorInterface
var service: Node
var state_store: Node
var undo_manager: Node

var _http_client: Node
var _collector: Node
var _tool_executor: Node
var _recovery_prompt: ConfirmationDialog
var _log_renderer: LogEntryRenderer

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
var _inline_confirm := InlineToolConfirmation.new()
var _interrupted_locally := false
var _indent_current_text := false
var _event_queue: Array = []
var _draining_events := false
var _force_scroll_once := false

var _stream_key := ""
var _stream_row: Control
var _stream_content_rich: RichTextLabel
var _stream_started_ms: int = -1
var _stream_display_text := ""
var _stream_text_dirty := false
var _stream_last_render_ms := 0
var _reasoning_key := ""
var _reasoning_toggle: Button
var _reasoning_detail_rich: RichTextLabel
var _reasoning_text := ""
var _reasoning_started_ms: int = -1
var _reasoning_text_dirty := false
var _reasoning_last_render_ms := 0
var _rendered_assistant_keys := {}
var _live_response_keys := {}   # 仅追踪本轮实时响应，避免历史加载的指纹误判为重复
var _closed_stream_keys := {}
var _closed_reasoning_keys := {}   # 新增：专门追踪已关闭的 reasoning stream
var _theme_colors: Dictionary = {}
var _auto_scroll := true
var _suppress_scroll_check := false   # 程序滚动时抑制 value_changed 误判
var _post_final_scroll_frames := 0   # final 响应后持续滚动到底部的剩余帧数
var _post_delta_scroll_frames := 0   # 文本流刷新后持续滚动到底部的剩余帧数（避免每帧都强制滚动）
var _empty_final_ignored_ms: int = -1   # 空 final 被忽略的时间戳，超时后强制结束 turn


func _ready() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Initializing chat panel.")
	_refresh_theme_colors()
	_build_ui()
	_build_children()
	_connect_signals()
	_set_state(AgentState.IDLE)
	_fetch_initial_service_data()


func _process(_delta: float) -> void:
	if _stream_text_dirty and _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		var now_ms := Time.get_ticks_msec()
		if _stream_last_render_ms == 0 or now_ms - _stream_last_render_ms >= STREAM_RENDER_INTERVAL_MS:
			_render_stream_content()
	if _post_delta_scroll_frames > 0 and _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		_post_delta_scroll_frames -= 1
		_do_scroll_to_bottom()
	# final 响应后连续多帧强制滚动到底部，等待 fit_content RichTextLabel 完成布局
	if _post_final_scroll_frames > 0:
		_post_final_scroll_frames -= 1
		_do_scroll_to_bottom()
	if _reasoning_toggle != null and is_instance_valid(_reasoning_toggle) and _reasoning_started_ms >= 0:
		_reasoning_toggle.text = "✻  " + _format_reasoning_header()
	if _reasoning_text_dirty and _reasoning_detail_rich != null and is_instance_valid(_reasoning_detail_rich):
		var reasoning_now_ms := Time.get_ticks_msec()
		if _reasoning_last_render_ms == 0 or reasoning_now_ms - _reasoning_last_render_ms >= REASONING_RENDER_INTERVAL_MS:
			_render_reasoning_entry()
	# 空 final 超时兜底：收到空 final 后 60 秒内没有真正的 final 到来，强制结束 turn
	if _empty_final_ignored_ms >= 0 and _state != AgentState.IDLE:
		var elapsed_ms := Time.get_ticks_msec() - _empty_final_ignored_ms
		if elapsed_ms > 60000:
			FrontendLogger.warn(editor_interface, "ChatPanel", "[handle_final] TIMEOUT: no real final after 60s, forcing IDLE", {
				"elapsed_ms": str(elapsed_ms)
			})
			_empty_final_ignored_ms = -1
			_mark_current_stream_closed()
			_mark_reasoning_stream_closed()
			_finish_reasoning_stream()
			_finish_streaming()
			_append_message("system", "⚠ 服务端未返回最终回复，已自动结束。")
			if undo_manager != null:
				undo_manager.commit_batch()
			_set_state(AgentState.IDLE)
			_http_client.current_turn_id = ""
			if state_store != null:
				state_store.set_value("current_turn_id", "")
				state_store.set_value("pending_calls", [])


func _render_stream_content() -> void:
	if _stream_content_rich == null or not is_instance_valid(_stream_content_rich):
		return
	_stream_content_rich.clear()
	_stream_content_rich.append_text(MarkdownRenderer.markdown_to_bbcode(
		_limit_render_text(_stream_display_text, MAX_LIVE_RENDER_CHARS),
		_theme_colors
	))
	_stream_text_dirty = false
	_stream_last_render_ms = Time.get_ticks_msec()
	if _auto_scroll:
		# 内容刚刷新，给 fit_content RichTextLabel 几帧时间稳定布局后再滚动，而不是
		# 像之前那样不管有没有新内容都每帧滚动一次——长会话里 _message_list 子节点
		# 一多，每帧都强制重算 ScrollContainer 高度是明显的卡顿来源（§界面卡顿）。
		_post_delta_scroll_frames = 2


func _render_reasoning_entry() -> void:
	if _reasoning_detail_rich == null or not is_instance_valid(_reasoning_detail_rich):
		return
	_reasoning_detail_rich.clear()
	_reasoning_detail_rich.append_text(MarkdownRenderer.markdown_to_bbcode(
		_limit_render_text(_reasoning_text, MAX_REASONING_RENDER_CHARS),
		_theme_colors
	))
	_reasoning_text_dirty = false
	_reasoning_last_render_ms = Time.get_ticks_msec()
	_scroll_to_bottom()


func _limit_render_text(text: String, max_chars: int) -> String:
	if max_chars <= 0 or text.length() <= max_chars:
		return text
	return text.left(max_chars) + "\n\n... (display truncated)"


## 程序控制滚动到底部，设置抑制标志防止 value_changed 误判
func _do_scroll_to_bottom() -> void:
	_suppress_scroll_check = true
	_scroll.scroll_vertical = 999999
	# 布局可能需要 1-2 帧才能稳定，用 call_deferred 链延长抑制窗口
	call_deferred("_reset_scroll_suppress_deferred")


func _reset_scroll_suppress_deferred() -> void:
	# 再延迟一帧确保布局完全稳定后才解除抑制
	call_deferred("_reset_scroll_suppress")


func _reset_scroll_suppress() -> void:
	_suppress_scroll_check = false


func _notification(what: int) -> void:
	if what == NOTIFICATION_THEME_CHANGED:
		_refresh_theme_colors()
		_refresh_live_theme_overrides()


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

	_log_renderer = LogEntryRenderer.new()
	_log_renderer.theme_colors = _theme_colors
	_log_renderer.editor_interface = editor_interface


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
	_scroll.get_v_scroll_bar().value_changed.connect(_on_scroll_value_changed)
	if service != null:
		service.service_started.connect(_on_service_started)
		service.service_failed.connect(_on_service_failed)


func _refresh_theme_colors() -> void:
	ChatPanelTheme.refresh_theme_colors(self, editor_interface, _theme_colors)


func _refresh_live_theme_overrides() -> void:
	if _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		_stream_content_rich.add_theme_color_override("default_color", _theme_color("text"))
	if _reasoning_toggle != null and is_instance_valid(_reasoning_toggle):
		ChatPanelTheme.set_button_text_colors(_reasoning_toggle, _theme_color("muted_text"), _theme_color("hover_text"))
	if _reasoning_detail_rich != null and is_instance_valid(_reasoning_detail_rich):
		_reasoning_detail_rich.add_theme_color_override("default_color", _theme_color("muted_text"))


func _theme_color(key: String) -> Color:
	return ChatPanelTheme.theme_color(_theme_colors, key)


func _on_send() -> void:
	var text := _input.text.strip_edges()
	if text == "" or _state != AgentState.IDLE:
		FrontendLogger.debug(editor_interface, "ChatPanel", "Ignored send request.", {
			"empty": text == "",
			"state": _status.text
		})
		return
	FrontendLogger.info(editor_interface, "ChatPanel", "Sending user message.", {"chars": text.length(), "text": text})
	_auto_scroll = true
	_force_scroll_once = true
	_interrupted_locally = false
	_finish_streaming()
	_mark_reasoning_stream_closed()
	_finish_reasoning_stream()
	_closed_stream_keys.clear()
	_closed_reasoning_keys.clear()   # 新增
	_live_response_keys.clear()
	_empty_final_ignored_ms = -1   # 重置空 final 超时计时器
	_input.clear()
	# 在用户消息之前追加两条空白消息：强制撑开 ScrollContainer 使滚动到底部，
	# 同时作为上一轮回复与当前用户消息之间的视觉间距
	_append_message("system", " ")
	_append_message("system", " ")
	_append_message("user", text)
	_append_message("system", _ui("waiting_model"))
	_set_state(AgentState.WAITING_LLM)
	if undo_manager != null:
		undo_manager.begin_batch("AI: " + text.left(40))
	_http_client.send_user_message(text, _collector.collect("any"))


func _on_response(response: Dictionary) -> void:
	var resp_type := str(response.get("type", "data"))
	if _interrupted_locally and resp_type in ["tool_calls", "final", "error"]:
		FrontendLogger.info(editor_interface, "ChatPanel", "Suppressed response after interrupt.", {
			"type": resp_type
		})
		return
	FrontendLogger.debug(editor_interface, "ChatPanel", "Handling response.", {
		"type": resp_type,
		"keys": response.keys(),
		"text_len": str(response.get("text", "")).length()
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

	if response.has("ok") and response.has("session_id") and response.size() == 2:
		FrontendLogger.debug(editor_interface, "ChatPanel", "Reset acknowledged.", {"session_id": str(response.get("session_id", ""))})
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
			FrontendLogger.debug(editor_interface, "ChatPanel", "[response] -> route: tool_calls")
			_handle_tool_calls(response)
		"final":
			FrontendLogger.debug(editor_interface, "ChatPanel", "[response] -> route: final")
			_handle_final(response)
		"error":
			FrontendLogger.debug(editor_interface, "ChatPanel", "[response] -> route: error", {
				"text": str(response.get("text", ""))
			})
			_on_error(str(response.get("text", "Unknown error")))
		_:
			FrontendLogger.debug(editor_interface, "ChatPanel", "[response] -> route: unknown", {
				"type": str(response.get("type", ""))
			})
			_append_message("system", JSON.stringify(response, "\t"))


func _handle_tool_calls(response: Dictionary) -> void:
	var calls: Array = response.get("calls", [])
	if _state == AgentState.WAITING_CONFIRM:
		FrontendLogger.warn(editor_interface, "ChatPanel", "Ignoring tool_calls while a previous batch is still pending confirmation.", {"count": calls.size()})
		return

	_mark_current_stream_closed()
	_finalize_stream_as_persistent()
	# 模型这一轮已经决定调用工具，说明它的思考已经结束——必须在这里冻结计时器，
	# 否则 _process() 会一直用 _reasoning_started_ms 刷新这条 "Thought for Xs"，
	# 直到下一轮 reasoning_delta 到来才会被关闭，期间会一直跟着 Edit/确认框走。
	_mark_reasoning_stream_closed()
	_finish_reasoning_stream()
	var silent: Array = []
	var confirm: Array = []
	for call in calls:
		if call is Dictionary and bool(call.get("needs_confirm", false)):
			confirm.append(call)
		else:
			silent.append(call)
	var call_names: Array = []
	for call in calls:
		if call is Dictionary:
			call_names.append(str(call.get("name", "")))
	FrontendLogger.info(editor_interface, "ChatPanel", "Handling tool calls.", {
		"count": calls.size(), "silent": silent.size(), "confirm": confirm.size(), "names": call_names
	})

	# workflow 工具（Edit/Write）的"宣告"消息直接跳过：确认框本身（confirm 分支）
	# 或下面紧接着渲染的 diff 预览（silent 分支）已经表达了同样的信息，等结果出来
	# 后只追加一条合并了 diff 的永久条目，避免一次编辑显示成两个工作流条目。
	for call in confirm:
		if call is Dictionary and not EventFormatter.is_workflow_tool(str(call.get("name", ""))):
			_append_message("system", EventFormatter.format_tool_call_header(call))

	if state_store != null:
		state_store.set_value("current_turn_id", _http_client.current_turn_id)
		state_store.set_value("pending_calls", confirm)

	var results: Array = []
	for call in silent:
		if call is Dictionary:
			if _interrupted_locally:
				return
			var is_workflow := EventFormatter.is_workflow_tool(str(call.get("name", "")))
			var preview: Control = null
			var stats := {}
			if is_workflow:
				# 必须在执行前渲染：之后文件已经被改写成 after_text，就读不到
				# 真正的 before 内容了。
				preview = ToolPreviewRenderer.render_call(call, _theme_colors)
				stats = ToolPreviewRenderer.diff_stats(call)
			else:
				_append_message("system", EventFormatter.format_tool_call_header(call))
			_set_state(AgentState.EXECUTING)
			var result: Dictionary = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			results.append(result)
			_append_tool_result(call, result, preview, stats)

	if not confirm.is_empty():
		FrontendLogger.info(editor_interface, "ChatPanel", "Waiting for inline tool confirmation.", {"count": confirm.size()})
		_pending_calls = confirm.duplicate(true)
		_pending_silent_results = results.duplicate(true)
		_show_inline_confirmation(confirm.duplicate(true))
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
	if results.is_empty():
		FrontendLogger.warn(editor_interface, "ChatPanel", "No tool results to submit; ending turn gracefully instead of erroring.", {})
		if undo_manager != null:
			undo_manager.abort_batch()
		if _http_client != null:
			_http_client.current_turn_id = ""
			_http_client.discard_pending()
		if state_store != null:
			state_store.set_value("current_turn_id", "")
		_set_state(AgentState.IDLE)
		_append_message("system", _ui("rejected_turn_ended"))
		return
	_set_state(AgentState.WAITING_LLM)
	_http_client.send_tool_results(results)


## 移除文本中的 `<think>…</think>` XML 块及所有残余的 `</think>` 标签。
func _strip_think_xml(text: String) -> String:
	var result := text
	var start := result.find("<think>")
	while start != -1:
		var end_tag := result.find("</think>", start)
		if end_tag == -1:
			result = result.substr(0, start)
			break
		result = result.substr(0, start) + result.substr(end_tag + "</think>".length())
		start = result.find("<think>")
	result = result.replace("</think>", "")
	# 如果移除 <think> 块后文本变空或几乎为空，记录警告
	if result.strip_edges().is_empty() and text.strip_edges().length() > 10:
		FrontendLogger.debug(editor_interface, "ChatPanel", "[strip_think_xml] WARNING: text becomes EMPTY after stripping", {
			"original_length": text.strip_edges().length(),
			"preview": text.left(100).replace("\n", "\\n")
		})
	return result


## 若回复以 `Thought: ...` 摘要行开头，拆分出摘要文本与剩余正文。
func _split_thought_summary(text: String) -> Dictionary:
	var stripped := text.strip_edges()
	if not stripped.begins_with("Thought:"):
		return {"summary": "", "rest": text}
	var newline := stripped.find("\n")
	var first_line := stripped if newline == -1 else stripped.substr(0, newline)
	var rest := "" if newline == -1 else stripped.substr(newline + 1)
	FrontendLogger.debug(editor_interface, "ChatPanel", "[split_thought_summary] Thought found", {
		"summary_len": first_line.length(), "rest_len": rest.strip_edges().length()
	})
	# 如果 Thought 之后没有正文，记录警告
	if rest.strip_edges().is_empty():
		FrontendLogger.debug(editor_interface, "ChatPanel", "[split_thought_summary] WARNING: no body text after Thought summary", {
			"preview": text.left(150).replace("\n", "\\n")
		})
	return {
		"summary": first_line.substr("Thought:".length()).strip_edges(),
		"rest": rest.strip_edges()
	}


## 把模型最终回复开头的 `Thought: ...` 摘要行转换为可折叠 workflow 条目格式。
func _apply_thought_prefix(text: String) -> String:
	var parts := _split_thought_summary(text)
	var summary := str(parts.get("summary", ""))
	if summary == "":
		return text
	var elapsed := 0.0
	if _stream_started_ms >= 0:
		elapsed = maxf(0.01, (Time.get_ticks_msec() - _reasoning_started_ms) / 1000.0)
	var thought_line := "Thought for %.2fs > %s" % [elapsed, summary]
	var rest := str(parts.get("rest", ""))
	if rest == "":
		return thought_line
	return "%s\n\n%s" % [thought_line, rest]


func _handle_final(response: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Received final response.", {
		"chars": str(response.get("text", "")).length()
	})
	var text := str(response.get("text", ""))
	var assistant_key := _message_fingerprint(text)
	var split := _split_thought_summary(text)
	var rest := str(split.get("rest", ""))
	var render_text := rest if rest.strip_edges() != "" else text
	FrontendLogger.debug(editor_interface, "ChatPanel", "[handle_final]", {
		"text_len": text.length(),
		"render_text_len": render_text.strip_edges().length()
	})
	if render_text.strip_edges().is_empty():
		# 空的 final 只是 agent 中间轮次的心跳/占位，不代表真正回复结束。
		# 跳过所有关闭和状态切换，继续等下一个非空 final。
		if _empty_final_ignored_ms < 0:
			_empty_final_ignored_ms = Time.get_ticks_msec()
		FrontendLogger.debug(editor_interface, "ChatPanel", "[handle_final] EMPTY final ignored, still waiting", {
			"preview": text.left(200).replace("\n", "\\n"),
			"since_ms": _empty_final_ignored_ms
		})
		return
	# 收到非空 final，重置空 final 计时器
	_empty_final_ignored_ms = -1
	render_text = render_text + "\n\n"
	_mark_current_stream_closed()
	_mark_reasoning_stream_closed()   # 阻止后续迟到的 reasoning delta
	_finish_reasoning_stream()         # 置空 toggle，停止 _process 刷新

	# 用 _live_response_keys（每次 send 清空）判断本轮是否已渲染，避免历史加载的
	# 指纹污染 _rendered_assistant_keys 导致当前回复被误判为重复而丢弃。
	if not _live_response_keys.has(assistant_key):
		_live_response_keys[assistant_key] = true
		_rendered_assistant_keys[assistant_key] = true
		if _stream_content_rich != null and is_instance_valid(_stream_content_rich):
			_stream_content_rich.clear()
			_stream_content_rich.append_text(MarkdownRenderer.markdown_to_bbcode(
				_limit_render_text(render_text, MAX_MESSAGE_RENDER_CHARS),
				_theme_colors
			))
			_finish_streaming()
		else:
			_discard_stream_message()
			_indent_current_text = true
			_append_log_stream_message(render_text)
			_indent_current_text = false
		# 无论走流式还是非流式路径，都在 _process 中连续多帧强制滚动到底部，
		# 等待 fit_content RichTextLabel 完成复杂 Markdown（表格、代码块）的布局计算
		_auto_scroll = true
		_force_scroll_once = true
		_do_scroll_to_bottom()
		_post_final_scroll_frames = 10
	else:
		FrontendLogger.debug(editor_interface, "ChatPanel", "Skipped duplicate final response.", {"key_len": assistant_key.length()})
		_discard_stream_message()
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
	# 消息列表已有内容说明当前会话已在进行，忽略此次历史加载（防止服务重启触发
	# 的意外历史响应覆盖当前对话内容）。
	if _message_list.get_child_count() > 0:
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored session history: messages already present.", {
			"count": _message_list.get_child_count()
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
		var text := _normalize_history_text(role, _strip_think_xml(str(item.get("text", "")))).strip_edges()
		if text == "":
			continue
		# agent_tool_calls 事件对应的历史条目，实时对话不显示，历史也跳过
		if text.begins_with("Tool calls"):
			continue
		var processed := _apply_thought_prefix(text) if role == "assistant" else text
		if role == "assistant":
			_indent_current_text = true
			_append_message(role, processed)
			_indent_current_text = false
		else:
			_append_message(role, processed)
	if pending_turn_id != null:
		_append_message("system", _ui("recovered_pending") % [session_id, str(pending_turn_id)])
		_show_pending_results_notice()
		_set_state(AgentState.WAITING_CONFIRM)
	if not items.is_empty():
		_append_message("system", _ui("history_restored") % str(items.size()))


func _normalize_history_text(role: String, text: String) -> String:
	if role != "system":
		return text
	var json_text := _extract_history_json_text(text)
	if not _looks_like_history_json_text(json_text):
		return text
	var parser := JSON.new()
	if parser.parse(json_text) != OK:
		return text
	var parsed = parser.data
	if not (parsed is Dictionary):
		return text
	var payload: Dictionary = parsed
	if _history_payload_has_delegate_results(payload):
		return _format_history_delegate_results(payload)
	if _history_payload_has_delegate_summary(payload):
		return _format_history_delegate_summary(payload)
	if _history_payload_has_plan_tasks(payload):
		return _format_history_plan_result(payload)
	return text


func _extract_history_json_text(text: String) -> String:
	var stripped := text.strip_edges()
	if stripped.begins_with("```json"):
		stripped = stripped.substr("```json".length()).strip_edges()
		if stripped.ends_with("```"):
			stripped = stripped.substr(0, stripped.length() - 3).strip_edges()
	return stripped


func _looks_like_history_json_text(text: String) -> bool:
	return text.strip_edges().begins_with("{")


func _history_payload_has_delegate_results(payload: Dictionary) -> bool:
	var results = payload.get("results")
	if not (results is Array) or results.is_empty():
		return false
	for item in results:
		if not (item is Dictionary) or not item.has("summary"):
			return false
	return true


func _history_payload_has_delegate_summary(payload: Dictionary) -> bool:
	return payload.has("summary") and not payload.has("results")


func _history_payload_has_plan_tasks(payload: Dictionary) -> bool:
	return bool(payload.get("ok", false)) and payload.get("tasks") is Array


func _format_history_delegate_results(payload: Dictionary) -> String:
	var lines := ["Delegate results:"]
	var results: Array = payload.get("results", [])
	var limit = mini(results.size(), 8)
	for index in range(limit):
		var item = results[index]
		if not (item is Dictionary):
			continue
		var agent := str(item.get("agent", "")).strip_edges()
		var summary := _truncate_history_markdown(str(item.get("summary", "")), 1600)
		lines.append("")
		lines.append("**%d. %s**" % [index + 1, agent if agent != "" else "delegate"])
		lines.append(summary if summary != "" else "No summary")
	if results.size() > limit:
		lines.append("")
		lines.append("... %d more result(s)" % [results.size() - limit])
	return "\n".join(lines)


func _format_history_delegate_summary(payload: Dictionary) -> String:
	var agent := str(payload.get("agent", "")).strip_edges()
	var summary := _truncate_history_markdown(str(payload.get("summary", "")), 2000)
	var title := "Delegate result: %s" % agent if agent != "" else "Delegate result:"
	return "%s\n%s" % [title, summary if summary != "" else "No summary"]


func _format_history_plan_result(payload: Dictionary) -> String:
	var lines := ["Plan created"]
	var tasks: Array = payload.get("tasks", [])
	var limit = mini(tasks.size(), 8)
	for index in range(limit):
		var task = tasks[index]
		if not (task is Dictionary):
			continue
		var agent := str(task.get("agent", "")).strip_edges()
		var label := _compact_history_summary(str(task.get("task", "")), 180)
		var suffix := " (%s)" % agent if agent != "" else ""
		lines.append("%d. %s%s" % [index + 1, label if label != "" else "Untitled task", suffix])
	if tasks.size() > limit:
		lines.append("... %d more task(s)" % [tasks.size() - limit])
	return "\n".join(lines)


func _compact_history_summary(text: String, max_chars: int) -> String:
	var compact := " ".join(text.strip_edges().split())
	if compact.length() > max_chars:
		return compact.left(max_chars) + "..."
	return compact


func _truncate_history_markdown(text: String, max_chars: int) -> String:
	var stripped := text.strip_edges()
	if stripped.length() > max_chars:
		return stripped.left(max_chars) + "\n... (truncated)"
	return stripped


func _on_error(message: String) -> void:
	FrontendLogger.error(editor_interface, "ChatPanel", "Agent error.", {"message": message})
	_append_message("error", message)
	_finish_streaming()
	_finish_reasoning_stream()
	if undo_manager != null:
		undo_manager.abort_batch()
	if state_store != null:
		state_store.set_value("pending_calls", [])
	_set_state(AgentState.IDLE)
	if message == PENDING_TOOL_RESULTS_ERROR or message.contains("工具结果") or message.contains("tool result"):
		_show_pending_results_notice()
		_set_state(AgentState.WAITING_CONFIRM)


func _show_pending_results_notice() -> void:
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var panel := _log_renderer.make_panel(_theme_color("panel_alt_bg"), _theme_color("panel_alt_border"))
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	row.add_child(panel)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)

	var label := _log_renderer.make_rich_text(_ui("pending_notice"))
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
	_set_state(AgentState.WAITING_LLM)


func _on_reset() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Reset requested.", {"state": _status.text})
	_auto_scroll = true
	_interrupted_locally = false
	_event_queue.clear()
	_draining_events = false
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
	_event_queue.clear()
	_draining_events = false
	_clear_inline_confirmation()
	_finish_streaming()
	_finish_reasoning_stream()
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
	var previous_session_id := _current_session_id()
	var session_id := "session_%d" % int(Time.get_unix_time_from_system())
	FrontendLogger.info(editor_interface, "ChatPanel", "New session requested.", {"session_id": session_id})
	_auto_scroll = true
	_interrupted_locally = false
	_event_queue.clear()
	_draining_events = false
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", session_id)
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _http_client != null:
		_http_client.start_new_session(previous_session_id, session_id)
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
	_clear_messages()   # 清空当前内容，确保历史加载时消息列表为空
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
	var coalesced := _coalesce_events(events)
	FrontendLogger.debug(editor_interface, "ChatPanel", "Handling events.", {
		"count": events.size(),
		"coalesced_count": coalesced.size()
	})
	if state_store != null and state_store.has_method("add_events"):
		state_store.add_events(coalesced)
	for event in coalesced:
		if event is Dictionary:
			var event_type := str(event.get("type", "<unknown>"))
			var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
			FrontendLogger.debug(editor_interface, "ChatPanel", "Event", {
				"type": event_type,
				"seq": int(event.get("seq", 0)),
				"payload_keys": payload.keys(),
				"text_len": str(payload.get("text", "")).length()
			})
			_event_queue.append(event)
	if not _draining_events:
		_drain_event_queue()


func _coalesce_events(events: Array) -> Array:
	var result: Array = []
	var latest_delta := {}
	var ordered_delta_keys: Array[String] = []
	for raw_event in events:
		if not (raw_event is Dictionary):
			continue
		var event: Dictionary = raw_event
		var event_type := str(event.get("type", ""))
		if event_type == "agent_reasoning_delta" or event_type == "agent_text_delta":
			_remember_delta_event(event, latest_delta, ordered_delta_keys)
			continue
		_flush_delta_events(result, latest_delta, ordered_delta_keys)
		result.append(event)
	_flush_delta_events(result, latest_delta, ordered_delta_keys)
	return result


func _remember_delta_event(event: Dictionary, latest_delta: Dictionary, ordered_delta_keys: Array[String]) -> void:
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	var key := "%s:%s:%s" % [
		str(event.get("type", "")),
		str(payload.get("frame_id", "")),
		str(payload.get("loop", ""))
	]
	if not latest_delta.has(key):
		ordered_delta_keys.append(key)
	latest_delta[key] = event


func _flush_delta_events(result: Array, latest_delta: Dictionary, ordered_delta_keys: Array[String]) -> void:
	for key in ordered_delta_keys:
		if latest_delta.has(key):
			result.append(latest_delta[key])
	latest_delta.clear()
	ordered_delta_keys.clear()


func _drain_event_queue() -> void:
	if _event_queue.is_empty():
		_draining_events = false
		return
	_draining_events = true
	if _interrupted_locally:
		_event_queue.clear()
		_draining_events = false
		return
	var started_ms := Time.get_ticks_msec()
	var processed := 0
	while not _event_queue.is_empty() and processed < EVENT_DRAIN_BATCH_SIZE:
		var event = _event_queue.pop_front()
		if event is Dictionary:
			_handle_event(event)
		processed += 1
		if Time.get_ticks_msec() - started_ms >= EVENT_DRAIN_TIME_BUDGET_MS:
			break
	if _event_queue.is_empty():
		_draining_events = false
	else:
		call_deferred("_drain_event_queue")


func _handle_event(event: Dictionary) -> void:
	var event_type := str(event.get("type", ""))
	if event_type == "agent_reasoning_delta":
		_on_reasoning_delta(event)
	elif event_type == "agent_text_delta":
		_on_text_delta(event)
	elif event_type == "final":
		var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
		if payload.has("text"):
			FrontendLogger.debug(editor_interface, "ChatPanel", "[event] -> route: final (via event stream)", {})
			_handle_final(payload)
	else:
		var previous_state := _state
		var is_compacting := event_type == "compact_boundary" and previous_state != AgentState.IDLE
		if is_compacting:
			_set_state(AgentState.COMPACTING)
		var description := EventFormatter.describe_event(event, _ui_table())
		if description != "":
			FrontendLogger.debug(editor_interface, "ChatPanel", "-> rendered", {
				"type": event_type,
				"description_len": description.length()
			})
			if _MILESTONE_EVENT_TYPES.has(event_type):
				_force_scroll_once = true
			_append_message("system", description)
		if is_compacting:
			_set_state(previous_state)


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
	_inline_confirm.show(_message_list, calls, _ui_table(), _theme_colors, _on_inline_apply, _on_inline_reject)
	_trim_message_list()
	# 确保确认面板出现时自动滚动到底部，让用户看到需要操作的内容
	_auto_scroll = true
	_force_scroll_once = true
	_do_scroll_to_bottom()
	# 布局可能需要额外帧才能稳定，用 _process 多帧兜底
	_post_final_scroll_frames = max(_post_final_scroll_frames, 5)


func _on_inline_apply() -> void:
	if _inline_confirm.is_busy():
		return
	_inline_confirm.set_busy(true)
	var results := _pending_silent_results.duplicate(true)
	for index in range(_pending_calls.size()):
		var call = _pending_calls[index]
		if not (call is Dictionary):
			continue
		var should_apply := _inline_confirm.should_apply(index)
		# 只对 workflow 工具（Edit/Write）合并成带 diff 的单条目；其它工具（如
		# set_node_property/add_node）走旧的"宣告 + 结果"两条文本消息——它们的
		# 宣告消息在上面没有被跳过，混进新面板只会再造一次重复条目。
		var is_workflow := EventFormatter.is_workflow_tool(str(call.get("name", "")))
		var preview := _inline_confirm.preview_for(index, is_workflow)
		var stats := _inline_confirm.diff_stats_for(index, is_workflow)
		if should_apply:
			if _interrupted_locally:
				return
			_set_state(AgentState.EXECUTING)
			var result: Dictionary = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			result["grant_session_allow"] = _inline_confirm.grant_session_allow()
			results.append(result)
			_append_tool_result(call, result, preview, stats)
		else:
			var rejected := AgentDTO.rejected_result(call)
			results.append(rejected)
			_append_tool_result(call, rejected, preview, stats)
	_inline_confirm.set_busy(false)
	_on_decision(results)


func _on_inline_reject() -> void:
	if _inline_confirm.is_busy():
		return
	_inline_confirm.set_busy(true)
	var calls := _pending_calls.duplicate(true)
	# 拒绝不等于挂断：把 rejected 结果回传给模型，让它读到"用户拒绝了这个
	# 编辑"之后继续给出建设性回复（如手动修改步骤、改成只读分析或降级
	# 方案），而不是前端单方面结束本轮、晾着用户。
	var results := _pending_silent_results.duplicate(true)
	for index in range(calls.size()):
		var call = calls[index]
		if not (call is Dictionary):
			continue
		var rejected := AgentDTO.rejected_result(call)
		results.append(rejected)
		var is_workflow := EventFormatter.is_workflow_tool(str(call.get("name", "")))
		var preview := _inline_confirm.preview_for(index, is_workflow)
		var stats := _inline_confirm.diff_stats_for(index, is_workflow)
		_append_tool_result(call, rejected, preview, stats)
	_inline_confirm.set_busy(false)
	_on_decision(results)


## 仅拆除确认框的 UI（旧的 checkbox/diff 预览/按钮），不触碰 `_pending_calls` /
## `_pending_silent_results`。`_show_inline_confirmation` 在构建新一轮确认框
## 前调用它来清掉上一轮遗留的控件——如果改用下面这个会清空 pending 数据的
## 完整版本，就会把调用者刚刚（在它之前一行）写入的 `_pending_calls` 清空，
## 导致确认框显示正常，但用户点"应用"/"拒绝"时已经没有数据可回传。
func _clear_inline_confirmation_ui() -> void:
	_inline_confirm.clear_ui()


func _clear_inline_confirmation() -> void:
	_clear_inline_confirmation_ui()
	_pending_calls.clear()
	_pending_silent_results.clear()


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
	var key := _stream_event_key(payload)
	if key != "" and _closed_reasoning_keys.has(key):
		FrontendLogger.debug(editor_interface, "ChatPanel", "[reasoning_delta] IGNORED - key already closed", {
			"key": key
		})
		return
	var text := str(payload.get("text", ""))
	FrontendLogger.debug(editor_interface, "ChatPanel", "[reasoning_delta]", {
		"key": key, "text_len": text.length(), "preview": text.left(60).replace("\n", "\\n")
	})
	_ensure_reasoning_entry(key)
	_reasoning_text = text
	_update_reasoning_entry()


func _on_text_delta(event: Dictionary) -> void:
	var payload: Dictionary = event.get("payload", {})
	var text := str(payload.get("text", ""))
	var key := _stream_event_key(payload)
	if _should_ignore_stream_delta(key, text):
		return
	_mark_reasoning_stream_closed()   # 防止迟到的 reasoning delta 再创建条目
	_finish_reasoning_stream()         # 置空 toggle，停止 _process 中的计时刷新
	if key != _stream_key:
		# 流式 key 变了（典型场景：coordinator 在服务端 delegate 给子 agent，
		# frame_id 从 f1 变成 f2，前端从未收到 tool_calls，_handle_tool_calls()
		# 不会被调用）。这里必须先把"旧 key"已经显示在屏幕上的文本保留为
		# 工作流条目，再开始新 key 的流式行——否则下面整段会直接覆盖
		# _stream_display_text，旧内容就在用户眼前消失得无影无踪。
		_finalize_stream_as_persistent()
	var stripped := _strip_think_xml(text)
	var parts := _split_thought_summary(stripped)
	var rest := str(parts.get("rest", ""))
	_stream_display_text = rest if rest.strip_edges() != "" else stripped
	_stream_text_dirty = true
	_ensure_stream_message(key, true)
	FrontendLogger.debug(editor_interface, "ChatPanel", "[text_delta]", {
		"key": key, "text_len": text.length(), "display_len": _stream_display_text.length(),
		"preview": text.left(40).replace("\n", "\\n")
	})


func _stream_event_key(payload: Dictionary) -> String:
	return "%s:%s" % [str(payload.get("frame_id", "")), str(payload.get("loop", ""))]


func _should_ignore_stream_delta(key: String, text: String) -> bool:
	if key != "" and _closed_stream_keys.has(key):
		return true
	# 只检查本轮实时响应的指纹，避免历史加载的 _rendered_assistant_keys 误拦截当前回复。
	var text_key := _message_fingerprint(text)
	if text_key != "" and _live_response_keys.has(text_key):
		if _stream_key == key:
			_discard_stream_message()
		return true
	return false


func _ensure_reasoning_entry(key: String) -> void:
	if _reasoning_key == key and _reasoning_detail_rich != null and is_instance_valid(_reasoning_detail_rich):
		return
	_mark_reasoning_stream_closed()
	_finish_reasoning_stream()
	_reasoning_key = key
	_reasoning_started_ms = Time.get_ticks_msec()
	if _stream_started_ms < 0:
		_stream_started_ms = _reasoning_started_ms

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	body.add_theme_constant_override("separation", 2)

	var toggle := _log_renderer.make_workflow_toggle(_format_reasoning_header(), _theme_color("muted_text"))
	var detail_rich := _log_renderer.append_collapsible(body, toggle, "", "✻")

	_message_list.add_child(body)
	_reasoning_toggle = toggle
	_reasoning_detail_rich = detail_rich
	_scroll_to_bottom()


func _update_reasoning_entry() -> void:
	if _reasoning_toggle != null and is_instance_valid(_reasoning_toggle):
		_reasoning_toggle.text = "✻  " + _format_reasoning_header()
	_reasoning_text_dirty = true


func _format_reasoning_header() -> String:
	var elapsed := 0.0
	if _reasoning_started_ms >= 0:
		elapsed = maxf(0.01, (Time.get_ticks_msec() - _reasoning_started_ms) / 1000.0)
	return "Thought for %.2fs" % elapsed


func _ensure_stream_message(key: String, indent := false) -> void:
	if _stream_key == key and _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		return
	_discard_stream_message()
	_stream_key = key
	_stream_started_ms = Time.get_ticks_msec()

	var content_rich := _log_renderer.make_log_rich_text("", null, "", indent)

	_message_list.add_child(content_rich)
	_stream_row = content_rich
	_stream_content_rich = content_rich
	_scroll_to_bottom()


func _finish_streaming() -> void:
	_stream_key = ""
	_stream_row = null
	_stream_content_rich = null
	_stream_started_ms = -1
	_stream_display_text = ""
	_stream_text_dirty = false
	_stream_last_render_ms = 0


func _finish_reasoning_stream() -> void:
	if _reasoning_text_dirty and _reasoning_detail_rich != null and is_instance_valid(_reasoning_detail_rich):
		_render_reasoning_entry()
	_reasoning_key = ""
	_reasoning_toggle = null
	_reasoning_detail_rich = null
	_reasoning_text = ""
	_reasoning_started_ms = -1
	_reasoning_text_dirty = false
	_reasoning_last_render_ms = 0


func _mark_current_stream_closed() -> void:
	if _stream_key != "":
		_closed_stream_keys[_stream_key] = true


func _mark_reasoning_stream_closed() -> void:
	if _reasoning_key != "":
		_closed_reasoning_keys[_reasoning_key] = true


func _discard_stream_message() -> void:
	if _stream_row != null and is_instance_valid(_stream_row):
		_stream_row.queue_free()
	_finish_streaming()


## 把 LLM 在决定调用工具之前已经输出的流式文本，从临时流式行转成持久的
## 工作流条目（带 `●`/`○` 前缀），而不是像 `_discard_stream_message()` 一样
## 直接丢弃——这样用户能看到模型在工具调用之间的说明/思考文字。
func _finalize_stream_as_persistent() -> void:
	var text := _stream_display_text
	if _stream_row != null and is_instance_valid(_stream_row):
		_stream_row.queue_free()
	if text.strip_edges() != "":
		_indent_current_text = true
		_append_log_stream_message(text, null, true)
		_indent_current_text = false
		_rendered_assistant_keys[_message_fingerprint(text)] = true
	_finish_streaming()


func _append_message(role: String, text: String, color = null) -> void:
	if role == "assistant":
		var assistant_key := _message_fingerprint(text)
		if _rendered_assistant_keys.has(assistant_key):
			FrontendLogger.debug(editor_interface, "ChatPanel", "Skipped duplicate assistant message.", {
				"chars": text.length()
		})
			return
		_rendered_assistant_keys[assistant_key] = true

	if role != "user" and role != "error":
		_append_log_stream_message(text, color, role != "assistant")
		return

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	if role == "user":
		var spacer := Control.new()
		spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		spacer.size_flags_stretch_ratio = 0.35
		row.add_child(spacer)

	var panel := _log_renderer.make_message_panel(role)
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.size_flags_stretch_ratio = 0.65 if role == "user" else 1.0
	panel.custom_minimum_size = Vector2(320, 0) if role == "user" else Vector2(0, 0)
	row.add_child(panel)

	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)

	var rich := _log_renderer.make_rich_text(_limit_render_text(text, MAX_MESSAGE_RENDER_CHARS), color)
	body.add_child(rich)

	_message_list.add_child(row)
	_scroll_to_bottom()


func _message_fingerprint(text: String) -> String:
	return " ".join(text.strip_edges().split())


func _append_log_stream_message(text: String, color = null, mark_text: bool = false) -> void:
	_log_renderer.append_log_stream_message(
		_message_list,
		_limit_render_text(text, MAX_MESSAGE_RENDER_CHARS),
		color,
		mark_text,
		_indent_current_text
	)
	_scroll_to_bottom()


## `preview`/`diff_stats` 仅在调用方已经为这个 call 渲染过 diff 预览时传入
## （即 workflow 类工具：Edit/Write）。传入时渲染一条带彩色 diff 的永久面板，
## 取代"宣告 + 结果"两条消息——避免同一次编辑在工作流列表里显示成两个条目。
func _append_tool_result(call: Dictionary, result: Dictionary, preview: Control = null, diff_stats: Dictionary = {}) -> void:
	var name := str(call.get("name", "unknown"))
	var status := str(result.get("status", ""))
	var input: Dictionary = call.get("input", {}) if call.get("input") is Dictionary else {}

	if preview != null and is_instance_valid(preview):
		_append_tool_result_panel(call, result, preview, diff_stats)
		return

	var detail := EventFormatter.format_tool_result_detail(name, input, status, result, _ui_table())
	if status == "applied":
		detail = EventFormatter.format_log_tool_result(name, input, result, detail)
	FrontendLogger.debug(editor_interface, "ChatPanel", "[tool_result]", {
		"name": name, "status": status, "detail_len": detail.length(),
		"detail": detail.left(120).replace("\n", "\\n")
	})
	var color := _theme_color("error_text") if status == "error" else _theme_color("text")
	_append_message("system", detail, color)


## 把已渲染好的 diff/参数预览（在工具执行前从确认框搬出，此时文件内容还是
## before_text）和执行结果合并成一条永久工作流条目：● 标记 + 标题行 + 缩进的
## diff 预览 + 状态行。特意不用卡片面板包起来——要和 Read/Grep 等其它工作流
## 条目保持同样的"⏺ 标记 + 纯文本行"外观，否则会显得是另一种不同的 UI 元素。
func _append_tool_result_panel(call: Dictionary, result: Dictionary, preview: Control, diff_stats: Dictionary) -> void:
	var status := str(result.get("status", ""))
	var content := VBoxContainer.new()
	content.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	content.add_theme_constant_override("separation", 2)

	var marker := _log_renderer.workflow_marker_text("edit")
	content.add_child(_log_renderer.make_log_rich_text(EventFormatter.format_tool_call_header(call), null, marker))

	var old_parent := preview.get_parent()
	if old_parent != null:
		old_parent.remove_child(preview)
	var indent := MarginContainer.new()
	indent.add_theme_constant_override("margin_left", 24)
	indent.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	indent.add_child(preview)
	content.add_child(indent)

	var name := str(call.get("name", "unknown"))
	var input: Dictionary = call.get("input", {}) if call.get("input") is Dictionary else {}
	var is_diff_kind := ToolPreviewRenderer.infer_render_kind(call) == "diff"

	var status_text := ""
	var status_color := _theme_color("text")
	match status:
		"applied":
			if is_diff_kind:
				status_text = "+%d -%d lines" % [int(diff_stats.get("added", 0)), int(diff_stats.get("removed", 0))]
			else:
				status_text = EventFormatter.format_tool_result_detail(name, input, status, result, _ui_table())
			status_color = _theme_color("success_text")
		"rejected":
			status_text = _ui("tool_rejected")
			status_color = _theme_color("muted_text")
		"error":
			status_text = EventFormatter.format_tool_result_detail(name, input, status, result, _ui_table())
			status_color = _theme_color("error_text")
		_:
			status_text = status
	if status_text != "":
		content.add_child(_log_renderer.make_log_rich_text(status_text, status_color))

	FrontendLogger.debug(editor_interface, "ChatPanel", "[tool_result]", {
		"name": name, "status": status, "status_text": status_text
	})

	_message_list.add_child(content)
	_scroll_to_bottom()


func _clear_messages() -> void:
	_finish_streaming()
	_finish_reasoning_stream()
	_rendered_assistant_keys.clear()
	_live_response_keys.clear()
	_closed_stream_keys.clear()
	_closed_reasoning_keys.clear()   # 新增
	for child in _message_list.get_children():
		child.queue_free()


func _scroll_to_bottom() -> void:
	_trim_message_list()
	call_deferred("_scroll_to_bottom_deferred")


func _trim_message_list() -> void:
	if _message_list == null:
		return
	while _message_list.get_child_count() > MAX_MESSAGE_LIST_CHILDREN:
		var child := _message_list.get_child(0)
		_message_list.remove_child(child)
		child.queue_free()


func _on_scroll_value_changed(value: float) -> void:
	if _suppress_scroll_check:
		return

	var bar := _scroll.get_v_scroll_bar()
	var scroll_max := bar.max_value - bar.page
	var is_at_bottom := scroll_max <= 0 or value >= scroll_max - 80

	if is_at_bottom:
		_auto_scroll = true
	else:
		# 如果自动滚动已开启且正在流式输出，此次偏移是内容增长引起的，不要关闭自动滚动，直接同步把滚动条推到底（避免 call_deferred 链追不上内容增长导致末尾消息无法滚到底部）。
		if _auto_scroll and _stream_content_rich != null and is_instance_valid(_stream_content_rich):
			_suppress_scroll_check = true
			_scroll.scroll_vertical = 999999
			call_deferred("_reset_scroll_suppress_deferred")
			return
		_auto_scroll = false


func _scroll_to_bottom_deferred() -> void:
	if _scroll == null:
		return
	if not _auto_scroll and not _force_scroll_once:
		return
	_force_scroll_once = false
	_do_scroll_to_bottom()


func _ui(key: String) -> String:
	var lang := "zh"
	if editor_interface != null:
		lang = str(ConfigMigrations.get_value(editor_interface, "ai_agent/ui_language"))
	return ChatPanelText.text(lang, key)


func _ui_table() -> Dictionary:
	var lang := "zh"
	if editor_interface != null:
		lang = str(ConfigMigrations.get_value(editor_interface, "ai_agent/ui_language"))
	return ChatPanelText.table(lang)


func _current_session_id() -> String:
	if editor_interface == null:
		return "default"
	return str(ConfigMigrations.get_value(editor_interface, "ai_agent/session_id"))
