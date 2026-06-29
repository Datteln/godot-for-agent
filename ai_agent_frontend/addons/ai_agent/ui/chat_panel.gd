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
const INPUT_MIN_HEIGHT := 60
const INPUT_MAX_HEIGHT := 240
const FINAL_RESPONSE_EVENT_WAIT_MS := 2500
## Plan/Verify 的展示性事件：通常没有活跃 LLM 文本流陪同到达，需要强制滚动一次，
## 否则容易在 ScrollContainer 重新计算高度期间被误判为"用户已上滑"而停止跟随。
const _MILESTONE_EVENT_TYPES := {
	"plan_created": true,
	"plan_step_started": true,
	"plan_step_completed": true,
	"verify_started": true,
	"verify_completed": true,
	"cache_hit": true,
	"compact_started": true,
	"compact_boundary": true,
}
const _REASONING_BOUNDARY_EVENT_TYPES := {
	"delegate_start": true,
	"server_tool_start": true,
	"server_tool_result": true,
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
var _input: TextEdit
var _context_bar: HFlowContainer
var _file_suggestions_panel: PanelContainer
var _file_suggestions: ItemList
var _file_popup_paths: Array[String] = []
var _project_files: Array = []
var _referenced_files := {}
var _dismissed_context := {}
var _selection_signature := ""
var _last_selection_refresh_ms := 0
var _message_context_popup: PopupMenu
var _message_context_source: RichTextLabel
var _send_btn: Button
var _stop_btn: Button
var _new_session_btn: Button
var _status: Label
var _doctor_btn: Button
var _extensions_btn: Button
var _commands_btn: Button
var _commands_popup: PopupMenu
var _available_commands: Array = []
var _commands_requested := false
var _memory_btn: Button
var _reset_btn: Button
var _history_btn: Button
var _history_popup: PopupMenu
var _history_session_ids: Array = []
var _effort_options: OptionButton
var _permission_options: OptionButton
var _style_options: OptionButton
var _model_input: LineEdit
var _active_model_name := ""
var _context_token_limit := 0
## 最近一次 cache_hit 事件的常驻状态栏摘要；与聊天记录里的滚动提示是两套
## 独立展示——这条不随对话滚走，方便随时确认当前缓存命中情况。
var _last_context_usage_status := ""
## `compact_started` 到达时记录下当时的状态，供 `compact_boundary` 到达时还原；
## 压缩前后状态对应"这一轮原本在干什么"（等待模型/执行工具等），不是固定回到 IDLE。
var _state_before_compact := AgentState.IDLE

var _state := AgentState.IDLE
var _last_doctor_report: Dictionary = {}
var _extensions_pending := false
var _pending_calls: Array = []
var _pending_silent_results: Array = []
var _inline_confirm := InlineToolConfirmation.new()
var _interrupted_locally := false
var _indent_current_text := false
var _event_queue: Array = []
var _draining_events := false
var _force_scroll_once := false
var _pending_final_response := {}
var _pending_final_event := {}
var _pending_final_received_ms: int = -1

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
var _reasoning_token_count: int = 0
var _reasoning_text_dirty := false
var _reasoning_last_render_ms := 0
var _rendered_assistant_keys := {}
var _live_response_keys := {}   # 仅追踪本轮实时响应，避免历史加载的指纹误判为重复
var _closed_stream_keys := {}
var _closed_reasoning_keys := {}   # 新增：专门追踪已关闭的 reasoning stream
var _theme_colors: Dictionary = {}
var _auto_scroll := true
var _suppress_scroll_check := false   # 程序滚动时抑制 value_changed 误判
var _scroll_request_pending := false
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
	_save_session_to_history(_current_session_id())
	_fetch_initial_service_data()


func _process(_delta: float) -> void:
	var selection_now := Time.get_ticks_msec()
	if selection_now - _last_selection_refresh_ms >= 500:
		_last_selection_refresh_ms = selection_now
		_refresh_context_bar()
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
	if not _pending_final_response.is_empty() and _pending_final_received_ms >= 0:
		var final_wait_ms := Time.get_ticks_msec() - _pending_final_received_ms
		if final_wait_ms >= FINAL_RESPONSE_EVENT_WAIT_MS and _event_queue.is_empty() and not _draining_events:
			var pending_final: Dictionary = _pending_final_response.duplicate(true)
			_clear_pending_final_pair()
			FrontendLogger.warn(editor_interface, "ChatPanel", "Rendering pending final without matching event.", {
				"wait_ms": final_wait_ms
			})
			_handle_final(pending_final)
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

	_new_session_btn = Button.new()
	_new_session_btn.text = _ui("new_session")
	toolbar.add_child(_new_session_btn)

	_model_input = LineEdit.new()
	_model_input.custom_minimum_size = Vector2(160, 0)
	_model_input.placeholder_text = str(
		ConfigMigrations.get_value(editor_interface, "ai_agent/llm_model")
	).strip_edges()
	_active_model_name = _model_input.placeholder_text
	_model_input.tooltip_text = "Model override (empty uses the configured default)"
	_model_input.context_menu_enabled = true
	toolbar.add_child(_model_input)

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

	_history_btn = Button.new()
	_history_btn.text = _ui("history")
	toolbar.add_child(_history_btn)

	_scroll = ScrollContainer.new()
	_scroll.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(_scroll)

	_message_list = VBoxContainer.new()
	_message_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_message_list.add_theme_constant_override("separation", 10)
	_scroll.add_child(_message_list)

	_file_suggestions_panel = PanelContainer.new()
	_file_suggestions_panel.visible = false
	_file_suggestions_panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(_file_suggestions_panel)
	_file_suggestions = ItemList.new()
	_file_suggestions.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_file_suggestions.custom_minimum_size = Vector2(0, 180)
	_file_suggestions_panel.add_child(_file_suggestions)

	_context_bar = HFlowContainer.new()
	_context_bar.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_context_bar.add_theme_constant_override("h_separation", 6)
	_context_bar.add_theme_constant_override("v_separation", 4)
	add_child(_context_bar)

	var bottom := HBoxContainer.new()
	bottom.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(bottom)

	_input = TextEdit.new()
	_input.placeholder_text = _ui("input_placeholder")
	_input.wrap_mode = TextEdit.LINE_WRAPPING_BOUNDARY
	_input.scroll_fit_content_height = false
	_input.custom_minimum_size = Vector2(0, INPUT_MIN_HEIGHT)
	_input.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	bottom.add_child(_input)

	var status_row := HBoxContainer.new()
	status_row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(status_row)

	_status = Label.new()
	_status.text = _status_text_for_state(AgentState.IDLE)
	status_row.add_child(_status)

	var status_spacer := Control.new()
	status_spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	status_row.add_child(status_spacer)

	_permission_options = OptionButton.new()
	_permission_options.tooltip_text = _ui("permission_tooltip")
	for choice in _permission_choices():
		_permission_options.add_item(str(choice.get("label", "")))
		_permission_options.set_item_metadata(_permission_options.get_item_count() - 1, choice.get("mode", "default"))
	status_row.add_child(_permission_options)
	_sync_permission_selection()

	_effort_options = OptionButton.new()
	for effort in ["quick", "standard", "deep", "verify", "advisor"]:
		_effort_options.add_item(effort)
	status_row.add_child(_effort_options)
	_sync_effort_selection()

	_send_btn = Button.new()
	_send_btn.text = _ui("send")
	status_row.add_child(_send_btn)

	_stop_btn = Button.new()
	_stop_btn.text = _ui("stop")
	_stop_btn.disabled = true
	status_row.add_child(_stop_btn)


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

	_history_popup = PopupMenu.new()
	add_child(_history_popup)
	_commands_popup = PopupMenu.new()
	add_child(_commands_popup)
	_project_files = _collector.project_files()
	_message_context_popup = PopupMenu.new()
	_message_context_popup.add_item("复制", 0)
	_message_context_popup.add_item("粘贴到输入框", 1)
	_message_context_popup.add_separator()
	_message_context_popup.add_item("全选", 2)
	add_child(_message_context_popup)

	_log_renderer = LogEntryRenderer.new()
	_log_renderer.theme_colors = _theme_colors
	_log_renderer.editor_interface = editor_interface
	_log_renderer.rich_text_setup = _configure_message_rich_text


func _ensure_log_renderer() -> void:
	if _log_renderer != null:
		return
	_log_renderer = LogEntryRenderer.new()
	_log_renderer.theme_colors = _theme_colors
	_log_renderer.editor_interface = editor_interface
	_log_renderer.rich_text_setup = _configure_message_rich_text


func _connect_signals() -> void:
	_send_btn.pressed.connect(_on_send)
	_stop_btn.pressed.connect(_on_interrupt)
	_new_session_btn.pressed.connect(_on_new_session)
	_input.gui_input.connect(_on_input_gui_input)
	_input.text_changed.connect(func(): _on_input_text_changed(_input.text))
	_permission_options.item_selected.connect(_on_permission_selected)
	_effort_options.item_selected.connect(_on_effort_selected)
	_style_options.item_selected.connect(_on_style_selected)
	_doctor_btn.pressed.connect(func(): _http_client.fetch_doctor())
	_extensions_btn.pressed.connect(_on_extensions)
	_commands_btn.pressed.connect(_on_show_commands)
	_commands_popup.id_pressed.connect(_on_command_selected)
	_memory_btn.pressed.connect(func(): _http_client.fetch_memory())
	_reset_btn.pressed.connect(_on_reset)
	_history_btn.pressed.connect(_on_show_history)
	_history_popup.index_pressed.connect(_on_history_item_selected)
	_file_suggestions.item_clicked.connect(func(index: int, _position: Vector2, mouse_button: int):
		if mouse_button == MOUSE_BUTTON_LEFT:
			_on_file_reference_selected(index)
	)
	_file_suggestions.item_activated.connect(_on_file_reference_selected)
	_message_context_popup.id_pressed.connect(_on_message_context_action)
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
	if _try_run_slash_command(text):
		return
	FrontendLogger.info(editor_interface, "ChatPanel", "Sending user message.", {"chars": text.length()})
	var requested_model = _request_model()
	_active_model_name = str(requested_model) if requested_model != null else _model_input.placeholder_text
	_auto_scroll = true
	_force_scroll_once = true
	_interrupted_locally = false
	_clear_pending_final_pair()
	_finish_streaming()
	_mark_reasoning_stream_closed()
	_finish_reasoning_stream()
	_closed_stream_keys.clear()
	_closed_reasoning_keys.clear()   # 新增
	_live_response_keys.clear()
	_empty_final_ignored_ms = -1   # 重置空 final 超时计时器
	_input.text = ""
	_update_input_height()
	var referenced_paths: Array = _referenced_files.keys()
	_referenced_files.clear()
	_selection_signature = ""
	_refresh_context_bar()
	# 在用户消息之前追加两条空白消息：强制撑开 ScrollContainer 使滚动到底部，
	# 同时作为上一轮回复与当前用户消息之间的视觉间距
	_append_message("system", " ")
	_append_message("system", " ")
	_append_message("user", text)
	_append_message("system", _ui("waiting_model"))
	_set_state(AgentState.WAITING_LLM)
	if undo_manager != null:
		undo_manager.begin_batch("AI: " + text.left(40))
	_http_client.send_user_message(text, _collector.collect("any", referenced_paths), requested_model)


func _try_run_slash_command(text: String) -> bool:
	if not text.begins_with("/"):
		return false
	var command_line := text.substr(1).strip_edges()
	if command_line.is_empty():
		return false
	var separator := command_line.find(" ")
	var command_name := command_line if separator < 0 else command_line.left(separator)
	var raw_args := "" if separator < 0 else command_line.substr(separator + 1).strip_edges()
	var args := {}
	if not raw_args.is_empty():
		var parsed = JSON.parse_string(raw_args)
		if not (parsed is Dictionary):
			_append_message("error", "命令参数必须是 JSON 对象，例如：/rebuild_index {\"incremental\": true}")
			FrontendLogger.warn(editor_interface, "ChatPanel", "Slash command rejected: invalid JSON args.", {
				"command": command_name,
				"args_chars": raw_args.length()
			})
			return true
		args = parsed
	_input.text = ""
	_update_input_height()
	_auto_scroll = true
	_force_scroll_once = true
	_append_message("user", text)
	_append_message("system", "正在执行命令 /%s …" % command_name)
	_set_state(AgentState.WAITING_LLM)
	FrontendLogger.info(editor_interface, "ChatPanel", "Running slash command.", {
		"command": command_name,
		"arg_keys": args.keys()
	})
	_http_client.run_command(command_name, args)
	return true


## Enter 发送消息；Ctrl+Enter 换行。
## Shift+Enter 在 Godot 的 TextEdit 里默认不会插入换行（默认的 ui_text_newline
## 动作要求精确匹配无修饰键，Shift+Enter 不命中任何内置动作，相当于无反应），
## 所以换行改用 Ctrl+Enter，并手动在光标处插入 "\n"。
## 注意：`gui_input` 信号在控件自身处理事件之前发出，是专门留给外部拦截用的；
## 因此这里只能对"要拦截"的 Enter 组合调用 accept_event()，其余按键必须原样
## 放行，否则会截断 TextEdit 自己插入字符/处理粘贴等内部逻辑（§全部输入被吞没）。
func _on_input_gui_input(event: InputEvent) -> void:
	if not (event is InputEventKey):
		return
	var key_event := event as InputEventKey
	if not key_event.pressed or key_event.echo:
		return
	if key_event.keycode != KEY_ENTER and key_event.keycode != KEY_KP_ENTER:
		return
	if key_event.ctrl_pressed or key_event.meta_pressed:
		_input.accept_event()
		_input.insert_text_at_caret("\n")
		return
	if key_event.shift_pressed or key_event.alt_pressed:
		return
	if DisplayServer.ime_get_text() != "":
		# 输入法正在合成候选（例如中文全角标点确认），这个 Enter 是输入法上屏键，
		# 不能拦截成"发送"，否则候选文本会丢字或被错误提交。
		return
	_input.accept_event()
	_on_send()


## 根据当前行数（含自动换行产生的视觉行）动态调整输入框高度，
## 在 INPUT_MIN_HEIGHT 与 INPUT_MAX_HEIGHT 之间撑大，超出上限后由 TextEdit 自带滚动条接管。
func _update_input_height() -> void:
	if _input == null or not is_instance_valid(_input):
		return
	var line_height := _input.get_line_height()
	if line_height <= 0:
		return
	var visual_lines := 0
	for line_index in range(_input.get_line_count()):
		visual_lines += _input.get_line_wrap_count(line_index) + 1
	var content_height := visual_lines * line_height + 16
	_input.custom_minimum_size.y = clampi(content_height, INPUT_MIN_HEIGHT, INPUT_MAX_HEIGHT)


func _on_input_text_changed(text: String) -> void:
	_update_input_height()
	if _file_suggestions == null:
		return
	var caret := _input.get_caret_column()
	var before_caret := text.left(caret)
	var at_index := before_caret.rfind("@")
	if at_index < 0 or (at_index > 0 and not before_caret.substr(at_index - 1, 1) in [" ", "\t", "\n"]):
		_file_suggestions_panel.visible = false
		return
	var query := before_caret.substr(at_index + 1)
	if query.contains(" ") or query.contains("\t") or query.contains("\n"):
		_file_suggestions_panel.visible = false
		return
	_file_suggestions.clear()
	_file_popup_paths.clear()
	var lowered := query.to_lower()
	for item in _project_files:
		var path := str(item)
		if lowered != "" and not path.to_lower().contains(lowered) and not path.get_file().to_lower().contains(lowered):
			continue
		_file_suggestions.add_item(path)
		_file_popup_paths.append(path)
		if _file_popup_paths.size() >= 12:
			break
	if _file_popup_paths.is_empty():
		_file_suggestions_panel.visible = false
		return
	_file_suggestions_panel.visible = true
	_file_suggestions.custom_minimum_size.y = minf(240.0, maxf(40.0, _file_popup_paths.size() * 26.0))


func _on_file_reference_selected(index: int) -> void:
	if index < 0 or index >= _file_popup_paths.size():
		return
	var path := _file_popup_paths[index]
	var caret := _input.get_caret_column()
	var text := _input.text
	var at_index := text.left(caret).rfind("@")
	if at_index >= 0:
		_input.text = text.left(at_index) + "@" + path + " " + text.substr(caret)
		_input.set_caret_column(at_index + path.length() + 2)
		_update_input_height()
	_referenced_files[path] = true
	_dismissed_context.erase("file:" + path)
	_selection_signature = ""
	_refresh_context_bar()
	_file_suggestions_panel.visible = false
	_input.grab_focus()


func _refresh_context_bar() -> void:
	if _context_bar == null or _collector == null:
		return
	var selection: Dictionary = _collector.collect_selection()
	var active_context_keys := {}
	for item in selection.get("nodes", []):
		if item is Dictionary:
			active_context_keys["node:" + str(item.get("path", item.get("name", "")))] = true
	var active_script := str(selection.get("current_script", ""))
	if active_script != "":
		active_context_keys["file:" + active_script] = true
	for selected_path in selection.get("selected_files", []):
		active_context_keys["file:" + str(selected_path)] = true
	for dismissed_key in _dismissed_context.keys():
		if not active_context_keys.has(dismissed_key):
			_dismissed_context.erase(dismissed_key)
	var referenced_path_strings := PackedStringArray()
	for referenced_path in _referenced_files.keys():
		referenced_path_strings.append(str(referenced_path))
	var dismissed_strings := PackedStringArray()
	for dismissed_key in _dismissed_context.keys():
		dismissed_strings.append(str(dismissed_key))
	var signature := JSON.stringify(selection) + "|" + "|".join(referenced_path_strings) + "|" + "|".join(dismissed_strings)
	if signature == _selection_signature:
		return
	_selection_signature = signature
	for child in _context_bar.get_children():
		child.queue_free()
	var nodes: Array = selection.get("nodes", [])
	for item in nodes:
		if not (item is Dictionary):
			continue
		var node_path := str(item.get("path", item.get("name", "")))
		var node_key := "node:" + node_path
		if _dismissed_context.has(node_key):
			continue
		var chip := Button.new()
		chip.text = "Node: %s  ×" % node_path
		chip.tooltip_text = "%s · %s" % [str(item.get("type", "Node")), str(item.get("script", ""))]
		chip.pressed.connect(_dismiss_auto_context.bind(node_key, ""))
		_context_bar.add_child(chip)
	var current_script := str(selection.get("current_script", ""))
	if current_script != "" and not _dismissed_context.has("file:" + current_script):
		var script_chip := Button.new()
		script_chip.text = "@%s  ×" % current_script
		script_chip.tooltip_text = "Current script · click to remove"
		script_chip.pressed.connect(_dismiss_auto_context.bind("file:" + current_script, current_script))
		_context_bar.add_child(script_chip)
	var selected_files: Array = selection.get("selected_files", [])
	for selected_file_value in selected_files:
		var selected_file := str(selected_file_value)
		if selected_file == current_script or _dismissed_context.has("file:" + selected_file):
			continue
		var selected_file_chip := Button.new()
		selected_file_chip.text = "@%s  ×" % selected_file
		selected_file_chip.tooltip_text = "Selected file · click to remove"
		selected_file_chip.pressed.connect(_dismiss_auto_context.bind("file:" + selected_file, selected_file))
		_context_bar.add_child(selected_file_chip)
	for path_value in _referenced_files.keys():
		var path := str(path_value)
		if path == current_script:
			continue
		var ref_chip := Button.new()
		ref_chip.text = "@%s  ×" % path
		ref_chip.tooltip_text = "Remove file reference"
		ref_chip.pressed.connect(_remove_file_reference.bind(path))
		_context_bar.add_child(ref_chip)
	_context_bar.visible = _context_bar.get_child_count() > 0


func _remove_file_reference(path: String) -> void:
	_referenced_files.erase(path)
	_selection_signature = ""
	_refresh_context_bar()


func _dismiss_auto_context(key: String, referenced_path: String) -> void:
	_dismissed_context[key] = true
	if referenced_path != "":
		_referenced_files.erase(referenced_path)
	_selection_signature = ""
	_refresh_context_bar()


func _configure_message_rich_text(rich: RichTextLabel) -> void:
	rich.selection_enabled = true
	rich.context_menu_enabled = false
	rich.gui_input.connect(_on_message_rich_input.bind(rich))


func _on_message_rich_input(event: InputEvent, rich: RichTextLabel) -> void:
	if not (event is InputEventMouseButton):
		return
	var mouse_event := event as InputEventMouseButton
	if mouse_event.button_index != MOUSE_BUTTON_RIGHT or not mouse_event.pressed:
		return
	_message_context_source = rich
	_message_context_popup.set_item_disabled(0, rich.get_selected_text() == "")
	_message_context_popup.position = DisplayServer.mouse_get_position()
	_message_context_popup.popup()
	rich.accept_event()


func _on_message_context_action(id: int) -> void:
	match id:
		0:
			if _message_context_source != null and is_instance_valid(_message_context_source):
				var selected := _message_context_source.get_selected_text()
				if selected != "":
					DisplayServer.clipboard_set(selected)
		1:
			var pasted := DisplayServer.clipboard_get()
			if pasted != "":
				var caret := _input.get_caret_column()
				_input.text = _input.text.left(caret) + pasted + _input.text.substr(caret)
				_input.set_caret_column(caret + pasted.length())
				_update_input_height()
				_input.grab_focus()
		2:
			if _message_context_source != null and is_instance_valid(_message_context_source):
				_message_context_source.select_all()


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
		if _extensions_pending:
			_extensions_pending = false
			_append_message("system", _format_extensions_report({
				"skills": response.get("skills", []),
				"warnings": response.get("warnings", [])
			}))
		else:
			_append_message("system", _format_doctor_report(response))
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
		_append_message("system", _format_memory_report(response))
		return

	if response.has("ok") and response.has("session_id") and response.size() == 2:
		FrontendLogger.debug(editor_interface, "ChatPanel", "Reset acknowledged.", {"session_id": str(response.get("session_id", ""))})
		return

	if response.has("type") and response.get("type") == "data":
		var value = response.get("value", null)
		if value is Array and _looks_like_command_list(value):
			_populate_commands_popup(value)
			if _commands_requested:
				_commands_requested = false
				_commands_btn.disabled = _state != AgentState.IDLE
				_show_commands_popup()
		else:
			_append_message("system", _format_plain_value("数据", value))
		return

	if response.has("ok") and response.has("text"):
		var command_text := _format_command_response(response)
		_append_message("system" if bool(response.get("ok", false)) else "error", command_text)
		if _state == AgentState.WAITING_LLM:
			_set_state(AgentState.IDLE)
		return

	if response.has("exists"):
		if response.get("exists", false):
			var raw_pointer: Variant = response.get("pointer", {})
			var pointer: Dictionary = raw_pointer if raw_pointer is Dictionary else {}
			if bool(ConfigMigrations.get_value(editor_interface, "ai_agent/show_recovery_prompt")):
				_recovery_prompt.show_pointer(pointer)
		return

	match str(response.get("type", "")):
		"tool_calls":
			FrontendLogger.debug(editor_interface, "ChatPanel", "[response] -> route: tool_calls")
			_handle_tool_calls(response)
		"final":
			FrontendLogger.debug(editor_interface, "ChatPanel", "[response] -> route: final")
			_on_final_response(response)
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


func _format_doctor_report(report: Dictionary) -> String:
	var capabilities: Dictionary = report.get("capabilities", {}) if report.get("capabilities", {}) is Dictionary else {}
	var lsp: Dictionary = capabilities.get("lsp", {}) if capabilities.get("lsp", {}) is Dictionary else {}
	var mcp: Dictionary = capabilities.get("mcp", {}) if capabilities.get("mcp", {}) is Dictionary else {}
	var rag: Dictionary = capabilities.get("rag", {}) if capabilities.get("rag", {}) is Dictionary else {}
	var lines: Array[String] = ["诊断报告", "", "基础状态"]
	lines.append("• Python：%s" % str(report.get("python_version", "未知")))
	lines.append("• 模型：%s" % str(report.get("llm_model", "未配置")))
	lines.append("• LLM 地址：%s" % _doctor_status(bool(report.get("llm_base_url_configured", false))))
	lines.append("• 鉴权：%s" % _doctor_status(bool(report.get("auth_enabled", false))))
	lines.append("• 权限模式：%s" % str(report.get("permission_mode", "未知")))
	lines.append("• 受信任项目：%s" % _doctor_status(bool(report.get("trusted_project", false))))
	lines.append("• 项目目录：%s" % str(report.get("project_root", "未知")))
	lines.append("• 会话目录：%s" % str(report.get("session_store_dir", "未知")))

	lines.append_array(["", "能力"])
	lines.append("• LSP：%s；模式：%s；服务：%s" % [
		_doctor_status(bool(lsp.get("enabled", false))), str(lsp.get("mode", "未知")), str(lsp.get("lsp_server", "未知"))
	])
	lines.append("  诊断来源：%s" % _doctor_list(lsp.get("diagnostics_sources", [])))
	lines.append("  回退工具：%s" % _doctor_list(lsp.get("fallbacks", [])))
	lines.append("• MCP：%s；模式：%s；权限：%s" % [
		_doctor_status(bool(mcp.get("enabled", false))), str(mcp.get("mode", "未知")), str(mcp.get("permission_mode_when_enabled", "未知"))
	])
	lines.append("  入口：%s" % str(mcp.get("entrypoint", "未配置")))
	lines.append("• RAG：%s；模式：%s；策略：%s" % [
		_doctor_status(bool(rag.get("enabled", false))), str(rag.get("mode", "未知")), str(rag.get("strategy", "未知"))
	])
	lines.append("  主索引：%s（%s）" % [
		"已创建" if bool(rag.get("index_exists", false)) else "未创建", str(rag.get("index_path", "未知"))
	])
	var sub_indexes: Dictionary = rag.get("sub_indexes", {}) if rag.get("sub_indexes", {}) is Dictionary else {}
	for index_name in sub_indexes.keys():
		var index_info: Dictionary = sub_indexes[index_name] if sub_indexes[index_name] is Dictionary else {}
		lines.append("  %s：%s（%s）" % [
			str(index_name), "已创建" if bool(index_info.get("exists", false)) else "未创建", str(index_info.get("path", "未知"))
		])

	lines.append_array(["", "启用域", _doctor_list(report.get("enabled_domains", []))])
	var tools: Array = report.get("registered_tools", []) if report.get("registered_tools", []) is Array else []
	lines.append_array(["", "已注册工具（%d）" % tools.size(), _doctor_list(tools)])

	lines.append_array(["", "输出风格"])
	var styles: Array = report.get("output_styles", []) if report.get("output_styles", []) is Array else []
	if styles.is_empty():
		lines.append("• 无")
	for style in styles:
		if style is Dictionary:
			lines.append("• %s：%s%s" % [
				str(style.get("name", "未命名")), str(style.get("description", "")), "" if bool(style.get("enabled", true)) else "（已禁用）"
			])

	lines.append_array(["", "技能"])
	var skills: Array = report.get("skills", []) if report.get("skills", []) is Array else []
	if skills.is_empty():
		lines.append("• 无")
	for skill in skills:
		if skill is Dictionary:
			lines.append("• %s：%s" % [str(skill.get("name", "未命名")), str(skill.get("description", ""))])
			lines.append("  工具：%s" % _doctor_list(skill.get("effective_tools", [])))

	lines.append_array(["", "警告"])
	var warnings: Array = report.get("warnings", []) if report.get("warnings", []) is Array else []
	if warnings.is_empty():
		lines.append("• 无")
	else:
		for warning in warnings:
			lines.append("• %s" % str(warning))
	return "\n".join(lines)


func _doctor_status(enabled: bool) -> String:
	return "已启用" if enabled else "未启用"


func _doctor_list(values) -> String:
	if not (values is Array) or values.is_empty():
		return "无"
	var items := PackedStringArray()
	for value in values:
		items.append(str(value))
	return "、".join(items)


func _looks_like_command_list(values: Array) -> bool:
	if values.is_empty():
		return true
	for value in values:
		if not (value is Dictionary) or not value.has("name") or not value.has("description"):
			return false
	return true


func _on_show_commands() -> void:
	if not _available_commands.is_empty():
		_show_commands_popup()
		return
	_commands_requested = true
	_commands_btn.disabled = true
	_http_client.fetch_commands()


func _populate_commands_popup(commands: Array) -> void:
	_available_commands.clear()
	_commands_popup.clear()
	for command in commands:
		if not (command is Dictionary):
			continue
		var index := _available_commands.size()
		_available_commands.append(command)
		_commands_popup.add_item(str(command.get("name", "未命名命令")), index)
		_commands_popup.set_item_tooltip(index, str(command.get("description", "无说明")))


func _show_commands_popup() -> void:
	var popup_position := _commands_btn.get_screen_position() + Vector2(0, _commands_btn.size.y)
	_commands_popup.position = Vector2i(roundi(popup_position.x), roundi(popup_position.y))
	_commands_popup.reset_size()
	_commands_popup.popup()


func _on_command_selected(command_index: int) -> void:
	if command_index < 0 or command_index >= _available_commands.size():
		return
	var command = _available_commands[command_index]
	if not (command is Dictionary):
		return
	var command_name := str(command.get("name", "")).strip_edges()
	var args := _command_default_args(command)
	_run_selected_command(command_name, args)


func _command_default_args(command: Dictionary) -> Dictionary:
	var args := {}
	var schema = command.get("args_schema", {})
	if not (schema is Dictionary):
		return args
	var properties = schema.get("properties", {})
	if not (properties is Dictionary):
		return args
	for property_name in properties.keys():
		var info = properties[property_name]
		if not (info is Dictionary):
			continue
		if info.has("default"):
			var default_value = info.get("default")
			args[property_name] = int(default_value) if str(info.get("type", "")) == "integer" else default_value
		elif property_name == "effort":
			args[property_name] = _effort_options.get_item_text(_effort_options.selected)
		elif property_name == "output_style":
			args[property_name] = _style_options.get_item_text(_style_options.selected)
		else:
			var enum_values = info.get("enum", [])
			if enum_values is Array and not enum_values.is_empty():
				args[property_name] = enum_values[0]
	return args


func _run_selected_command(command_name: String, args: Dictionary) -> void:
	if _state != AgentState.IDLE:
		_append_message("error", "当前任务尚未结束，暂时不能运行其他命令。")
		return
	_auto_scroll = true
	_force_scroll_once = true
	_append_message("user", "/%s %s" % [command_name, JSON.stringify(args)])
	_append_message("system", "正在执行命令 /%s …" % command_name)
	_set_state(AgentState.WAITING_LLM)
	FrontendLogger.info(editor_interface, "ChatPanel", "Running command selected from dropdown.", {
		"command": command_name,
		"arg_keys": args.keys()
	})
	_http_client.run_command(command_name, args)


func _format_commands_report(commands: Array) -> String:
	var lines: Array[String] = ["命令", "", "共 %d 个可用命令" % commands.size()]
	if commands.is_empty():
		lines.append("• 无")
		return "\n".join(lines)
	for command in commands:
		if not (command is Dictionary):
			continue
		lines.append("")
		lines.append("• %s" % str(command.get("name", "未命名")))
		lines.append("  %s" % str(command.get("description", "无说明")))
		var schema: Dictionary = command.get("args_schema", {}) if command.get("args_schema", {}) is Dictionary else {}
		var properties: Dictionary = schema.get("properties", {}) if schema.get("properties", {}) is Dictionary else {}
		if properties.is_empty():
			lines.append("  参数：无")
		else:
			var required: Array = schema.get("required", []) if schema.get("required", []) is Array else []
			var args: Array[String] = []
			for arg_name in properties.keys():
				var info: Dictionary = properties[arg_name] if properties[arg_name] is Dictionary else {}
				var label := str(arg_name) + "：" + str(info.get("type", "任意"))
				if required.has(arg_name):
					label += "，必填"
				elif info.has("default"):
					label += "，默认 %s" % str(info.get("default"))
				args.append(label)
			lines.append("  参数：%s" % "；".join(PackedStringArray(args)))
	return "\n".join(lines)


func _format_command_response(response: Dictionary) -> String:
	var text := str(response.get("text", "")).strip_edges()
	var result = response.get("result", null)
	if not (result is Dictionary):
		return text
	if result.has("python_version"):
		return _format_doctor_report(result)
	if result.has("files") and result.has("chunks") and result.has("changed_files"):
		return _format_rebuild_index_result(result)
	if result.has("compacted_frames") and result.has("removed_messages"):
		return _format_compact_result(result)
	var formatted := _format_plain_value("命令结果", result)
	return formatted if text.is_empty() else text + "\n\n" + formatted


func _format_rebuild_index_result(result: Dictionary) -> String:
	var lines: Array[String] = ["RAG 索引构建完成", ""]
	lines.append("• 本次处理文件：%d" % int(result.get("files", 0)))
	lines.append("• 索引片段：%d" % int(result.get("chunks", 0)))
	lines.append("• 发生变化的文件：%d" % int(result.get("changed_files", 0)))
	if result.has("vectors"):
		lines.append("• 向量数量：%d" % int(result.get("vectors", 0)))
	if result.has("symbols"):
		lines.append("• 符号数量：%d" % int(result.get("symbols", 0)))
	if result.has("assets"):
		lines.append("• 资源数量：%d" % int(result.get("assets", 0)))
	lines.append("• 文件数量是否超限：%s" % ("是" if bool(result.get("truncated_files", false)) else "否"))
	return "\n".join(lines)


func _format_compact_result(result: Dictionary) -> String:
	var lines: Array[String] = ["会话上下文压缩完成", ""]
	lines.append("• 压缩帧数：%d" % int(result.get("compacted_frames", 0)))
	lines.append("• 移除消息：%d" % int(result.get("removed_messages", 0)))
	lines.append("• 截断超长消息：%d" % int(result.get("truncated_messages", 0)))
	lines.append("• 待处理任务：%s" % ("已保留" if result.get("pending_turn_id", null) != null else "无"))
	return "\n".join(lines)


func _format_memory_report(response: Dictionary) -> String:
	var items: Array = response.get("items", []) if response.get("items", []) is Array else []
	var lines: Array[String] = ["记忆", "", "状态：%s" % ("成功" if bool(response.get("ok", true)) else "失败")]
	var response_text := str(response.get("text", "")).strip_edges()
	if response_text != "":
		lines.append("消息：%s" % response_text)
	lines.append("条目：%d" % items.size())
	if items.is_empty():
		lines.append("• 无")
		return "\n".join(lines)
	for index in range(items.size()):
		var item = items[index]
		if not (item is Dictionary):
			continue
		lines.append("")
		lines.append("%d. %s" % [index + 1, str(item.get("text", ""))])
		lines.append("   ID：%s" % str(item.get("id", "未知")))
		lines.append("   范围：%s；标签：%s" % [str(item.get("scope", "未知")), _doctor_list(item.get("tags", []))])
		var updated_at := int(float(item.get("updated_at", 0.0)))
		if updated_at > 0:
			lines.append("   更新时间：%s" % Time.get_datetime_string_from_unix_time(updated_at, true))
	return "\n".join(lines)


func _format_extensions_report(payload: Dictionary) -> String:
	var skills: Array = payload.get("skills", []) if payload.get("skills", []) is Array else []
	var lines: Array[String] = ["扩展", "", "技能：%d" % skills.size()]
	if skills.is_empty():
		lines.append("• 无")
	for skill in skills:
		if not (skill is Dictionary):
			continue
		lines.append("")
		lines.append("• %s%s" % [
			str(skill.get("name", "未命名")), "" if bool(skill.get("enabled", true)) else "（已禁用）"
		])
		lines.append("  %s" % str(skill.get("description", "无说明")))
		var qualified_name := str(skill.get("qualified_name", "")).strip_edges()
		if qualified_name != "":
			lines.append("  标识：%s；来源：%s" % [qualified_name, str(skill.get("source", "未知"))])
		lines.append("  工具：%s" % _doctor_list(skill.get("effective_tools", [])))
		var when_to_use := str(skill.get("when_to_use", "")).strip_edges()
		if when_to_use != "":
			lines.append("  使用时机：%s" % when_to_use)
	var warnings: Array = payload.get("warnings", []) if payload.get("warnings", []) is Array else []
	lines.append_array(["", "警告"])
	if warnings.is_empty():
		lines.append("• 无")
	else:
		for warning in warnings:
			lines.append("• %s" % str(warning))
	return "\n".join(lines)


func _format_plain_value(title: String, value) -> String:
	if value == null:
		return title + "\n\n无"
	if value is Array:
		return title + "\n\n" + _doctor_list(value)
	if value is Dictionary:
		var lines: Array[String] = [title, ""]
		for key in value.keys():
			lines.append("• %s：%s" % [str(key), str(value[key])])
		return "\n".join(lines)
	return title + "\n\n" + str(value)


func _handle_tool_calls(response: Dictionary) -> void:
	# 后端理应返回数组；但 HTTP 链路上的版本不匹配/截断/代理篡改都可能让 `calls`
	# 变成 null 或对象。直接赋给强类型 `Array` 会在运行时崩溃并中断整条工具调用
	# 回调，所以先判型兜底（§前端外部数据强类型赋值）。
	var raw_calls: Variant = response.get("calls", [])
	var calls: Array = raw_calls if raw_calls is Array else []
	if _state == AgentState.WAITING_CONFIRM:
		FrontendLogger.warn(editor_interface, "ChatPanel", "Ignoring tool_calls while a previous batch is still pending confirmation.", {"count": calls.size()})
		return

	_mark_current_stream_closed()
	_discard_stream_message()
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
			result = _ensure_tool_result_for_call(call, result)
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
		_http_client.send_tool_results(results, _request_model())


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
	_http_client.send_tool_results(results, _request_model())


## 移除文本中的 `<think>…</think>` XML 块及所有残余的 `</think>` 标签。
func _strip_think_xml(text: String) -> String:
	var result := text
	for tag_name in ["think", "thinking"]:
		var open_tag := "<%s>" % tag_name
		var close_tag := "</%s>" % tag_name
		var start := result.find(open_tag)
		while start != -1:
			var end_tag := result.find(close_tag, start)
			if end_tag == -1:
				result = result.substr(0, start)
				break
			result = result.substr(0, start) + result.substr(end_tag + close_tag.length())
			start = result.find(open_tag)
		result = result.replace(close_tag, "")
	# 如果移除 <think> 块后文本变空或几乎为空，记录警告
	if result.strip_edges().is_empty() and text.strip_edges().length() > 10:
		FrontendLogger.debug(editor_interface, "ChatPanel", "[strip_think_xml] WARNING: text becomes EMPTY after stripping", {
			"original_length": text.strip_edges().length(),
			"preview": text.left(100).replace("\n", "\\n")
		})
	return result


## 若回复以 `Thought: ...` 摘要行开头，拆分出摘要文本与剩余正文。
func _split_reasoning_xml_payload(text: String) -> Dictionary:
	for tag_name in ["think", "thinking"]:
		var open_tag := "<%s>" % tag_name
		var close_tag := "</%s>" % tag_name
		var start := text.find(open_tag)
		if start == -1:
			var close_only := text.find(close_tag)
			if close_only != -1:
				return {
					"reasoning": text.substr(0, close_only).strip_edges(),
					"body": text.substr(close_only + close_tag.length()).strip_edges()
				}
			continue
		var content_start := start + open_tag.length()
		var end_tag := text.find(close_tag, content_start)
		if end_tag == -1:
			return {
				"reasoning": (text.substr(0, start) + text.substr(content_start)).strip_edges(),
				"body": ""
			}
		var before := text.substr(0, start).strip_edges()
		var inside := text.substr(content_start, end_tag - content_start).strip_edges()
		var body := text.substr(end_tag + close_tag.length()).strip_edges()
		var reasoning := inside
		if before != "":
			reasoning = before if reasoning == "" else before + "\n\n" + reasoning
		return {"reasoning": reasoning, "body": body}
	return {"reasoning": text.strip_edges(), "body": ""}


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


## 渲染后端为历史回放重建的 "Thought for Xs\n<思考正文>" 条目：第一行做折叠标题，
## 剩余部分原样作为 detail，整段不经过通用的按行动作前缀拆分（见调用处注释）。
func _append_history_thought_item(text: String) -> void:
	_ensure_log_renderer()
	var stripped := text.strip_edges()
	var newline := stripped.find("\n")
	var header := stripped if newline == -1 else stripped.substr(0, newline)
	var detail := "" if newline == -1 else stripped.substr(newline + 1).strip_edges()
	_log_renderer.append_history_thought_entry(_message_list, header, detail)
	_scroll_to_bottom()


func _handle_final(response: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Received final response.", {
		"chars": str(response.get("text", "")).length()
	})
	var text := _strip_think_xml(str(response.get("text", "")))
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
	_ensure_log_renderer()
	if _state != AgentState.IDLE:
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored session history while a turn is active.", {
			"state": _status.text
		})
		return
	var raw_items: Variant = response.get("items", [])
	var items: Array = raw_items if raw_items is Array else []
	var raw_blocks: Variant = response.get("blocks", [])
	var blocks: Array = raw_blocks if raw_blocks is Array else []
	var session_id := str(response.get("session_id", ""))
	# 响应 session_id 与当前不符说明是切换会话后迟到的过期响应，直接丢弃。
	if session_id != "" and session_id != _current_session_id():
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored stale session history: session mismatch.", {
			"response_session": session_id,
			"current": _current_session_id()
		})
		return
	FrontendLogger.info(editor_interface, "ChatPanel", "Restoring session history.", {
		"session_id": session_id,
		"count": blocks.size() if response.has("blocks") else items.size(),
		"structured": response.has("blocks")
	})
	_clear_messages()
	_update_context_usage_status(
		int(response.get("context_used_tokens", 0)),
		int(response.get("context_token_limit", 0))
	)
	if state_store != null:
		state_store.set_value("session_id", session_id)
	var pending_turn_id = response.get("pending_turn_id")
	_http_client.sync_event_cursor(int(response.get("last_event_seq", 0)))
	if pending_turn_id != null:
		_http_client.current_turn_id = str(pending_turn_id)
		if state_store != null:
			state_store.set_value("current_turn_id", _http_client.current_turn_id)
	var saved_auto_scroll := _auto_scroll
	_auto_scroll = false
	if response.has("blocks"):
		for block in blocks:
			if block is Dictionary:
				_render_history_block(block)
	else:
		_render_legacy_history_items(items)
	if pending_turn_id != null:
		_append_message("system", _ui("recovered_pending") % [session_id, str(pending_turn_id)])
		_show_pending_results_notice()
		_set_state(AgentState.WAITING_CONFIRM)
	# 旧服务没有 blocks 时保留恢复提示；结构化响应本身已经完整表达内容，
	# 不再向历史时间线注入一条并不存在的系统消息。
	if not response.has("blocks") and not items.is_empty():
		_append_message("system", _ui("history_restored") % str(items.size()))
	elif blocks.is_empty() and items.is_empty() and _message_list.get_child_count() == 0:
		_append_message("system", _ui("switch_session_empty"))
	_auto_scroll = saved_auto_scroll
	_force_scroll_once = true
	_scroll_to_bottom()


func _render_legacy_history_items(items: Array) -> void:
	for item in items:
		if not (item is Dictionary):
			continue
		var role := str(item.get("role", "system"))
		var raw_text := str(item.get("text", ""))
		if role == "assistant" and raw_text.strip_edges().begins_with("Thought for "):
			_append_history_thought_item(raw_text)
			continue
		var text := _normalize_history_text(role, _strip_think_xml(raw_text)).strip_edges()
		if text == "":
			continue
		# agent_tool_calls 事件对应的历史条目，实时对话不显示，历史也跳过
		if text.begins_with("Tool calls"):
			continue
		var processed := _apply_thought_prefix(text) if role == "assistant" else text
		if role == "assistant" and processed.begins_with("Thought for "):
			_append_history_thought_item(processed)
		elif role == "assistant":
			_indent_current_text = true
			_append_message(role, processed)
			_indent_current_text = false
		else:
			_append_message(role, processed)


func _render_history_block(block: Dictionary) -> void:
	_ensure_log_renderer()
	var block_type := str(block.get("type", ""))
	match block_type:
		"user":
			_render_message_block("user", str(block.get("text", "")))
		"error":
			_render_message_block("error", str(block.get("text", "")))
		"log_text":
			_log_renderer.append_history_text_entry(
				_message_list,
				str(block.get("text", "")),
				bool(block.get("marker", false)),
				bool(block.get("indent", false))
			)
		"log_read":
			_append_log_stream_message(
				"Read %s (lines %d-%d)" % [
					str(block.get("path", "<unknown>")),
					int(block.get("line_start", 1)),
					int(block.get("line_end", 1))
				]
			)
		"log_grep":
			_render_history_grep_block(block)
		"log_edit":
			_append_log_stream_message(
				"Edit %s\n+%d -%d lines" % [
					str(block.get("path", "<unknown>")),
					int(block.get("added", 0)),
					int(block.get("removed", 0))
				]
			)
			var edit_after_text := str(block.get("after_text", ""))
			if edit_after_text != "":
				var ext := str(block.get("path", "")).get_extension().to_lower()
				var lang := "gdscript" if ext == "gd" else ("python" if ext == "py" else "")
				_log_renderer.append_history_code_entry(_message_list, edit_after_text, lang, true)
		"node_tree":
			_render_history_node_tree(block)
		"thought":
			_log_renderer.append_history_thought_entry(
				_message_list,
				str(block.get("header", "Thought")),
				str(block.get("detail", ""))
			)
		"plan_created":
			_render_history_plan_created(block)
		"step_started":
			_append_log_stream_message(
				"Step %d/%d started:\n%s%s" % [
					int(block.get("index", 0)), int(block.get("total", 0)),
					str(block.get("title", "")), _history_agent_suffix(block)
				], null, true
			)
		"step_completed":
			_append_log_stream_message(
				"Step %d/%d completed:\n%s" % [
					int(block.get("index", 0)), int(block.get("total", 0)),
					str(block.get("summary", ""))
				], null, true
			)
		"verify_started":
			_append_log_stream_message(
				"Verify started:\n%s (%s)" % [
					str(block.get("file_path", "")), str(block.get("phase", ""))
				], null, true
			)
		"verify_passed":
			_append_log_stream_message(
				"Verify passed:\n%s" % str(block.get("summary", "")), null, true
			)
		"verify_failed":
			_append_log_stream_message(
				"Verify found %d issue(s):\n%s" % [
					int(block.get("issues_count", 0)), str(block.get("summary", ""))
				], null, true
			)
		"delegate_results":
			_render_history_delegate_results(block)
		"delegate_result":
			_append_log_stream_message(
				"Delegate result: %s\n%s" % [
					str(block.get("agent", "")),
					str(block.get("summary", ""))
				], null, true
			)
		"event":
			var payload: Dictionary = block.get("payload", {}) if block.get("payload", {}) is Dictionary else {}
			_render_event_description({
				"type": str(block.get("event_type", "")),
				"payload": payload
			})
		"system_text":
			_render_message_block("system", str(block.get("text", "")))


func _history_agent_suffix(block: Dictionary) -> String:
	var agent := str(block.get("agent", "")).strip_edges()
	return " (%s)" % agent if agent != "" else ""


func _render_history_node_tree(block: Dictionary) -> void:
	var lines: Array[String] = [str(block.get("title", "Scene tree"))]
	var tree = block.get("tree", {})
	if tree is Dictionary and not tree.is_empty():
		_append_history_node_lines(tree, "", true, lines)
	else:
		lines.append("(empty)")
	_append_log_stream_message("\n".join(lines), null, true)


func _append_history_node_lines(node: Dictionary, prefix: String, is_last: bool, lines: Array[String]) -> void:
	var branch := "└─ " if is_last else "├─ "
	var name := str(node.get("name", node.get("path", "Node")))
	var type_name := str(node.get("type", "Node"))
	lines.append(prefix + branch + name + " (" + type_name + ")")
	var children: Array = node.get("children", []) if node.get("children", []) is Array else []
	var child_prefix := prefix + ("   " if is_last else "│  ")
	for index in range(children.size()):
		if children[index] is Dictionary:
			_append_history_node_lines(children[index], child_prefix, index == children.size() - 1, lines)


func _render_history_grep_block(block: Dictionary) -> void:
	var lines: Array[String] = []
	lines.append("Grep \"%s\" (in %s)" % [
		str(block.get("pattern", "")).replace("\"", "\\\""),
		str(block.get("include", "project"))
	])
	lines.append("%d match(es)" % int(block.get("match_count", 0)))
	var results: Array = block.get("results", [])
	for result in results:
		if not (result is Dictionary):
			continue
		var location := str(result.get("path", ""))
		var line = result.get("line")
		if line != null:
			location += ":%d" % int(line)
		lines.append("%s %s" % [location, str(result.get("text", ""))])
	if bool(block.get("truncated", false)):
		lines.append("... (truncated)")
	_append_log_stream_message("\n".join(lines))


func _render_history_plan_created(block: Dictionary) -> void:
	var lines: Array[String] = ["Plan created:"]
	var summary := str(block.get("summary", "")).strip_edges()
	if summary != "":
		lines.append(summary)
	var steps: Array = block.get("steps", [])
	for step in steps:
		if not (step is Dictionary):
			continue
		var label := str(step.get("title", "")).strip_edges()
		if label == "":
			label = str(step.get("task", "")).strip_edges()
		var agent := str(step.get("agent", "")).strip_edges()
		var suffix := " (%s)" % agent if agent != "" else ""
		lines.append("%d. %s%s" % [int(step.get("index", 0)), label, suffix])
	_append_log_stream_message("\n".join(lines), null, true)


func _render_history_delegate_results(block: Dictionary) -> void:
	var lines: Array[String] = ["Delegate results:"]
	var results: Array = block.get("results", [])
	for index in range(results.size()):
		var result = results[index]
		if not (result is Dictionary):
			continue
		lines.append("")
		lines.append("**%d. %s**" % [index + 1, str(result.get("agent", "delegate"))])
		lines.append(str(result.get("summary", "")))
	_append_log_stream_message("\n".join(lines), null, true)


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
	var raw_results: Variant = payload.get("results", [])
	var results: Array = raw_results if raw_results is Array else []
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
	var raw_tasks: Variant = payload.get("tasks", [])
	var tasks: Array = raw_tasks if raw_tasks is Array else []
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
	if _commands_requested:
		_commands_requested = false
		_commands_btn.disabled = _state != AgentState.IDLE
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
	_ensure_log_renderer()
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
	_update_context_usage_status(0, _context_token_limit)
	_set_state(AgentState.IDLE)


func _on_interrupt() -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Interrupt requested.", {"state": _status.text})
	_interrupted_locally = true
	_event_queue.clear()
	_draining_events = false
	_clear_inline_confirmation()
	_clear_pending_final_pair()
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
	_save_session_to_history(previous_session_id)
	var session_id := "session_%d" % int(Time.get_unix_time_from_system())
	FrontendLogger.info(editor_interface, "ChatPanel", "New session requested.", {"session_id": session_id})
	_auto_scroll = true
	_interrupted_locally = false
	_event_queue.clear()
	_draining_events = false
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", session_id)
	_save_session_to_history(session_id)
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _http_client != null:
		_http_client.start_new_session(previous_session_id, session_id)
	if state_store != null:
		state_store.reset()
		state_store.set_value("session_id", session_id)
	_clear_messages()
	_update_context_usage_status(0, _context_token_limit)
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
			_enqueue_event(event)
	if not _draining_events:
		_drain_event_queue()


func _enqueue_event(event: Dictionary) -> void:
	_event_queue.append(event)
	_event_queue.sort_custom(func(a, b):
		if not (a is Dictionary) or not (b is Dictionary):
			return false
		return int(a.get("seq", 0)) < int(b.get("seq", 0))
	)


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
	# 同 `_stream_event_key()`：用 `message_index` 而不是会在每次 round-trip 重新清零
	# 的 `loop` 去重，否则跨轮次合批时可能把两个不同轮次的增量错误合并成一条。
	var key := "%s:%s:%s" % [
		str(event.get("type", "")),
		str(payload.get("frame_id", "")),
		str(payload.get("message_index", ""))
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


func _on_final_response(response: Dictionary) -> void:
	if not _pending_final_event.is_empty():
		var event: Dictionary = _pending_final_event.duplicate(true)
		var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
		payload["text"] = str(response.get("text", ""))
		event["payload"] = payload
		_clear_pending_final_pair()
		_enqueue_event(event)
		if not _draining_events:
			_drain_event_queue()
		return
	_pending_final_response = response.duplicate(true)
	_pending_final_received_ms = Time.get_ticks_msec()
	if _http_client != null:
		_http_client.poll_events()


func _clear_pending_final_pair() -> void:
	_pending_final_response.clear()
	_pending_final_event.clear()
	_pending_final_received_ms = -1


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
	if event_type == "server_tool_result":
		_remember_server_file_read(event)
	if event_type == "agent_reasoning_delta":
		_on_reasoning_delta(event)
	elif event_type == "agent_text_delta":
		_on_text_delta(event)
	elif event_type == "final":
		var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
		if payload.has("text"):
			FrontendLogger.debug(editor_interface, "ChatPanel", "[event] -> route: final (via event stream)", {})
			_clear_pending_final_pair()
			_handle_final(payload)
		elif not _pending_final_response.is_empty():
			var response: Dictionary = _pending_final_response.duplicate(true)
			_clear_pending_final_pair()
			FrontendLogger.debug(editor_interface, "ChatPanel", "[event] -> route: final (paired response)", {
				"seq": int(event.get("seq", 0))
			})
			_handle_final(response)
		else:
			_pending_final_event = event.duplicate(true)
	elif event_type == "agent_model_selected":
		var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
		_active_model_name = str(payload.get("model", "")).strip_edges()
		_refresh_status_text()
	else:
		if event_type == "agent_model_fallback":
			var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
			_active_model_name = str(payload.get("fallback_model", "")).strip_edges()
			_refresh_status_text()
		if event_type == "context_usage":
			var usage_payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
			_update_context_usage_status(
				int(usage_payload.get("used_tokens", 0)),
				int(usage_payload.get("token_limit", 0))
			)
		# `compact_started`/`compact_boundary` 总是成对到达（见后端 `compact()`），
		# 中间跨越的才是压缩真正发生的窗口；据此让状态栏在这段时间显示"正在压缩"，
		# 结束后还原成压缩前原本的状态（等待模型/执行工具等），而不是固定回到 IDLE。
		if event_type == "compact_started" and _state != AgentState.IDLE:
			_state_before_compact = _state
			_set_state(AgentState.COMPACTING)
		if _REASONING_BOUNDARY_EVENT_TYPES.has(event_type):
			_mark_reasoning_stream_closed()
			_finish_reasoning_stream()
		var force_milestone_scroll := _MILESTONE_EVENT_TYPES.has(event_type)
		if force_milestone_scroll:
			_force_scroll_once = true
		var rendered_description := _render_event_description(event)
		if rendered_description != "":
			FrontendLogger.debug(editor_interface, "ChatPanel", "-> rendered", {
				"type": event_type,
				"description_len": rendered_description.length()
			})
			if force_milestone_scroll:
				_force_scroll_once = true
				_scroll_to_bottom()
				_post_final_scroll_frames = max(_post_final_scroll_frames, 5)
		if event_type == "compact_boundary" and _state == AgentState.COMPACTING:
			_set_state(_state_before_compact)


func _remember_server_file_read(event: Dictionary) -> void:
	var payload_value = event.get("payload", {})
	if not payload_value is Dictionary:
		return
	var payload: Dictionary = payload_value
	if bool(payload.get("is_error", false)):
		return
	var tool_name := str(payload.get("tool", ""))
	if tool_name != "read_file" and tool_name != "read_script":
		return
	var result_summary_value = payload.get("result_summary", {})
	var path := ""
	if result_summary_value is Dictionary:
		path = str(result_summary_value.get("path", ""))
	if path == "":
		var args_value = payload.get("args", {})
		if args_value is Dictionary:
			path = str(args_value.get("path", ""))
	if path != "" and _tool_executor != null:
		_tool_executor.remember_server_file_read(path)


func _on_extensions() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Extensions requested.")
	if _last_doctor_report.is_empty():
		_extensions_pending = true
		_http_client.fetch_doctor()
	else:
		var payload := {
			"skills": _last_doctor_report.get("skills", []),
			"warnings": _last_doctor_report.get("warnings", [])
		}
		_append_message("system", _format_extensions_report(payload))


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


func _on_permission_selected(index: int) -> void:
	var mode := str(_permission_options.get_item_metadata(index))
	FrontendLogger.info(editor_interface, "ChatPanel", "Permission mode selected.", {"permission_mode": mode})
	ConfigMigrations.set_value(editor_interface, "ai_agent/permission_mode", mode)
	_refresh_status_text()
	if state_store != null:
		state_store.set_value("permission_mode", mode)


func _sync_effort_selection() -> void:
	if editor_interface == null:
		return
	var current := str(ConfigMigrations.get_value(editor_interface, "ai_agent/effort"))
	for index in range(_effort_options.get_item_count()):
		if _effort_options.get_item_text(index) == current:
			_effort_options.select(index)
			return


func _sync_permission_selection() -> void:
	if editor_interface == null:
		return
	var configured := str(ConfigMigrations.get_value(editor_interface, "ai_agent/permission_mode"))
	var current := _normalize_permission_mode(configured)
	if current != configured:
		ConfigMigrations.set_value(editor_interface, "ai_agent/permission_mode", current)
	for index in range(_permission_options.get_item_count()):
		if str(_permission_options.get_item_metadata(index)) == current:
			_permission_options.select(index)
			return


func _permission_choices() -> Array:
	return [
		{"mode": "read_only", "label": _ui("permission_read_only")},
		{"mode": "default", "label": _ui("permission_confirm")},
		{"mode": "full_access", "label": _ui("permission_full")},
	]


func _normalize_permission_mode(mode: String) -> String:
	match mode:
		"read_only", "plan":
			return "read_only"
		"full_access", "auto_approve":
			return "full_access"
		_:
			return "default"


func _permission_label() -> String:
	var mode := "default"
	if editor_interface != null:
		mode = _normalize_permission_mode(str(ConfigMigrations.get_value(editor_interface, "ai_agent/permission_mode")))
	match mode:
		"read_only":
			return _ui("permission_read_only")
		"full_access":
			return _ui("permission_full")
		_:
			return _ui("permission_confirm")


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
			result = _ensure_tool_result_for_call(call, result)
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


func _request_model():
	var model := _model_input.text.strip_edges()
	return model if model != "" else null


func _ensure_tool_result_for_call(call: Dictionary, result: Dictionary) -> Dictionary:
	for key in ["tool_use_id", "frame_id", "status"]:
		if str(result.get(key, "")).strip_edges() == "":
			FrontendLogger.warn(editor_interface, "ChatPanel", "Tool executor returned an invalid result; converting to error result.", {
				"tool": str(call.get("name", "")),
				"tool_use_id": str(call.get("id", "")),
				"frame_id": str(call.get("frame_id", "")),
				"result_keys": result.keys(),
			})
			return AgentDTO.error_result(
				call,
				"Tool executor returned an invalid result without required metadata.",
				"invalid_front_tool_result"
			)
	return result


func _status_text_for_state(value: int) -> String:
	var base := _ui("idle")
	match value:
		AgentState.WAITING_LLM:
			base = _ui("waiting_model")
		AgentState.WAITING_CONFIRM:
			base = _ui("waiting_confirm")
		AgentState.EXECUTING:
			base = _ui("executing")
		AgentState.COMPACTING:
			base = _ui("compacting")
	var parts: Array[String] = [base]
	if _active_model_name != "":
		parts.append(_active_model_name)
	if _last_context_usage_status != "":
		parts.append(_last_context_usage_status)
	return " · ".join(parts)


func _refresh_status_text() -> void:
	_status.text = _status_text_for_state(_state)
	if state_store != null:
		state_store.set_value("state", _status.text)


func _update_context_usage_status(used_tokens: int, token_limit: int) -> void:
	if token_limit > 0:
		_context_token_limit = token_limit
	if _context_token_limit <= 0:
		_last_context_usage_status = ""
	else:
		_last_context_usage_status = EventFormatter.format_context_usage_indicator({
			"used_tokens": maxi(used_tokens, 0),
			"token_limit": _context_token_limit
		}, _ui_table())
	_refresh_status_text()


func _set_state(value: int) -> void:
	var previous_state := _state
	_state = value
	_send_btn.disabled = value != AgentState.IDLE
	_commands_btn.disabled = value != AgentState.IDLE or _commands_requested
	_stop_btn.disabled = value == AgentState.IDLE
	_new_session_btn.disabled = value == AgentState.EXECUTING
	_model_input.editable = value == AgentState.IDLE
	_refresh_status_text()
	if previous_state != value:
		FrontendLogger.debug(editor_interface, "ChatPanel", "State changed.", {
			"from": previous_state,
			"to": value,
			"text": _status.text
		})


func _on_reasoning_delta(event: Dictionary) -> void:
	var raw_payload: Variant = event.get("payload", {})
	var payload: Dictionary = raw_payload if raw_payload is Dictionary else {}
	var key := _stream_event_key(payload)
	var token_count := int(payload.get("token_count", 0))
	if key != "" and _closed_reasoning_keys.has(key):
		var closed_split := _split_reasoning_xml_payload(str(payload.get("text", "")))
		var closed_body := str(closed_split.get("body", ""))
		if closed_body != "":
			_render_text_delta_body(key, closed_body)
		FrontendLogger.debug(editor_interface, "ChatPanel", "[reasoning_delta] IGNORED - key already closed", {
			"key": key,
			"body_len": closed_body.length(),
			"token_count": token_count
		})
		return
	var split := _split_reasoning_xml_payload(str(payload.get("text", "")))
	var text := str(split.get("reasoning", ""))
	var body := str(split.get("body", ""))
	FrontendLogger.debug(editor_interface, "ChatPanel", "[reasoning_delta]", {
		"body_len": body.length(), "key": key, "text_len": text.length(), "preview": text.left(60).replace("\n", "\\n")
	})
	if text != "":
		_ensure_reasoning_entry(key)
		_reasoning_text = text
		if token_count > 0:
			_reasoning_token_count = token_count
		_update_reasoning_entry()
	if body != "":
		_render_text_delta_body(key, body)


func _on_text_delta(event: Dictionary) -> void:
	var raw_payload: Variant = event.get("payload", {})
	var payload: Dictionary = raw_payload if raw_payload is Dictionary else {}
	var text := str(payload.get("text", ""))
	var key := _stream_event_key(payload)
	_render_text_delta_body(key, text)


func _render_text_delta_body(key: String, text: String) -> void:
	if _should_ignore_stream_delta(key, text):
		return
	# 只有当这条 text_delta 确实属于另一个 key 时才提前结束当前 reasoning——
	# 同一个 key（同一 frame_id:loop）下，后端会出现先吐一两句简短旁白
	# text_delta、随后继续吐大段 reasoning_delta 的交错顺序，并不是"text 一
	# 开始就代表这轮 reasoning 已经结束"。之前不分 key 一律关闭，会导致同一
	# key 后续真正的 reasoning_delta 全部被当成"迟到"丢弃（参见
	# `_on_reasoning_delta` 里的 IGNORED 分支）。真正的结束时机交给各响应
	# 路由（tool_calls/最终响应）那几处现有的 `_finish_reasoning_stream()`。
	if key != _reasoning_key:
		_mark_reasoning_stream_closed()
		_finish_reasoning_stream()
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


## 不能用 `loop` 拼 key：`loop` 是单次 `run_turn()` 调用内部的局部计数器，从 0 开始；
## map-agent 这类几乎全是前端工具的 agent，每次前端把工具结果 POST 回去都会触发后端
## 重新调一次 `run_turn()`，同一个 frame 的 `loop` 几乎每轮 round-trip 都会重新变成 1，
## 导致不同轮次的 reasoning/text 流共享同一个 key，互相误判成"迟到的旧流"而被吞掉。
## `message_index`（= 这条增量即将写入 `frame.messages` 的下标）在整个 frame 生命周期
## 内只会单调增长，不会重置，才是真正稳定唯一的"这是哪一次 LLM 调用"标识。
func _stream_event_key(payload: Dictionary) -> String:
	return "%s:%s" % [str(payload.get("frame_id", "")), str(payload.get("message_index", ""))]


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
	_ensure_log_renderer()
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
	# 容器默认 mouse_filter=STOP；鼠标悬停在 Thought 区块（即使没有点在 toggle/
	# 文本上，比如行间空隙）时会拦住滚轮事件，导致流式思考期间无法上滑看历史。
	body.mouse_filter = Control.MOUSE_FILTER_PASS

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
	var header := _format_reasoning_base_header()
	if _reasoning_token_count > 0:
		header += " · %s tokens" % _format_token_count(_reasoning_token_count)
	return header


func _format_reasoning_base_header() -> String:
	var elapsed := 0.0
	if _reasoning_started_ms >= 0:
		elapsed = maxf(0.01, (Time.get_ticks_msec() - _reasoning_started_ms) / 1000.0)
	return "Thought for %.2fs" % elapsed


func _format_token_count(count: int) -> String:
	if count < 1000:
		return str(count)
	return "%d,%03d" % [count / 1000, count % 1000]


func _ensure_stream_message(key: String, indent := false) -> void:
	_ensure_log_renderer()
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
	if _reasoning_toggle != null and is_instance_valid(_reasoning_toggle) and _reasoning_started_ms >= 0:
		var finished_header := _format_reasoning_header()
		_reasoning_toggle.text = "✻  " + finished_header + " ✓"
	_reasoning_key = ""
	_reasoning_toggle = null
	_reasoning_detail_rich = null
	_reasoning_text = ""
	_reasoning_started_ms = -1
	_reasoning_token_count = 0
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


func _render_message_block(role: String, text: String, color = null) -> void:
	_append_message(role, text, color)


func _render_event_description(event: Dictionary) -> String:
	var description := EventFormatter.describe_event(event, _ui_table())
	if description != "":
		_render_message_block("system", description)
	return description


func _append_message(role: String, text: String, color = null) -> void:
	_ensure_log_renderer()
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
	_ensure_log_renderer()
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
	_ensure_log_renderer()
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
	_clear_pending_final_pair()
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
	if not _auto_scroll and not _force_scroll_once:
		return
	if _scroll_request_pending:
		return
	_scroll_request_pending = true
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
	_scroll_request_pending = false
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


func _session_label(session_id: String) -> String:
	if session_id.begins_with("session_"):
		var ts_str := session_id.substr("session_".length())
		if ts_str.is_valid_int():
			var dt := Time.get_datetime_dict_from_unix_time(int(ts_str))
			return "%04d-%02d-%02d %02d:%02d" % [dt.year, dt.month, dt.day, dt.hour, dt.minute]
	return session_id


func _load_session_history() -> Array:
	if editor_interface == null:
		return []
	var json_str := str(ConfigMigrations.get_value(editor_interface, "ai_agent/session_history_json"))
	if json_str.strip_edges() == "" or json_str == "null":
		return []
	var parsed = JSON.parse_string(json_str)
	if parsed is Array:
		return parsed
	return []


func _save_session_to_history(session_id: String) -> void:
	if editor_interface == null or session_id.strip_edges() == "":
		return
	var sessions := _load_session_history()
	for i in range(sessions.size() - 1, -1, -1):
		if sessions[i] is Dictionary and str(sessions[i].get("id", "")) == session_id:
			sessions.remove_at(i)
	sessions.insert(0, {"id": session_id, "label": _session_label(session_id)})
	while sessions.size() > 20:
		sessions.pop_back()
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_history_json", JSON.stringify(sessions))


func _on_show_history() -> void:
	_history_popup.clear()
	_history_session_ids.clear()
	var sessions := _load_session_history()
	if sessions.is_empty():
		_history_popup.add_item(_ui("history_empty"))
		_history_popup.set_item_disabled(0, true)
		_history_session_ids.append("")
	else:
		var current_id := _current_session_id()
		for entry in sessions:
			if not (entry is Dictionary):
				continue
			var sid := str(entry.get("id", ""))
			var label := str(entry.get("label", sid))
			if sid == current_id:
				label += " ✓"
			_history_popup.add_item(label)
			var item_idx := _history_popup.get_item_count() - 1
			if sid == current_id:
				_history_popup.set_item_disabled(item_idx, true)
			_history_session_ids.append(sid)
	var screen_pos: Vector2 = _history_btn.get_screen_transform() * Vector2(0, _history_btn.size.y)
	_history_popup.popup(Rect2i(Vector2i(screen_pos), Vector2i(280, 0)))


func _on_history_item_selected(index: int) -> void:
	if index < 0 or index >= _history_session_ids.size():
		return
	var session_id: String = str(_history_session_ids[index])
	if session_id == "":
		return
	_switch_to_session(session_id)


func _switch_to_session(session_id: String) -> void:
	if session_id == _current_session_id():
		return
	if _state != AgentState.IDLE:
		return
	var previous_session_id := _current_session_id()
	FrontendLogger.info(editor_interface, "ChatPanel", "Switching to session.", {
		"from": previous_session_id,
		"to": session_id
	})
	_save_session_to_history(previous_session_id)
	_auto_scroll = true
	_post_final_scroll_frames = 0
	_post_delta_scroll_frames = 0
	_interrupted_locally = false
	_event_queue.clear()
	_draining_events = false
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", session_id)
	if _http_client != null:
		_http_client.switch_to_session(previous_session_id)
	if state_store != null:
		state_store.reset()
		state_store.set_value("session_id", session_id)
	_clear_messages()
	_update_context_usage_status(0, _context_token_limit)
	_set_state(AgentState.IDLE)
	_http_client.fetch_session_history()
	_save_session_to_history(session_id)


func _current_session_id() -> String:
	if editor_interface == null:
		return "default"
	return str(ConfigMigrations.get_value(editor_interface, "ai_agent/session_id"))
