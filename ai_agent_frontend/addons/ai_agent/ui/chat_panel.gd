@tool
extends VBoxContainer

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const AgentEventSocket = preload("res://addons/ai_agent/service/agent_event_socket.gd")
const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const ContextCollector = preload("res://addons/ai_agent/context/context_collector.gd")
const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")
const ChatPanelText = preload("res://addons/ai_agent/ui/chat_panel_text.gd")
const ChatPanelTheme = preload("res://addons/ai_agent/ui/chat_panel_theme.gd")
const ChatTimelineController = preload("res://addons/ai_agent/controllers/chat_timeline_controller.gd")
const ChatItemRendererRegistry = preload("res://addons/ai_agent/timeline/chat_item_renderer_registry.gd")
const SessionTurnStateReducer = preload("res://addons/ai_agent/state/session_turn_state_reducer.gd")
# 报告格式化逻辑已迁出到独立模块 chat_report_formatter.gd，本文件仅保留调用入口
const ChatReportFormatter = preload("res://addons/ai_agent/ui/chat_report_formatter.gd")
const ChatVirtualScroller = preload("res://addons/ai_agent/ui/chat_virtual_scroller.gd")
const ChatStreamingController = preload("res://addons/ai_agent/controllers/chat_streaming_controller.gd")
const ToolApprovalController = preload("res://addons/ai_agent/controllers/tool_approval_controller.gd")
const SubmissionController = preload("res://addons/ai_agent/controllers/submission_controller.gd")
const RecoveryController = preload("res://addons/ai_agent/controllers/recovery_controller.gd")
const HistoryController = preload("res://addons/ai_agent/controllers/history_controller.gd")
const InlineToolConfirmation = preload("res://addons/ai_agent/ui/inline_tool_confirmation.gd")
const LogEntryRenderer = preload("res://addons/ai_agent/ui/log_entry_renderer.gd")
const RecoveryPrompt = preload("res://addons/ai_agent/ui/recovery_prompt.gd")
const SessionTurnState = preload("res://addons/ai_agent/state/session_turn_state.gd")
const ToolExecutor = preload("res://addons/ai_agent/tools/tool_executor.gd")

enum AgentState {
	IDLE,
	WAITING_LLM,
	WAITING_CONFIRM,
	EXECUTING,
	COMPACTING,
	RESETTING,
	RECOVERING,
	PAUSED,
}

const PENDING_TOOL_RESULTS_ERROR := "当前会话仍有待回传的工具结果，不能开始新的用户消息"
const EVENT_DRAIN_BATCH_SIZE := 24
const EVENT_DRAIN_TIME_BUDGET_MS := 6
const HISTORY_PAGE_SIZE := 40
const MAX_MESSAGE_RENDER_CHARS := 90000
const INPUT_MIN_HEIGHT := 60
const INPUT_MAX_HEIGHT := 240
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
var editor_interface: EditorInterface
var service: Node
var state_store: Node
var undo_manager: Node

var _http_client: Node
var _event_socket: Node
var _session_state := SessionTurnState.new()
var _collector: Node
var _tool_executor: Node
var _recovery_prompt: ConfirmationDialog
var _log_renderer: LogEntryRenderer

var _scroll: ScrollContainer
var _message_list: VBoxContainer
var _confirmation_host: VBoxContainer
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
var _timeline_controller := ChatTimelineController.new()
var _pending_user_local_id := ""
var _renderer_registry := ChatItemRendererRegistry.new()
var _session_state_reducer := SessionTurnStateReducer.new()
var _virtual_scroller: ChatVirtualScroller
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
var _history_before := 0
var _history_has_more := false
var _history_loading := false
var _history_refresh_needed := false
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
var _approval_controller := ToolApprovalController.new()
var _submission_controller := SubmissionController.new()
var _recovery_controller := RecoveryController.new()
var _history_controller := HistoryController.new()
var _inline_confirm := InlineToolConfirmation.new()
var _interrupted_locally := false
var _streaming_controller := ChatStreamingController.new()
var _force_scroll_once := false
var _theme_colors: Dictionary = {}
var _auto_scroll := true
var _suppress_scroll_check := false   # 程序滚动时抑制 value_changed 误判
var _scroll_request_pending := false
var _post_final_scroll_frames := 0   # final 响应后持续滚动到底部的剩余帧数
var _post_delta_scroll_frames := 0   # 文本流刷新后持续滚动到底部的剩余帧数（避免每帧都强制滚动）
var _post_history_layout_frames := 0  # 历史节点完成布局后重新测量虚拟列表的剩余帧数
var _user_is_dragging_scrollbar := false   # 用户正在拖拽滚动条
var _user_scroll_intent := false
var _user_scrolled_up_ms: int = 0   # 用户主动上滚的时间戳，用于冷却期
var _empty_final_ignored_ms: int = -1   # 空 final 被忽略的时间戳，超时后强制结束 turn


func _ready() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Initializing chat panel.")
	_session_state.configure(_current_session_id(), "", 0)
	_session_state_reducer.state = _session_state
	_refresh_theme_colors()
	_build_ui()
	_build_children()
	_connect_signals()
	_set_state(AgentState.IDLE)
	_save_session_to_history(_current_session_id())
	_fetch_initial_service_data()


func _process(_delta: float) -> void:
	if _streaming_controller.has_pending():
		_drain_event_queue()
	if _virtual_scroller != null:
		var resync_request := _virtual_scroller.consume_resync_request()
		if bool(resync_request.get("requested", false)):
			_virtual_scroller.sync(
				float(_scroll.scroll_vertical) if _scroll != null else 0.0,
				bool(resync_request.get("stick_to_bottom", false))
			)
	var selection_now := Time.get_ticks_msec()
	if selection_now - _last_selection_refresh_ms >= 500:
		_last_selection_refresh_ms = selection_now
		_refresh_context_bar()
	if _auto_scroll and not _user_is_dragging_scrollbar and _scroll != null:
		var stream_bar := _scroll.get_v_scroll_bar()
		var stream_bottom := maxf(0.0, stream_bar.max_value - stream_bar.page)
		if float(_scroll.scroll_vertical) < stream_bottom - 2.0:
			_sync_virtual_messages()
			_do_scroll_to_bottom()
	if _post_history_layout_frames > 0:
		_post_history_layout_frames -= 1
		_sync_virtual_messages()
		if _auto_scroll and not _user_is_dragging_scrollbar:
			_do_scroll_to_bottom()
	# final 响应后连续多帧强制滚动到底部，等待 fit_content RichTextLabel 完成布局
	if _post_final_scroll_frames > 0:
		_post_final_scroll_frames -= 1
		if _auto_scroll and not _user_is_dragging_scrollbar:
			_do_scroll_to_bottom()
	# 空 final 超时兜底：收到空 final 后 60 秒内没有真正的 final 到来，强制结束 turn
	if _empty_final_ignored_ms >= 0 and _state != AgentState.IDLE:
		var elapsed_ms := Time.get_ticks_msec() - _empty_final_ignored_ms
		if elapsed_ms > 60000:
			FrontendLogger.warn(editor_interface, "ChatPanel", "[handle_final] TIMEOUT: no real final after 60s, forcing IDLE", {
				"elapsed_ms": str(elapsed_ms)
			})
			_empty_final_ignored_ms = -1
			_present_local_text("system", "⚠ 服务端未返回最终回复，已自动结束。")
			if undo_manager != null:
				undo_manager.commit_batch()
			_set_state(AgentState.IDLE)
			_session_state.complete_turn()
			if state_store != null:
				state_store.set_value("current_turn_id", "")
				state_store.set_value("pending_calls", [])


func _limit_render_text(text: String, max_chars: int) -> String:
	if max_chars <= 0 or text.length() <= max_chars:
		return text
	return text.left(max_chars) + "\n\n... (display truncated)"


## 程序控制滚动到底部，设置抑制标志防止 value_changed 误判
func _do_scroll_to_bottom() -> void:
	if _scroll == null:
		return
	_suppress_scroll_check = true
	var bar := _scroll.get_v_scroll_bar()
	_scroll.scroll_vertical = int(ceil(maxf(0.0, bar.max_value - bar.page)))
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
	_confirmation_host = VBoxContainer.new()
	_confirmation_host.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	add_child(_confirmation_host)

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
	for effort in ["quick", "standard", "deep"]:
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
	_http_client.session_state = _session_state
	add_child(_http_client)
	_submission_controller.configure(_http_client)
	_history_controller.configure(_http_client)

	_event_socket = AgentEventSocket.new()
	_event_socket.editor_interface = editor_interface
	_event_socket.service = service
	_event_socket.configure(_session_state)
	add_child(_event_socket)

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
	_log_renderer.layout_changed = _on_collapsible_layout_changed
	_initialize_virtual_messages()


func _ensure_log_renderer() -> void:
	if _log_renderer != null:
		return
	_log_renderer = LogEntryRenderer.new()
	_log_renderer.theme_colors = _theme_colors
	_log_renderer.editor_interface = editor_interface
	_log_renderer.rich_text_setup = _configure_message_rich_text
	_log_renderer.layout_changed = _on_collapsible_layout_changed


func _initialize_virtual_messages() -> void:
	_renderer_registry.log_renderer = _log_renderer
	_renderer_registry.theme_colors = _theme_colors
	_renderer_registry.ui_text = _ui_table()
	if _virtual_scroller == null:
		_virtual_scroller = ChatVirtualScroller.new()
		_virtual_scroller.setup(_scroll, _message_list, _timeline_controller.store, _renderer_registry)
	var timeline_epoch := str(_session_state.snapshot().get("session_epoch", ""))
	if timeline_epoch.is_empty():
		timeline_epoch = "pending:%s" % _current_session_id()
	if _timeline_controller.store.epoch().is_empty():
		_timeline_controller.reset_epoch(timeline_epoch)


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
	_http_client.error_occurred.connect(_on_error)
	_event_socket.events_received.connect(_on_events)
	_event_socket.error_occurred.connect(_on_error)
	_event_socket.snapshot_required.connect(_on_socket_snapshot_required)
	_event_socket.epoch_changed.connect(_on_socket_epoch_changed)
	_event_socket.application_progress.connect(_on_application_progress)
	_recovery_prompt.accepted_recovery.connect(_on_recovery_accepted)
	_recovery_prompt.rejected_recovery.connect(_on_recovery_rejected)
	_scroll.get_v_scroll_bar().value_changed.connect(_on_scroll_value_changed)
	_scroll.gui_input.connect(_on_scroll_gui_input)
	_scroll.get_v_scroll_bar().gui_input.connect(_on_scroll_gui_input)
	_scroll.scroll_started.connect(_on_scrollbar_button_down)
	_scroll.scroll_ended.connect(_on_scrollbar_button_up)
	if service != null:
		service.service_started.connect(_on_service_started)
		service.service_failed.connect(_on_service_failed)


func _refresh_theme_colors() -> void:
	ChatPanelTheme.refresh_theme_colors(self, editor_interface, _theme_colors)
	_renderer_registry.theme_colors = _theme_colors


func _refresh_live_theme_overrides() -> void:
	_renderer_registry.theme_colors = _theme_colors


func _theme_color(key: String) -> Color:
	return ChatPanelTheme.theme_color(_theme_colors, key)


func _on_send() -> void:
	var text := _input.text.strip_edges()
	if text == "" or _state not in [AgentState.IDLE, AgentState.PAUSED]:
		FrontendLogger.debug(editor_interface, "ChatPanel", "Ignored send request.", {
			"empty": text == "",
			"state": _status.text
		})
		return
	if _try_run_slash_command(text):
		return
	_session_state.set_suppressing(false)
	if not _session_state.begin_submission():
		_on_error("当前 Session 正在处理另一轮请求。")
		return
	_event_socket.connect_stream()
	FrontendLogger.info(editor_interface, "ChatPanel", "Sending user message.", {"chars": text.length()})
	var requested_model = _request_model()
	_active_model_name = str(requested_model) if requested_model != null else _model_input.placeholder_text
	_auto_scroll = true
	_force_scroll_once = true
	_interrupted_locally = false
	_empty_final_ignored_ms = -1   # 重置空 final 超时计时器
	_input.text = ""
	_update_input_height()
	var referenced_paths: Array = _referenced_files.keys()
	_referenced_files.clear()
	_selection_signature = ""
	_refresh_context_bar()
	_set_state(AgentState.WAITING_LLM)
	_pending_user_local_id = _present_local_text("user", text)
	if undo_manager != null:
		undo_manager.begin_batch("AI: " + text.left(40))
	_submission_controller.submit_user(
		text,
		_collector.collect("any", referenced_paths),
		requested_model
	)


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
			_present_local_text("error", _ui("command_param_error"))
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
	_present_local_text("user", text)
	_present_local_text("system", "正在执行命令 /%s …" % command_name)
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
	_message_context_popup.set_item_disabled(0, _message_copy_text(rich) == "")
	_message_context_popup.position = DisplayServer.mouse_get_position()
	_message_context_popup.popup()
	rich.accept_event()


func _message_copy_text(rich: RichTextLabel) -> String:
	if rich == null or not is_instance_valid(rich):
		return ""
	var selected := rich.get_selected_text()
	if selected != "":
		return selected
	var node: Node = rich
	var fallback := ""
	while node != null:
		if node.has_meta("copy_text"):
			fallback = str(node.get_meta("copy_text"))
		node = node.get_parent()
	return fallback


func _on_message_context_action(id: int) -> void:
	match id:
		0:
			if _message_context_source != null and is_instance_valid(_message_context_source):
				var copy_text := _message_copy_text(_message_context_source)
				if copy_text != "":
					DisplayServer.clipboard_set(copy_text)
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
			_present_local_text("system", ChatReportFormatter.extensions_report({
				"skills": response.get("skills", []),
				"warnings": response.get("warnings", [])
			}))
		else:
			_present_local_text("system", ChatReportFormatter.doctor_report(response))
		if state_store != null:
			state_store.set_value("doctor_warnings", response.get("warnings", []))
		return

	if response.has("output_styles"):
		_update_output_styles(response.get("output_styles", []))
		return

	if response.has("history") and response.get("history") is Dictionary:
		var history_response: Dictionary = (response.get("history", {}) as Dictionary).duplicate(true)
		history_response["_snapshot_recovery"] = bool(response.get("_snapshot_recovery", false))
		_handle_session_history(history_response)
		return

	if response.has("session_id") and response.has("pending_turn_id") and response.has("events"):
		_handle_session_history(response)
		return

	if response.has("items") and response.has("ok"):
		_present_local_text("system", ChatReportFormatter.memory_report(response))
		return

	if response.has("ok") and response.has("session_id") and (
		response.has("session_epoch") or response.has("error_code")
	):
		_handle_reset_response(response)
		return

	if response.has("type") and response.get("type") == "data":
		var value = response.get("value", null)
		if value is Array and ChatReportFormatter.looks_like_command_list(value):
			_populate_commands_popup(value)
			if _commands_requested:
				_commands_requested = false
				_commands_btn.disabled = _state != AgentState.IDLE
				_show_commands_popup()
		else:
			_present_local_text("system", ChatReportFormatter.plain_value("数据", value))
		return

	if response.has("ok") and response.has("text"):
		var command_text := ChatReportFormatter.command_response(response)
		_present_local_text("system" if bool(response.get("ok", false)) else "error", command_text)
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
			FrontendLogger.debug(editor_interface, "ChatPanel", "Tool command acknowledged; presentation awaits WebSocket event.")
		"final":
			FrontendLogger.debug(editor_interface, "ChatPanel", "Final command acknowledged; presentation awaits WebSocket event.")
		"error":
			FrontendLogger.debug(editor_interface, "ChatPanel", "Error command acknowledged; presentation awaits WebSocket event.")
		_:
			FrontendLogger.debug(editor_interface, "ChatPanel", "[response] -> route: unknown", {
				"type": str(response.get("type", ""))
			})
			_present_local_text("system", JSON.stringify(response, "\t"))


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
	if _state not in [AgentState.IDLE, AgentState.PAUSED]:
		_present_local_text("error", "当前任务尚未结束，暂时不能运行其他命令。")
		return
	_auto_scroll = true
	_force_scroll_once = true
	_present_local_text("user", "/%s %s" % [command_name, JSON.stringify(args)])
	_present_local_text("system", "正在执行命令 /%s …" % command_name)
	_set_state(AgentState.WAITING_LLM)
	FrontendLogger.info(editor_interface, "ChatPanel", "Running command selected from dropdown.", {
		"command": command_name,
		"arg_keys": args.keys()
	})
	_http_client.run_command(command_name, args)

func _handle_tool_calls(response: Dictionary) -> void:
	var raw_calls: Variant = response.get("calls", [])
	var calls: Array = raw_calls if raw_calls is Array else []
	if calls.is_empty():
		return
	var turn_id := str(response.get("turn_id", ""))
	if not turn_id.is_empty():
		_session_state.adopt_turn(turn_id)
	if _state == AgentState.WAITING_CONFIRM:
		FrontendLogger.warn(editor_interface, "ChatPanel", "Ignoring tool_calls while a previous batch is still pending confirmation.", {"count": calls.size()})
		return
	var confirm: Array = []
	var silent_count := 0
	for call in calls:
		if call is Dictionary and bool(call.get("needs_confirm", false)):
			confirm.append(call)
		elif call is Dictionary:
			silent_count += 1
	var call_names: Array = []
	for call in calls:
		if call is Dictionary:
			call_names.append(str(call.get("name", "")))
	FrontendLogger.info(editor_interface, "ChatPanel", "Handling tool calls.", {
		"count": calls.size(), "silent": silent_count, "confirm": confirm.size(), "names": call_names
	})

	if state_store != null:
		state_store.set_value("current_turn_id", str(_session_state.snapshot().get("active_turn_id", "")))
		state_store.set_value("pending_calls", confirm)

	var results: Array = []
	if confirm.is_empty():
		for call in calls:
			if call is Dictionary:
				if _interrupted_locally:
					return
				_set_state(AgentState.EXECUTING)
				var result: Dictionary = await _tool_executor.execute(call)
				if _interrupted_locally:
					return
				result = _ensure_tool_result_for_call(call, result)
				results.append(result)
	if not confirm.is_empty():
		var leading_results: Array = []
		var ordered_calls: Array = []
		var reached_confirmation := false
		for call in calls:
			if not (call is Dictionary):
				continue
			if bool(call.get("needs_confirm", false)):
				reached_confirmation = true
			if reached_confirmation:
				ordered_calls.append(call)
				continue
			if _interrupted_locally:
				return
			_set_state(AgentState.EXECUTING)
			var leading_result: Dictionary = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			leading_result = _ensure_tool_result_for_call(call, leading_result)
			leading_results.append(leading_result)
		FrontendLogger.info(editor_interface, "ChatPanel", "Waiting for inline tool confirmation.", {"count": confirm.size()})
		_approval_controller.prepare(confirm, leading_results, ordered_calls)
		_show_inline_confirmation(confirm.duplicate(true))
		_set_state(AgentState.WAITING_CONFIRM)
	else:
		_set_state(AgentState.WAITING_LLM)
		_submission_controller.submit_tool_results(results, _request_model())


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
			_session_state.complete_turn()
			_http_client.discard_pending()
		if state_store != null:
			state_store.set_value("current_turn_id", "")
		_set_state(AgentState.IDLE)
		_present_local_text("system", _ui("rejected_turn_ended"))
		return
	_set_state(AgentState.WAITING_LLM)
	_submission_controller.submit_tool_results(results, _request_model())


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


func _handle_final(response: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Received final response.", {
		"chars": str(response.get("text", "")).length()
	})
	var text := str(response.get("text", ""))
	if text.strip_edges().is_empty():
		# 空的 final 只是 agent 中间轮次的心跳/占位，不代表真正回复结束。
		# 跳过所有关闭和状态切换，继续等下一个非空 final。
		if _empty_final_ignored_ms < 0:
			_empty_final_ignored_ms = Time.get_ticks_msec()
		FrontendLogger.debug(editor_interface, "ChatPanel", "[handle_final] EMPTY final ignored, still waiting", {
			"preview": text.left(200).replace("\n", "\\n"),
			"since_ms": _empty_final_ignored_ms
		})
		return
	_empty_final_ignored_ms = -1
	if _auto_scroll:
		_force_scroll_once = true
		_do_scroll_to_bottom()
		_post_final_scroll_frames = 10
	if undo_manager != null:
		undo_manager.commit_batch()
	_set_state(AgentState.IDLE)
	_session_state.complete_turn()
	if state_store != null:
		state_store.set_value("current_turn_id", "")
		state_store.set_value("pending_calls", [])


func _handle_session_history(response: Dictionary) -> void:
	_ensure_log_renderer()
	var snapshot_recovery := bool(response.get("_snapshot_recovery", false))
	if _state != AgentState.IDLE and not (
		snapshot_recovery and _state == AgentState.RECOVERING
	):
		_history_loading = false
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored session history while a turn is active.", {
			"state": _status.text
		})
		return
	var raw_events: Variant = response.get("events", [])
	var events: Array = raw_events if raw_events is Array else []
	var session_id := str(response.get("session_id", ""))
	# 响应 session_id 与当前不符说明是切换会话后迟到的过期响应，直接丢弃。
	if session_id != "" and session_id != _current_session_id():
		_history_loading = false
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored stale session history: session mismatch.", {
			"response_session": session_id,
			"current": _current_session_id()
		})
		return
	FrontendLogger.info(editor_interface, "ChatPanel", "Restoring session history.", {
		"session_id": session_id,
		"count": events.size(),
		"before": int(response.get("history_before", 0)),
		"has_more": bool(response.get("history_has_more", false))
	})
	var requested_before := _history_before
	var next_before := int(response.get("history_before", 0))
	var prepend := _timeline_controller.store.size() > 0 and requested_before > 0
	_update_context_usage_status(
		int(response.get("context_used_tokens", 0)),
		int(response.get("context_token_limit", 0))
	)
	if state_store != null:
		state_store.set_value("session_id", session_id)
	var pending_turn_id = response.get("pending_turn_id")
	_session_state.configure(
		session_id,
		str(response.get("session_epoch", "")),
		int(response.get("last_event_seq", 0))
	)
	var epoch := str(response.get("session_epoch", ""))
	if not prepend:
		_timeline_controller.reset_epoch(epoch)
	if pending_turn_id != null:
		_session_state.adopt_turn(str(pending_turn_id))
		if state_store != null:
			state_store.set_value("current_turn_id", str(pending_turn_id))
	_event_socket.reconnect_from_state()
	var restored := _timeline_controller.prepend_history(events)
	if not restored:
		_on_error("History contained an invalid canonical Timeline record.")
		return
	_history_loading = false
	_history_before = next_before
	_history_has_more = bool(response.get("history_has_more", false)) and next_before > requested_before
	_history_refresh_needed = false
	if pending_turn_id != null:
		_present_local_text("system", _ui("recovered_pending") % [session_id, str(pending_turn_id)])
		_show_pending_results_notice()
		_set_state(AgentState.WAITING_CONFIRM)
	elif snapshot_recovery:
		_set_state(AgentState.IDLE)
	if events.is_empty() and _timeline_controller.store.size() == 0:
		_present_local_text("system", _ui("switch_session_empty"))
	_force_scroll_once = true
	_post_history_layout_frames = 4
	_scroll_to_bottom()


func _on_error(message: String) -> void:
	FrontendLogger.error(editor_interface, "ChatPanel", "Agent error.", {"message": message})
	if _commands_requested:
		_commands_requested = false
		_commands_btn.disabled = _state != AgentState.IDLE
	var visible_message := message
	if message.begins_with("ws_connect_failed:"):
		visible_message = _ui("ws_connect_failed") % message.get_slice(":", 1)
	elif message.begins_with("ws_closed:"):
		visible_message = _ui("ws_closed") % message.get_slice(":", 1)
	elif message == "ws_reconnect_exhausted":
		visible_message = _ui("ws_reconnect_exhausted")
	_present_local_text("error", visible_message)
	if undo_manager != null:
		undo_manager.abort_batch()
	if state_store != null:
		state_store.set_value("pending_calls", [])
	_set_state(AgentState.IDLE)


func _on_problem(problem: Dictionary) -> void:
	## 恢复控制只依赖稳定 disposition/side_effect_state，不解析本地化文本。
	var message := str(problem.get("text", "Unknown error"))
	var disposition := str(problem.get("disposition", "terminal"))
	var side_effect_state := str(problem.get("side_effect_state", "none"))
	FrontendLogger.error(editor_interface, "ChatPanel", "Structured task problem.", {
		"error_code": str(problem.get("error_code", "")),
		"disposition": disposition,
		"side_effect_state": side_effect_state,
		"attempt_id": str(problem.get("attempt_id", "")),
	})
	if disposition == "terminal":
		if undo_manager != null:
			undo_manager.abort_batch()
		_set_state(AgentState.IDLE)
		return
	if side_effect_state == "ambiguous":
		_set_state(AgentState.PAUSED)
		return
	match disposition:
		"continue_agent":
			_set_state(AgentState.RECOVERING)
		"retry_same_attempt", "retry_new_turn", "wait_frontend":
			_show_pending_results_notice()
			_set_state(AgentState.WAITING_CONFIRM)
		"retry_new_attempt", "refresh_and_replan", "pause_for_user":
			_set_state(AgentState.PAUSED)
		_:
			_set_state(AgentState.PAUSED)


func _show_pending_results_notice() -> void:
	_present_local_text("system", _ui("pending_notice"))


func _discard_pending_results() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Discarding pending tool results.")
	_submission_controller.discard_pending()
	_present_local_text("system", _ui("discard_pending"))
	_set_state(AgentState.WAITING_LLM)


func _on_reset() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Reset requested.", {"state": _status.text})
	_auto_scroll = true
	_interrupted_locally = false
	_recovery_controller.begin_reset(_state, _current_session_id())
	_set_state(AgentState.RESETTING)
	_event_socket.close_stream()
	_submission_controller.reset_session()


func _handle_reset_response(response: Dictionary) -> void:
	if not bool(response.get("ok", false)):
		FrontendLogger.error(editor_interface, "ChatPanel", "Reset failed.", {
			"error_code": str(response.get("error_code", "")),
		})
		_present_local_text("error", str(response.get("text", "Reset failed.")))
		var recovery := _recovery_controller.failure_recovery()
		var pending_session_id := str(recovery.get("pending_session_id", ""))
		var previous_session_id := str(recovery.get("previous_session_id", ""))
		if pending_session_id != "" and previous_session_id != "":
			ConfigMigrations.set_value(
				editor_interface,
				"ai_agent/session_id",
				previous_session_id
			)
		_set_state(int(recovery.get("state", AgentState.IDLE)))
		return
	FrontendLogger.info(editor_interface, "ChatPanel", "Reset acknowledged.", {
		"session_id": str(response.get("session_id", "")),
		"session_epoch": str(response.get("session_epoch", "")),
		"last_event_seq": int(response.get("last_event_seq", 0)),
	})
	_streaming_controller.clear()
	_clear_inline_confirmation()
	if _recovery_prompt != null and is_instance_valid(_recovery_prompt):
		_recovery_prompt.hide()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _tool_executor != null and _tool_executor.has_method("reset_session_state"):
		_tool_executor.reset_session_state()
	_timeline_controller.reset_epoch(str(response.get("session_epoch", "")))
	if state_store != null:
		state_store.reset(
			str(response.get("session_epoch", "")),
			int(response.get("last_event_seq", 0))
		)
		state_store.set_value("session_id", str(response.get("session_id", "")))
		state_store.set_value("recovery_pointer", null)
	_update_context_usage_status(0, _context_token_limit)
	_session_state.adopt_reset(
		str(response.get("session_id", "")),
		str(response.get("session_epoch", "")),
		int(response.get("last_event_seq", 0))
	)
	_event_socket.reconnect_from_state()
	_set_state(AgentState.IDLE)
	var pending_session_id := _recovery_controller.complete_reset()
	if pending_session_id != "":
		_present_local_text("system", _ui("new_session_started") % pending_session_id)


func _on_interrupt() -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Interrupt requested.", {"state": _status.text})
	var interrupt_turn_id := str(_session_state.snapshot().get("active_turn_id", ""))
	_interrupted_locally = true
	_streaming_controller.clear()
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _http_client != null:
		_submission_controller.interrupt()
	if _event_socket != null:
		_event_socket.close_stream()
	if state_store != null:
		state_store.set_value("pending_calls", [])
		state_store.set_value("current_turn_id", "")
	_timeline_controller.interrupt_pending_tools(interrupt_turn_id)
	_set_state(AgentState.PAUSED)
	_present_local_text("system", _ui("interrupted"))


func _on_new_session() -> void:
	var previous_session_id := _current_session_id()
	_save_session_to_history(previous_session_id)
	var session_id := "session_%s" % Crypto.new().generate_random_bytes(16).hex_encode()
	FrontendLogger.info(editor_interface, "ChatPanel", "New session requested.", {"session_id": session_id})
	_auto_scroll = true
	_interrupted_locally = false
	_recovery_controller.begin_reset(_state, previous_session_id, session_id)
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", session_id)
	_save_session_to_history(session_id)
	if _http_client != null:
		_event_socket.close_stream()
		_http_client.start_new_session(previous_session_id, session_id)
	_set_state(AgentState.RESETTING)


func _on_recovery_accepted(pointer: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Recovery accepted.", {
		"session_id": str(pointer.get("session_id", "")),
		"pending_turn_id": str(pointer.get("pending_turn_id", ""))
	})
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", str(pointer.get("session_id", "default")))
	_clear_messages()   # 清空当前内容，确保历史加载时消息列表为空
	_session_state.configure(
		str(pointer.get("session_id", "default")),
		str(pointer.get("session_epoch", "")),
		int(pointer.get("last_event_seq", 0))
	)
	var pending_turn_id = pointer.get("pending_turn_id")
	if pending_turn_id != null:
		_session_state.adopt_turn(str(pending_turn_id))
	_event_socket.reconnect_from_state()
	_history_controller.fetch_initial()
	if state_store != null:
		state_store.merge({
			"session_id": str(pointer.get("session_id", "default")),
			"recovery_pointer": pointer,
			"last_event_seq": int(pointer.get("last_event_seq", 0)),
			"current_turn_id": str(_session_state.snapshot().get("active_turn_id", ""))
		})


func _on_recovery_rejected() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Recovery rejected.")
	_http_client.dismiss_recovery_pointer()
	if state_store != null:
		state_store.set_value("recovery_pointer", null)
	_present_local_text("system", _ui("recovery_dismissed"))


func _on_service_started(base_url: String) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Service started signal received.", {"base_url": base_url})
	_fetch_initial_service_data()
	if service != null and not service.is_running():
		_present_local_text("system", _ui("service_manual") % [base_url, str(service.token)])


func _on_service_failed(message: String) -> void:
	FrontendLogger.error(editor_interface, "ChatPanel", "Service failed signal received.", {"message": message})
	_present_local_text("error", _ui("service_failed") % message)
	if service != null and str(service.token) != "":
		_present_local_text("system", _ui("service_manual_full") % [str(service.base_url), str(service.token)])


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
	_history_controller.fetch_initial()
	_http_client.fetch_recovery_pointer()
	_http_client.fetch_output_styles()


func _on_events(events: Array) -> void:
	if _interrupted_locally:
		FrontendLogger.debug(editor_interface, "ChatPanel", "Suppressed events after interrupt.", {"count": events.size()})
		return
	for event in events:
		if event is Dictionary and _http_client != null:
			_http_client.recover_tool_calls_response(event)
	var coalesced := _streaming_controller.coalesce(events)
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
	if _streaming_controller.has_pending():
		_drain_event_queue()


func _on_socket_snapshot_required(problem: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "WebSocket", "Snapshot recovery required.", {
		"reason": str(problem.get("reason", "unknown")),
	})
	_set_state(AgentState.RECOVERING)
	_history_controller.fetch_snapshot()


func _on_socket_epoch_changed(
	previous_epoch: String,
	new_epoch: String,
	last_event_seq: int
) -> void:
	FrontendLogger.info(editor_interface, "WebSocket", "Session epoch changed.", {
		"previous_epoch": previous_epoch,
		"new_epoch": new_epoch,
		"last_event_seq": last_event_seq,
	})


func _on_application_progress(_event: Dictionary) -> void:
	_http_client.notify_application_progress()


func _enqueue_event(event: Dictionary) -> void:
	_streaming_controller.enqueue(event)


func _drain_event_queue() -> void:
	if not _streaming_controller.has_pending():
		return
	if _interrupted_locally:
		_streaming_controller.clear()
		return
	var batch := _streaming_controller.take_batch(
		EVENT_DRAIN_BATCH_SIZE,
		EVENT_DRAIN_TIME_BUDGET_MS
	)
	for event in batch:
		if event is Dictionary:
			_handle_event(event)


func _handle_event(event: Dictionary) -> void:
	var event_type := str(event.get("type", ""))
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	if event_type == "user_submitted" and _pending_user_local_id != "":
		_timeline_controller.promote_local_to_next_insert(_pending_user_local_id)
		_pending_user_local_id = ""
	_session_state_reducer.reduce(event)
	if not _timeline_controller.present_event(event):
		FrontendLogger.error(editor_interface, "ChatPanel", "Canonical Timeline rejected event.", {
			"type": event_type,
			"seq": int(event.get("seq", 0)),
		})
		return
	if event_type == "server_tool_result":
		_remember_server_file_read(event)
	if event_type == "tool_calls":
		_handle_tool_calls(payload)
	elif event_type == "final":
		if payload.has("text"):
			_handle_final(payload)
		else:
			_on_error("Final event is missing presentation text.")
	elif event_type == "error":
		_on_problem(payload)
	elif event_type == "agent_model_selected":
		_active_model_name = str(payload.get("model", "")).strip_edges()
		_refresh_status_text()
	elif event_type == "agent_model_fallback":
		_active_model_name = str(payload.get("fallback_model", "")).strip_edges()
		_refresh_status_text()
	elif event_type == "context_usage":
		_update_context_usage_status(int(payload.get("used_tokens", 0)), int(payload.get("token_limit", 0)))
	elif event_type == "compact_started" and _state != AgentState.IDLE:
		_state_before_compact = _state
		_set_state(AgentState.COMPACTING)
	elif event_type == "compact_boundary" and _state == AgentState.COMPACTING:
		_set_state(_state_before_compact)
	if _MILESTONE_EVENT_TYPES.has(event_type) and _auto_scroll:
		_force_scroll_once = true
		_scroll_to_bottom()


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
		_present_local_text("system", ChatReportFormatter.extensions_report(payload))


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
	# verify/advisor 是固定内部档位，不再暴露给用户；历史值降级回 standard。
	if current == "verify" or current == "advisor":
		current = "standard"
		ConfigMigrations.set_value(editor_interface, "ai_agent/effort", "standard")
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
	_inline_confirm.show(_confirmation_host, calls, _ui_table(), _theme_colors, _on_inline_apply, _on_inline_reject)
	_sync_virtual_messages()
	# 确保确认面板出现时自动滚动到底部，让用户看到需要操作的内容
	_auto_scroll = true
	_force_scroll_once = true
	_do_scroll_to_bottom()
	# 布局可能需要额外帧才能稳定，用 _process 多帧兜底
	_post_final_scroll_frames = max(_post_final_scroll_frames, 5)


## 用户点击"应用"：按 _pending_ordered_calls 的原始顺序逐项处理。
## 每个调用先通过 _pending_confirmation_index 判断它是否真的需要确认：
## - 需要确认的项：读取确认面板中对应条目的 should_apply 状态，决定执行或拒绝；
##   grant_session_allow 仅附着在这类真正经过用户确认的调用上。
## - 不需要确认的项（暂停点之后夹带的静默调用）：直接执行，should_apply 恒为 true。
func _on_inline_apply() -> void:
	if _inline_confirm.is_busy():
		return
	_inline_confirm.set_busy(true)
	var results: Array = _approval_controller.leading_results()
	for call in _approval_controller.ordered_calls():
		if not (call is Dictionary):
			continue
		var confirmation_index := _pending_confirmation_index(str(call.get("id", "")))
		var needs_confirmation := confirmation_index >= 0
		var should_apply := (
			_inline_confirm.should_apply(confirmation_index) if needs_confirmation else true
		)
		if should_apply:
			if _interrupted_locally:
				return
			_set_state(AgentState.EXECUTING)
			var result: Dictionary = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			result = _ensure_tool_result_for_call(call, result)
			if needs_confirmation:
				# grant_session_allow 只附着在真正经过用户确认的调用上，
				# 避免夹带的静默调用意外获得会话级权限豁免。
				result["grant_session_allow"] = _inline_confirm.grant_session_allow()
			results.append(result)
		else:
			var rejected := AgentDTO.rejected_result(call)
			results.append(rejected)
	_inline_confirm.set_busy(false)
	_on_decision(results)


## 用户点击"拒绝"：按原始顺序遍历 _pending_ordered_calls。
## - 需要确认的项：生成 rejected 结果回传模型，让模型知道用户拒绝了该编辑，
##   可以继续给出建设性回复（如手动修改步骤或降级方案），而非前端单方面中断。
## - 不需要确认的项（夹带的静默调用）：正常执行，不因其后的确认项被拒绝而跳过。
func _on_inline_reject() -> void:
	if _inline_confirm.is_busy():
		return
	_inline_confirm.set_busy(true)
	var results: Array = _approval_controller.leading_results()
	for call in _approval_controller.ordered_calls():
		if not (call is Dictionary):
			continue
		var confirmation_index := _pending_confirmation_index(str(call.get("id", "")))
		var needs_confirmation := confirmation_index >= 0
		if needs_confirmation:
			var rejected := AgentDTO.rejected_result(call)
			results.append(rejected)
			continue
		if _interrupted_locally:
			return
		_set_state(AgentState.EXECUTING)
		var result: Dictionary = await _tool_executor.execute(call)
		if _interrupted_locally:
			return
		result = _ensure_tool_result_for_call(call, result)
		results.append(result)
	_inline_confirm.set_busy(false)
	_on_decision(results)


## 在 _pending_calls（仅含 needs_confirm 的调用）中查找指定 tool_use_id 的索引。
## 返回 -1 表示该调用不需要确认（是暂停点之后夹带的静默调用）。
func _pending_confirmation_index(tool_use_id: String) -> int:
	return _approval_controller.confirmation_index(tool_use_id)


## 仅拆除确认框的 UI（旧的 checkbox/diff 预览/按钮），不触碰 `_pending_calls` /
## `_pending_silent_results`。`_show_inline_confirmation` 在构建新一轮确认框
## 前调用它来清掉上一轮遗留的控件——如果改用下面这个会清空 pending 数据的
## 完整版本，就会把调用者刚刚（在它之前一行）写入的 `_pending_calls` 清空，
## 导致确认框显示正常，但用户点"应用"/"拒绝"时已经没有数据可回传。
func _clear_inline_confirmation_ui() -> void:
	_inline_confirm.clear_ui()


func _clear_inline_confirmation() -> void:
	_clear_inline_confirmation_ui()
	_approval_controller.clear()


func _request_model():
	var model := _model_input.text.strip_edges()
	return model if model != "" else null


func _ensure_tool_result_for_call(call: Dictionary, result: Dictionary) -> Dictionary:
	# ── 排查日志：记录所有字段值 ──
	var _dbg_tool_use_id := str(result.get("tool_use_id", ""))
	var _dbg_frame_id := str(result.get("frame_id", ""))
	var _dbg_status := str(result.get("status", ""))
	var _empty_fields: Array = []
	if _dbg_tool_use_id.strip_edges() == "":
		_empty_fields.append("tool_use_id")
	if _dbg_frame_id.strip_edges() == "":
		_empty_fields.append("frame_id")
	if _dbg_status.strip_edges() == "":
		_empty_fields.append("status")
	FrontendLogger.info(editor_interface, "ChatPanel", "_ensure_tool_result metadata snapshot.", {
		"tool": str(call.get("name", "")),
		"call_id": str(call.get("id", "")),
		"call_frame_id": str(call.get("frame_id", "")),
		"result_tool_use_id": _dbg_tool_use_id,
		"result_frame_id": _dbg_frame_id,
		"result_status": _dbg_status,
		"empty_fields": _empty_fields,
		"result_keys": result.keys(),
	})
	for key in ["tool_use_id", "frame_id", "status"]:
		if str(result.get(key, "")).strip_edges() == "":
			FrontendLogger.warn(editor_interface, "ChatPanel", "Tool executor returned an invalid result; converting to error result.", {
				"tool": str(call.get("name", "")),
				"tool_use_id": str(call.get("id", "")),
				"frame_id": str(call.get("frame_id", "")),
				"result_keys": result.keys(),
				"empty_fields": _empty_fields,
				"result_tool_use_id": _dbg_tool_use_id,
				"result_frame_id": _dbg_frame_id,
				"result_status": _dbg_status,
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
		AgentState.RESETTING:
			base = _ui("state_resetting")
		AgentState.RECOVERING:
			base = _ui("state_recovering")
		AgentState.PAUSED:
			base = _ui("state_paused")
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
	_send_btn.disabled = value not in [AgentState.IDLE, AgentState.PAUSED]
	_commands_btn.disabled = (
		value not in [AgentState.IDLE, AgentState.PAUSED] or _commands_requested
	)
	_stop_btn.disabled = value in [AgentState.IDLE, AgentState.RESETTING]
	_new_session_btn.disabled = value in [AgentState.EXECUTING, AgentState.RESETTING]
	_model_input.editable = value in [AgentState.IDLE, AgentState.PAUSED]
	_refresh_status_text()
	if previous_state != value:
		FrontendLogger.debug(editor_interface, "ChatPanel", "State changed.", {
			"from": previous_state,
			"to": value,
			"text": _status.text
		})
func _present_local_text(role: String, text: String, color = null) -> String:
	var style_token := "error" if role == "error" else role
	var item_id := _timeline_controller.present_local_text(role, _limit_render_text(text, MAX_MESSAGE_RENDER_CHARS), style_token)
	if item_id.is_empty():
		FrontendLogger.error(editor_interface, "ChatPanel", "Local Timeline item rejected.", {"role": role})
		return ""
	_scroll_to_bottom()
	return item_id


func _clear_messages(reset_history := true) -> void:
	FrontendLogger.debug(editor_interface, "ChatPanel", "Timeline cleared.", {
		"reset_history": reset_history,
		"store_before": _timeline_controller.store.size(),
	})
	_pending_user_local_id = ""
	var epoch := str(_session_state.snapshot().get("session_epoch", ""))
	if epoch.is_empty():
		epoch = "pending:%s" % _current_session_id()
	_timeline_controller.reset_epoch(epoch)
	if reset_history:
		_history_before = 0
		_history_has_more = false
		_history_loading = false
		_history_refresh_needed = false


func _scroll_to_bottom() -> void:
	_sync_virtual_messages()
	if not _auto_scroll and not _force_scroll_once:
		return
	_force_scroll_once = false
	if _scroll_request_pending:
		return
	_scroll_request_pending = true
	call_deferred("_scroll_to_bottom_deferred")


func _sync_virtual_messages() -> void:
	if _virtual_scroller != null:
		_virtual_scroller.sync(float(_scroll.scroll_vertical) if _scroll != null else 0.0, _auto_scroll or _force_scroll_once)


func _on_collapsible_layout_changed() -> void:
	_post_history_layout_frames = max(_post_history_layout_frames, 2)


func _on_scroll_value_changed(value: float) -> void:
	var bar := _scroll.get_v_scroll_bar()
	var scroll_max := bar.max_value - bar.page
	var is_at_bottom := scroll_max <= 0 or value >= scroll_max - 8
	if _suppress_scroll_check and not is_at_bottom:
		return

	if is_at_bottom:
		# Only an identified user navigation can change follow intent. Layout
		# growth and virtual-scroller corrections also emit value_changed.
		if _user_scroll_intent or _user_is_dragging_scrollbar:
			_auto_scroll = true
			_user_scrolled_up_ms = 0
	elif _user_scroll_intent or _user_is_dragging_scrollbar:
		_auto_scroll = false
		_user_scrolled_up_ms = Time.get_ticks_msec()
	var history_before := _history_request_before(value)
	if history_before >= 0:
		FrontendLogger.debug(editor_interface, "ChatPanel", "History page requested.", {
			"value": value,
			"history_before": history_before,
			"refresh": _history_refresh_needed,
			"store_size": _timeline_controller.store.size(),
		})
		_history_loading = true
		_history_controller.fetch_page(HISTORY_PAGE_SIZE, history_before)
	if _virtual_scroller != null:
		_virtual_scroller.on_scroll_changed(value)


func _history_request_before(value: float) -> int:
	if _state != AgentState.IDLE or _history_loading:
		return -1
	if _history_refresh_needed:
		return 0
	if value <= 40.0 and _history_has_more:
		return _history_before
	return -1


func _on_scrollbar_button_down() -> void:
	_user_is_dragging_scrollbar = true
	_user_scroll_intent = true


func _on_scrollbar_button_up() -> void:
	_user_is_dragging_scrollbar = false
	_user_scroll_intent = false


func _on_scroll_gui_input(event: InputEvent) -> void:
	var navigation := false
	if event is InputEventMouseButton:
		navigation = (
			event.button_index == MOUSE_BUTTON_WHEEL_UP
			or event.button_index == MOUSE_BUTTON_WHEEL_DOWN
		)
	elif event is InputEventPanGesture:
		navigation = true
	elif event is InputEventKey and event.pressed:
		navigation = event.keycode in [
			KEY_UP,
			KEY_DOWN,
			KEY_PAGEUP,
			KEY_PAGEDOWN,
			KEY_HOME,
			KEY_END,
		]
	if not navigation:
		return
	_user_scroll_intent = true
	call_deferred("_clear_scroll_intent")


func _clear_scroll_intent() -> void:
	if not _user_is_dragging_scrollbar:
		_user_scroll_intent = false


func _scroll_to_bottom_deferred() -> void:
	_scroll_request_pending = false
	if _scroll == null:
		return
	if not _auto_scroll:
		return
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
	_post_history_layout_frames = 0
	_interrupted_locally = false
	_streaming_controller.clear()
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
	_history_controller.fetch_initial()
	_save_session_to_history(session_id)


func _current_session_id() -> String:
	if editor_interface == null:
		return "default"
	return str(ConfigMigrations.get_value(editor_interface, "ai_agent/session_id"))
