@tool
extends VBoxContainer

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const ChatEventSocket = preload("res://addons/ai_agent/service/chat_event_socket.gd")
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
const TranscriptStore = preload("res://addons/ai_agent/transcript/transcript_store.gd")
const TranscriptProjector = preload("res://addons/ai_agent/transcript/transcript_projector.gd")
const TranscriptRecovery = preload("res://addons/ai_agent/transcript/transcript_recovery.gd")
const TranscriptRenderer = preload("res://addons/ai_agent/transcript/transcript_renderer.gd")
const TranscriptViewport = preload("res://addons/ai_agent/transcript/transcript_viewport.gd")
const TranscriptPatchBatcher = preload("res://addons/ai_agent/transcript/transcript_patch_batcher.gd")
const TransientNoticeHost = preload("res://addons/ai_agent/ui/transient_notice_host.gd")

enum AgentState { IDLE, WAITING_LLM, WAITING_CONFIRM, EXECUTING, COMPACTING }

const PENDING_TOOL_RESULTS_ERROR := "当前会话仍有待回传的工具结果，不能开始新的用户消息"
const EVENT_DRAIN_BATCH_SIZE := 24
const EVENT_DRAIN_TIME_BUDGET_MS := 6
const MAX_MESSAGE_LIST_CHILDREN := 240
const INPUT_MIN_HEIGHT := 60
const INPUT_MAX_HEIGHT := 240

var editor_interface: EditorInterface
var service: Node
var state_store: Node
var undo_manager: Node

var _http_client: Node
var _event_socket: Node
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
var _return_to_latest_btn: Button
var _history_popup: PopupMenu
var _history_session_ids: Array = []
var _effort_options: OptionButton
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
var _event_queue: Array = []
var _draining_events := false
var _force_scroll_once := false

## 可合并流式补丁的按条目暂存与投影窗口（任务 3.2 / 3.3）。
## WebSocket 接收即确认（socket 层已发 ack）；批处理器保序暂存同一条目的
## 修订并就地合并连续增量，窗口内按序应用——首个完整补丁（建立条目）绝不
## 会被其后的增量覆盖。非流式/终态补丁立即应用，不延迟超过一个帧边界。
var _patch_batcher: RefCounted = TranscriptPatchBatcher.new()
var _projection_window_pending := false
var _last_projection_window_usec := 0
## 单个投影窗口内最多应用的条目数；超出部分顺延到下一个窗口，保证每帧有界。
const PROJECTION_WINDOW_MAX_ENTRIES := 64
## 相邻两个投影窗口的最小间隔（微秒）；把逐包同步重排降为受限帧率批处理。
const PROJECTION_WINDOW_INTERVAL_USEC := 30_000

## 权威展示稿三件套：Store（数据）+ Projector（水合状态机）+ Renderer（按 kind 渲染）。
var _transcript_store: RefCounted
var _projector: RefCounted
var _recovery: RefCounted
## 已渲染水位（任务 2.1）：视口已接受渲染的最大 projected 游标。
var _rendered_upto_event_seq := 0
var _last_stall_check_msec := 0
## 半开订阅新鲜度检测（任务 2.10）：服务端协议心跳默认 20s、活跃轮次
## keepalive 15s；静默超过两倍心跳窗口即可疑。探针响应有界，超时后
## 允许下一个静默回合重新探测。
const SOCKET_SILENCE_THRESHOLD_MS := 45_000
const SOCKET_PROBE_TIMEOUT_MS := 15_000
## 在途静默探针的发起时间（毫秒）；0 表示没有在途探针。
var _socket_silence_probe_msec := 0
var _transcript_renderer: RefCounted
var _transcript_viewport: RefCounted
## 本地瞬时提示宿主：等待/命令执行等提示不进入展示稿（任务 3.4）。
var _transient_host: RefCounted
## 当前水合世代；发起一次历史加载前递增，用于拒绝迟到的旧会话/旧世代响应。
var _hydration_generation := 0
## 空闲恢复（任务 4.2/4.3）：HTTP 客户端恢复窗口期间为 true。
var _idle_recovery_active := false
## 恢复水合标记：为 true 时允许在活跃轮次中应用历史快照水合。
var _hydrating_for_recovery := false
## 本轮是否已经通过 transcript_patch 接受了完成态助手条目；HTTP final 只作确认，
## 若完成补丁始终没有实时到达则回退到重新水合（任务 3.2）。
var _assistant_completed_this_turn := false
## 乐观用户条目的 client_message_id 生成计数器。
var _optimistic_counter := 0

var _theme_colors: Dictionary = {}
var _auto_scroll := true
var _suppress_scroll_check := false   # 程序滚动时抑制 value_changed 误判
var _scroll_request_pending := false
var _post_final_scroll_frames := 0   # final 响应后持续滚动到底部的剩余帧数


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
	# final 响应后连续多帧强制滚动到底部，等待 fit_content RichTextLabel 完成布局
	if _post_final_scroll_frames > 0:
		_post_final_scroll_frames -= 1
		_do_scroll_to_bottom()
	# 可合并流式补丁的受限帧率投影窗口（任务 3.3）。
	if _projection_window_pending \
			and Time.get_ticks_usec() - _last_projection_window_usec >= PROJECTION_WINDOW_INTERVAL_USEC:
		_run_projection_window()
	# 可见停滞看门狗（任务 2.3）：每秒检查一次，仅在活跃轮次中生效。
	if selection_now - _last_stall_check_msec >= 1000:
		_last_stall_check_msec = selection_now
		_check_visible_stall()


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
	elif what == NOTIFICATION_PREDELETE and _event_socket != null:
		_event_socket.stop()


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
	_return_to_latest_btn = Button.new()
	_return_to_latest_btn.text = "回到最新"
	_return_to_latest_btn.visible = false
	toolbar.add_child(_return_to_latest_btn)

	_scroll = ScrollContainer.new()
	_scroll.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_scroll.size_flags_vertical = Control.SIZE_EXPAND_FILL
	add_child(_scroll)

	# ScrollContainer 只对单个子控件做布局/滚动：转录列表与瞬时提示列表
	# 必须包在同一个容器里，否则提示层（确认框等）不会跟随滚动显示。
	var scroll_body := VBoxContainer.new()
	scroll_body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	scroll_body.add_theme_constant_override("separation", 10)
	_scroll.add_child(scroll_body)

	_message_list = VBoxContainer.new()
	_message_list.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_message_list.add_theme_constant_override("separation", 10)
	scroll_body.add_child(_message_list)

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

	_event_socket = ChatEventSocket.new()
	_event_socket.editor_interface = editor_interface
	_event_socket.service = service
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
	_message_context_popup.add_separator()
	_message_context_popup.add_item("复制全文", 3)
	add_child(_message_context_popup)

	_log_renderer = LogEntryRenderer.new()
	_log_renderer.theme_colors = _theme_colors
	_log_renderer.editor_interface = editor_interface
	_log_renderer.rich_text_setup = _configure_message_rich_text

	_transcript_store = TranscriptStore.new()
	_projector = TranscriptProjector.new(_transcript_store)
	_recovery = TranscriptRecovery.new()
	_recovery.notify_visible_progress()
	_recovery.recovery_started.connect(_on_recovery_started)
	_recovery.recovery_escalated.connect(_on_recovery_escalated)
	_recovery.recovery_finished.connect(_on_recovery_finished)
	_recovery.recovery_exhausted.connect(_on_recovery_exhausted)
	_transcript_renderer = TranscriptRenderer.new()
	_transcript_renderer.log_renderer = _log_renderer
	_transcript_renderer.theme_colors = _theme_colors
	_transcript_renderer.ui_text = _ui
	_transcript_viewport = TranscriptViewport.new()
	var transient_mount := VBoxContainer.new()
	transient_mount.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_transcript_viewport.attach(_message_list, _transcript_renderer, _transcript_store, _scroll, transient_mount)
	_transcript_viewport.older_page_requested.connect(_on_older_page_requested)
	_transcript_viewport.diagnostics_changed.connect(_on_viewport_diagnostics)
	_transcript_viewport.follow_mode_changed.connect(_on_viewport_follow_mode_changed)
	_transcript_viewport.renderer_rejected.connect(_on_transcript_renderer_rejected)
	_projector.patch_rejected.connect(_on_transcript_patch_rejected)
	_projector.hydration_required.connect(_on_projector_hydration_required)

	_transient_host = TransientNoticeHost.new()
	_transient_host.node_factory = _log_renderer
	_transient_host.theme_colors = _theme_colors
	_transient_host.ui_text = _ui
	# 瞬时提示不进入 Store，却必须位于已挂载条目与虚拟 bottom spacer 之间；
	# 直接追加到 _message_list 会使错误/报告提示被未挂载历史的 spacer 隔开。
	_transient_host.attach(transient_mount)
	_transcript_renderer.error_retry_requested.connect(_on_transcript_retry_requested)


func _connect_signals() -> void:
	_send_btn.pressed.connect(_on_send)
	_stop_btn.pressed.connect(_on_interrupt)
	_new_session_btn.pressed.connect(_on_new_session)
	_input.gui_input.connect(_on_input_gui_input)
	_input.text_changed.connect(func(): _on_input_text_changed(_input.text))
	_effort_options.item_selected.connect(_on_effort_selected)
	_style_options.item_selected.connect(_on_style_selected)
	_doctor_btn.pressed.connect(func(): _http_client.fetch_doctor())
	_extensions_btn.pressed.connect(_on_extensions)
	_commands_btn.pressed.connect(_on_show_commands)
	_commands_popup.id_pressed.connect(_on_command_selected)
	_memory_btn.pressed.connect(func(): _http_client.fetch_memory())
	_reset_btn.pressed.connect(_on_reset)
	_history_btn.pressed.connect(_on_show_history)
	_return_to_latest_btn.pressed.connect(_on_return_to_latest)
	_history_popup.index_pressed.connect(_on_history_item_selected)
	_file_suggestions.item_clicked.connect(func(index: int, _position: Vector2, mouse_button: int):
		if mouse_button == MOUSE_BUTTON_LEFT:
			_on_file_reference_selected(index)
	)
	_file_suggestions.item_activated.connect(_on_file_reference_selected)
	_message_context_popup.id_pressed.connect(_on_message_context_action)
	_http_client.response_received.connect(_on_response)
	_http_client.error_occurred.connect(_on_error)
	_http_client.idle_recovery_requested.connect(_on_idle_recovery_requested)
	_http_client.idle_recovery_finished.connect(_on_idle_recovery_finished)
	_http_client.probe_response_received.connect(_on_probe_response)
	_event_socket.event_received.connect(_on_event_received)
	_event_socket.history_gap_received.connect(_on_event_history_gap)
	_event_socket.resync_required_received.connect(_on_event_resync_required)
	_event_socket.sequence_gap_detected.connect(_on_event_sequence_gap)
	_event_socket.oversized_packet_rejected.connect(_on_event_oversized_packet)
	_event_socket.protocol_error_received.connect(_on_event_protocol_error)
	_recovery_prompt.accepted_recovery.connect(_on_recovery_accepted)
	_recovery_prompt.rejected_recovery.connect(_on_recovery_rejected)
	_scroll.get_v_scroll_bar().value_changed.connect(_on_scroll_value_changed)
	if service != null:
		service.service_started.connect(_on_service_started)
		service.service_failed.connect(_on_service_failed)


func _refresh_theme_colors() -> void:
	ChatPanelTheme.refresh_theme_colors(self, editor_interface, _theme_colors)


func _refresh_live_theme_overrides() -> void:
	if _transcript_renderer != null:
		_transcript_renderer.theme_colors = _theme_colors
	if _log_renderer != null:
		_log_renderer.theme_colors = _theme_colors
	if _transient_host != null:
		_transient_host.theme_colors = _theme_colors
	if _transcript_viewport != null:
		_transcript_viewport.advance_presentation_epoch()


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
	_assistant_completed_this_turn = false
	_input.text = ""
	_update_input_height()
	var referenced_paths: Array = _referenced_files.keys()
	_referenced_files.clear()
	_selection_signature = ""
	_refresh_context_bar()
	# 乐观用户条目：仅凭 client_message_id 与服务端展示稿条目对账；服务端条目
	# 到达（补丁或快照）时自动替换该乐观条目。
	_optimistic_counter += 1
	var client_message_id := "cm_%d_%d" % [Time.get_ticks_msec(), _optimistic_counter]
	_transcript_store.add_optimistic_user_entry(text, client_message_id)
	_render_optimistic_user_entry(client_message_id, text)
	_transient_host.show_keyed("waiting", _ui("waiting_model"), "system")
	_scroll_to_bottom()
	_set_state(AgentState.WAITING_LLM)
	if undo_manager != null:
		undo_manager.begin_batch("AI: " + text.left(40))
	_http_client.send_user_message(text, _collector.collect("any", referenced_paths), requested_model, client_message_id)


## 渲染乐观用户条目（临时节点；快照/补丁确认后由展示稿渲染器接管）。
func _render_optimistic_user_entry(client_message_id: String, text: String) -> void:
	var entry: Dictionary = _transcript_store.get_entry("optimistic:" + client_message_id)
	if entry.is_empty():
		return
	_transcript_renderer.apply_entry(entry)
	_scroll_to_bottom()


## 本地结束一轮（final 确认/超时兜底共用）：提交 undo、复位状态与 turn id。
func _end_turn_locally() -> void:
	if _transient_host != null:
		_transient_host.discard_keyed("waiting")
		_transient_host.discard_keyed("command")
	if undo_manager != null:
		undo_manager.commit_batch()
	_set_state(AgentState.IDLE)
	if _http_client != null:
		_http_client.current_turn_id = ""
	if state_store != null:
		state_store.set_value("current_turn_id", "")
		state_store.set_value("pending_calls", [])


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
			_show_notice("error", "命令参数必须是 JSON 对象，例如：/rebuild_index {\"incremental\": true}")
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
	_show_notice("user", text)
	_transient_host.show_keyed("command", "正在执行命令 /%s …" % command_name, "system")
	_scroll_to_bottom()
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
	if _transcript_viewport != null:
		_transcript_viewport.suppress_follow()
	_message_context_popup.set_item_disabled(0, rich.get_selected_text() == "")
	_message_context_popup.position = DisplayServer.mouse_get_position()
	_message_context_popup.popup()
	rich.accept_event()


func _on_message_context_action(id: int) -> void:
	if _transcript_viewport != null:
		_transcript_viewport.suppress_follow()
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
		3:
			# 规范复制：取该条目持久化的完整规范文本，与选区/截断无关。
			if _message_context_source != null and is_instance_valid(_message_context_source) and _transcript_renderer != null:
				var canonical: String = _transcript_renderer.copy_text_for_node(_message_context_source)
				if canonical != "":
					DisplayServer.clipboard_set(canonical)


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
			_show_notice("system", _format_extensions_report({
				"skills": response.get("skills", []),
				"warnings": response.get("warnings", [])
			}))
		else:
			_show_notice("system", _format_doctor_report(response))
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
		_show_notice("system", _format_memory_report(response))
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
			_show_notice("system", _format_plain_value("数据", value))
		return

	if response.has("ok") and response.has("text"):
		if _transient_host != null:
			_transient_host.discard_keyed("command")
		var command_text := _format_command_response(response)
		_show_notice("system" if bool(response.get("ok", false)) else "error", command_text)
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
			_show_notice("system", JSON.stringify(response, "\t"))


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
		_show_notice("error", "当前任务尚未结束，暂时不能运行其他命令。")
		return
	_auto_scroll = true
	_force_scroll_once = true
	_show_notice("user", "/%s %s" % [command_name, JSON.stringify(args)])
	_transient_host.show_keyed("command", "正在执行命令 /%s …" % command_name, "system")
	_scroll_to_bottom()
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

	# 可见记录全部由展示稿条目（tool_activity/approval）表达；这里只负责执行与
	# 确认交互。workflow 工具的 diff 预览必须在执行前渲染（执行后磁盘上的文件
	# 已变成 after_text），登记给展示稿渲染器，在对应条目补丁到达时复用。
	if state_store != null:
		state_store.set_value("current_turn_id", _http_client.current_turn_id)
		state_store.set_value("pending_calls", confirm)

	var results: Array = []
	for call in silent:
		if call is Dictionary:
			if _interrupted_locally:
				return
			var is_workflow := EventFormatter.is_workflow_tool(str(call.get("name", "")))
			if is_workflow:
				var preview: Control = ToolPreviewRenderer.render_call(call, _theme_colors)
				var stats := ToolPreviewRenderer.diff_stats(call)
				_transcript_renderer.register_preview(str(call.get("id", "")), preview, stats)
			_set_state(AgentState.EXECUTING)
			var raw_result: Variant = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			var result := AgentDTO.normalize_execution_result(call, raw_result)
			results.append(result)

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
		_show_notice("system", _ui("rejected_turn_ended"))
		return
	_set_state(AgentState.WAITING_LLM)
	_http_client.send_tool_results(results, _request_model())


## HTTP final 优先作为展示稿补丁的兜底，避免水合/投影异常使整轮正文不可见。
func _handle_final(response: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Received final response (confirmation-only).", {
		"chars": str(response.get("text", "")).length(),
		"assistant_completed_by_patch": _assistant_completed_this_turn
	})
	var text := str(response.get("text", ""))
	if text.strip_edges().is_empty():
		# 空 final 仅作确认处理（任务 4.7）：新契约下服务端不会把空正文当作成功
		# 完成下发。若本轮已接受有效的助手完成补丁，直接终结本轮；否则不等待
		# 超时，立即重新水合对账。
		if _assistant_completed_this_turn and _projector.is_ready():
			FrontendLogger.debug(editor_interface, "ChatPanel", "[handle_final] EMPTY final is terminal: completion patch already accepted", {})
			_auto_scroll = true
			_force_scroll_once = true
			_do_scroll_to_bottom()
			_post_final_scroll_frames = 10
			_end_turn_locally()
		else:
			FrontendLogger.warn(editor_interface, "ChatPanel", "[handle_final] EMPTY final without accepted completion; re-hydrating", {})
			_end_turn_locally()
			_begin_hydration(_current_session_id())
			_http_client.fetch_session_history()
		return
	# 完成补丁未到达或投影器未就绪时，先显示 HTTP final 兜底；随后在本轮
	# 已结束的下一帧再水合，避免 history 响应因仍处于 WAITING_LLM 被忽略。
	var projector_ready: bool = _projector.is_ready()
	var needs_resync: bool = (not _assistant_completed_this_turn) or (not projector_ready)
	if needs_resync and _state == AgentState.WAITING_LLM:
		FrontendLogger.warn(editor_interface, "ChatPanel", "Final confirmation arrived without an accepted completion; re-hydrating.", {
			"assistant_completed_by_patch": _assistant_completed_this_turn,
			"projector_ready": projector_ready
		})
		_show_notice("report", text)
	_auto_scroll = true
	_force_scroll_once = true
	_do_scroll_to_bottom()
	_post_final_scroll_frames = 10
	_end_turn_locally()
	if needs_resync:
		call_deferred("_hydrate_after_final")


## 在轮次状态已复位后水合权威展示稿，避免活跃轮次的 history 响应被丢弃。
func _hydrate_after_final() -> void:
	_begin_hydration(_current_session_id())
	_request_hydration_history(true)


func _handle_session_history(response: Dictionary) -> void:
	if _state != AgentState.IDLE:
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored session history while a turn is active.", {
			"state": _status.text
		})
		return
	# 水合顺序：先由 Projector 校验（session_id + generation）并原子替换 Store，
	# 再从 Store 渲染，最后才以快照游标订阅 WebSocket——订阅不得早于替换完成。
	var generation := _hydration_generation
	if _projector.is_ready():
		var transcript: Dictionary = response.get("transcript", {}) if response.get("transcript", {}) is Dictionary else {}
		var requested_cursor: int = _transcript_store.next_before_ordinal
		var merged: bool = _projector.apply_older_page(response, generation)
		_transcript_viewport.complete_older_page(requested_cursor, merged)
		if merged:
			_transcript_viewport.merge_older_page()
		return
	if not _projector.apply_snapshot(response, generation):
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored stale session history snapshot.", {
			"response_session": str(response.get("session_id", "")),
			"current": _current_session_id(),
			"generation": generation
		})
		return
	var session_id := str(response.get("session_id", ""))
	var entry_count: int = _transcript_store.entry_count()
	FrontendLogger.info(editor_interface, "ChatPanel", "Restoring session transcript.", {
		"session_id": session_id,
		"entries": entry_count,
		"upto_event_seq": _transcript_store.upto_event_seq,
		"legacy": _transcript_store.legacy
	})
	_clear_messages()
	_transcript_viewport.replace_from_store()
	_advance_rendered_watermark()
	_notify_visible_progress()
	_recovery.finish("snapshot_hydrated")
	_update_context_usage_status(
		int(response.get("context_used_tokens", 0)),
		int(response.get("context_token_limit", 0))
	)
	if state_store != null:
		state_store.set_value("session_id", session_id)
		state_store.set_value("last_event_seq", _transcript_store.upto_event_seq)
	var pending_turn_id = response.get("pending_turn_id")
	if pending_turn_id != null:
		_http_client.current_turn_id = str(pending_turn_id)
		if state_store != null:
			state_store.set_value("current_turn_id", _http_client.current_turn_id)
	var saved_auto_scroll := _auto_scroll
	_auto_scroll = false
	if pending_turn_id != null:
		_show_notice("system", _ui("recovered_pending") % [session_id, str(pending_turn_id)])
		_show_pending_results_notice()
		_set_state(AgentState.WAITING_CONFIRM)
	if entry_count == 0 and _message_list.get_child_count() == 0:
		_show_notice("system", _ui("switch_session_empty"))
	_auto_scroll = saved_auto_scroll
	_force_scroll_once = true
	_scroll_to_bottom()
	# 替换完成后才订阅：after_seq = 快照游标，保证不重放快照内已表示的补丁。
	if _event_socket != null:
		_event_socket.start(_current_session_id(), _transcript_store.upto_event_seq)


## 按 Store 顺序取出全部条目字典（含未确认的乐观条目）。
func _ordered_store_entries() -> Array:
	var result: Array = []
	for entry_id in _transcript_store.ordered_entry_ids():
		var entry: Dictionary = _transcript_store.get_entry(str(entry_id))
		if not entry.is_empty():
			result.append(entry)
	return result


func _on_error(message: String) -> void:
	FrontendLogger.error(editor_interface, "ChatPanel", "Agent error.", {"message": message})
	if _commands_requested:
		_commands_requested = false
		_commands_btn.disabled = _state != AgentState.IDLE
	if _transient_host != null:
		_transient_host.discard_keyed("waiting")
		_transient_host.discard_keyed("command")
	_show_notice("error", message)
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
	_show_notice("system", _ui("discard_pending"))
	_set_state(AgentState.WAITING_LLM)


## Reset 中断屏障（任务 2.11）：先抑制旧轮次的迟到响应/事件，取消在途与
## 排队的聊天/工具结果请求并请求服务端中断，再重置会话、水合新快照。
## 旧轮次数据由会话/世代/请求世代三重校验拒绝（HTTP 生成号 + Projector
## generation + 会话 id 比对）。
func _on_reset() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Reset requested.", {"state": _status.text})
	_auto_scroll = true
	_interrupted_locally = true
	_idle_recovery_active = false
	_hydrating_for_recovery = false
	_socket_silence_probe_msec = 0
	_event_queue.clear()
	_draining_events = false
	_patch_batcher.clear()
	_projection_window_pending = false
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	_clear_messages()
	if _event_socket != null:
		_event_socket.stop()
	_recovery.reset()
	_http_client.reset_session()
	_begin_hydration(_current_session_id())
	_http_client.fetch_session_history()
	if state_store != null:
		state_store.reset()
	_update_context_usage_status(0, _context_token_limit)
	_set_state(AgentState.IDLE)


func _on_interrupt() -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Interrupt requested.", {"state": _status.text})
	_interrupted_locally = true
	_event_queue.clear()
	_draining_events = false
	_patch_batcher.clear()
	_projection_window_pending = false
	_idle_recovery_active = false
	_hydrating_for_recovery = false
	_recovery.reset()
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _http_client != null:
		_http_client.interrupt_current()
	if state_store != null:
		state_store.set_value("pending_calls", [])
		state_store.set_value("current_turn_id", "")
	_set_state(AgentState.IDLE)
	if _transient_host != null:
		_transient_host.discard_keyed("waiting")
		_transient_host.discard_keyed("command")
	_show_notice("system", _ui("interrupted"))


func _on_new_session() -> void:
	var previous_session_id := _current_session_id()
	_save_session_to_history(previous_session_id)
	var session_id := "session_%d" % int(Time.get_unix_time_from_system())
	FrontendLogger.info(editor_interface, "ChatPanel", "New session requested.", {"session_id": session_id})
	_auto_scroll = true
	_interrupted_locally = false
	_event_queue.clear()
	_draining_events = false
	_patch_batcher.clear()
	_projection_window_pending = false
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", session_id)
	_save_session_to_history(session_id)
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	if _http_client != null:
		_http_client.start_new_session(previous_session_id, session_id)
		_recovery.reset()
		_begin_hydration(session_id)
		_http_client.fetch_session_history()
	if _event_socket != null:
		_event_socket.stop()
	if state_store != null:
		state_store.reset()
		state_store.set_value("session_id", session_id)
	_clear_messages()
	_update_context_usage_status(0, _context_token_limit)
	_set_state(AgentState.IDLE)
	_show_notice("system", _ui("new_session_started") % session_id)


func _on_recovery_accepted(pointer: Dictionary) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Recovery accepted.", {
		"session_id": str(pointer.get("session_id", "")),
		"pending_turn_id": str(pointer.get("pending_turn_id", ""))
	})
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", str(pointer.get("session_id", "default")))
	_clear_messages()   # 清空当前内容，确保历史加载时消息列表为空
	if _event_socket != null:
		_event_socket.stop()
	_http_client.resume_from_pointer(pointer)
	_recovery.reset()
	_begin_hydration(str(pointer.get("session_id", "default")))
	_http_client.fetch_session_history()
	if state_store != null:
		state_store.merge({
			"session_id": str(pointer.get("session_id", "default")),
			"recovery_pointer": pointer,
			"last_event_seq": int(pointer.get("last_event_seq", 0)),
			"current_turn_id": _http_client.current_turn_id
		})


func _on_recovery_rejected() -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Recovery rejected.")
	_http_client.reset_session()
	if state_store != null:
		state_store.set_value("recovery_pointer", null)
	_show_notice("system", _ui("recovery_dismissed"))


func _on_service_started(base_url: String) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Service started signal received.", {"base_url": base_url})
	_fetch_initial_service_data()
	if service != null and not service.is_running():
		_show_notice("system", _ui("service_manual") % [base_url, str(service.token)])


func _on_service_failed(message: String) -> void:
	FrontendLogger.error(editor_interface, "ChatPanel", "Service failed signal received.", {"message": message})
	_show_notice("error", _ui("service_failed") % message)
	if service != null and str(service.token) != "":
		_show_notice("system", _ui("service_manual_full") % [str(service.base_url), str(service.token)])
	if _event_socket != null:
		_event_socket.stop()


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
	_begin_hydration(_current_session_id())
	_http_client.fetch_session_history()
	_http_client.fetch_recovery_pointer()
	_http_client.fetch_output_styles()


func _on_event_received(event: Dictionary) -> void:
	if _http_client != null:
		_http_client.note_event_progress()
		# 恢复窗口内任何新事件都证明活跃轮次仍在推进：立即结束恢复、继续等待。
		_http_client.note_recovery_progress()
	# 中断屏障（任务 2.11）：Reset/Stop 之后旧轮次的迟到事件一律不得进入展示态。
	if _interrupted_locally:
		return
	# 事件按会话关联（任务 2.11）：拒绝旧会话/串会话的事件，防止污染当前转录。
	var event_session := str(event.get("session_id", ""))
	if event_session != "" and event_session != _current_session_id():
		FrontendLogger.debug(editor_interface, "ChatPanel", "Rejected event from another session.", {
			"event_session": event_session,
			"current_session": _current_session_id(),
		})
		return
	if state_store != null and state_store.has_method("add_events"):
		state_store.add_events([event])
	var event_type := str(event.get("type", ""))
	if event_type == "transcript_patch":
		_handle_transcript_patch(event)
		return
	_handle_transport_event(event)
	# 非展示稿事件不产生可渲染条目：处理完成即视为已呈现，可提交（任务 2.12）。
	_commit_event_seq(event)


## 提交事件序号（任务 2.12）：仅在对应修订已被 Store 与视口接受后调用。
func _commit_event_seq(event: Dictionary) -> void:
	if _event_socket != null:
		_event_socket.commit_seq(int(event.get("seq", 0)))


## 应用一条 transcript_patch：只有 Projector 校验通过（READY + session +
## generation + event_id/revision）才改变 Store 与渲染。
##
## 可合并的增长型流式补丁（追加增量/受限预览，或增长中的完整快照）进入按
## 条目暂存的待投影集，由投影窗口统一应用；非流式与终态补丁立即应用，且
## 会清掉同条目已被取代的暂存补丁，避免终态之后回退到流式中间态。
func _handle_transcript_patch(event: Dictionary) -> void:
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	var patch_format := str(payload.get("patch_format", "full"))
	if _is_replaceable_stream_patch(payload, patch_format):
		var entry_id := str(payload.get("entry_id", ""))
		if entry_id == "":
			var entry_value: Variant = payload.get("entry", {})
			if entry_value is Dictionary:
				entry_id = str((entry_value as Dictionary).get("entry_id", ""))
		if entry_id != "":
			_patch_batcher.enqueue(entry_id, event)
			if _patch_batcher.overflowed:
				# 单条目暂存异常堆积（正常流下增量会就地合并）：保守重同步。
				_patch_batcher.clear()
				_projector.request_resync("pending_overflow")
				return
			_schedule_projection_window()
			return
	# 非流式/终态补丁立即应用（不延迟超过一个帧边界），并丢弃同条目仍在
	# 暂存的中间修订，防止终态之后被迟到的流式修订回退。
	var immediate: Dictionary = _apply_transcript_patch_now(event)
	if not immediate.is_empty():
		_patch_batcher.discard(str(immediate.get("entry_id", "")))
		var presented: bool = _transcript_viewport.apply_entry(immediate)
		if presented:
			# 已渲染水位只在视口真正接受后推进（任务 2.8）。
			_advance_rendered_watermark()
			_notify_visible_progress()
			_scroll_to_bottom()
			# 提交判定（任务 2.12）：仅当 Store 与视口都接受才提交。
			_commit_event_seq(event)
		# 视口/渲染器拒绝绝不提交：renderer_rejected 已路由恢复，
		# 未提交序号由从提交游标的续传重放弥补。
		return
	# Store 未改变展示态：若该事件已被 Store 处理过（重复投递/修订未前进的
	# 无害去重），可见状态已一致，可提交以推进连续游标；投影器早期拒绝
	# （未见过的事件）绝不提交（任务 2.12）。
	if _transcript_store.has_seen_event(str(event.get("event_id", ""))):
		_commit_event_seq(event)


## 判断补丁是否为可合并的增长型流式修订（正文仍在增长的 Thought/assistant）。
func _is_replaceable_stream_patch(payload: Dictionary, patch_format: String) -> bool:
	if patch_format == "append_delta" or patch_format == "preview":
		return true
	if patch_format != "full":
		return false
	var entry_value: Variant = payload.get("entry", {})
	if not (entry_value is Dictionary):
		return false
	var entry: Dictionary = entry_value
	var kind := str(entry.get("kind", ""))
	var state := str(entry.get("state", ""))
	return (kind == "assistant" and state == "streaming") or (kind == "thought" and state == "thinking")


## 立即应用一条补丁（终态/结构态路径，或投影窗口到点后逐条应用）。
## 返回被投影器接受并改变 Store 的条目字典；未改变时返回空字典。
func _apply_transcript_patch_now(event: Dictionary) -> Dictionary:
	if not _projector.apply_event(event, _hydration_generation):
		return {}
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	var entry_id := ""
	var entry_value: Variant = payload.get("entry", {})
	if entry_value is Dictionary:
		entry_id = str((entry_value as Dictionary).get("entry_id", ""))
	if entry_id == "":
		entry_id = str(payload.get("entry_id", ""))
	var stored: Dictionary = _transcript_store.get_entry(entry_id)
	if stored.is_empty():
		return {}
	var entry_payload: Dictionary = stored.get("payload", {}) if stored.get("payload", {}) is Dictionary else {}
	# 服务端用户条目到达：移除同 client_message_id 的乐观节点，由权威条目接管。
	var client_message_id := str(entry_payload.get("client_message_id", ""))
	if client_message_id != "":
		_transcript_renderer.forget_entry("optimistic:" + client_message_id)
	if str(stored.get("kind", "")) == "assistant" and str(stored.get("state", "")) == "complete":
		_assistant_completed_this_turn = true
	if str(stored.get("kind", "")) == "tool_activity" and str(stored.get("state", "")) == "resolved":
		_remember_server_file_read_from_entry(entry_payload)
	return stored


## 到点应用暂存的可合并补丁：每条目只投影最新修订，视口批量更新，
## 整个窗口最多触发一次自动滚动（任务 3.3）。
func _schedule_projection_window() -> void:
	_projection_window_pending = true


func _run_projection_window() -> void:
	_projection_window_pending = false
	_last_projection_window_usec = Time.get_ticks_usec()
	if _patch_batcher.is_empty():
		return
	var window_start_usec := Time.get_ticks_usec()
	var batches: Array = _patch_batcher.take(PROJECTION_WINDOW_MAX_ENTRIES)
	var changed: Array = []
	## 待提交序号（任务 2.12）：只有视口整批接受后才真正提交。
	var commit_seqs: Array = []
	var applied_events := 0
	for batch_value in batches:
		if not (batch_value is Dictionary):
			continue
		var batch: Dictionary = batch_value
		var events_value: Variant = batch.get("events", [])
		if not (events_value is Array):
			continue
		var last_stored: Dictionary = {}
		# 严格按到达顺序应用同一条目的修订：首个完整补丁先建立条目，
		# 其后的增量/预览才能找到依赖的基础修订。
		for event_value in events_value:
			if not (event_value is Dictionary):
				continue
			applied_events += 1
			var event: Dictionary = event_value
			var stored: Dictionary = _apply_transcript_patch_now(event)
			if not stored.is_empty():
				last_stored = stored
				commit_seqs.append(int(event.get("seq", 0)))
			elif _transcript_store.has_seen_event(str(event.get("event_id", ""))):
				# 被 Store 去重/修订未前进：该事件此前已呈现，提交以保持
				# 提交游标连续（任务 2.12）；投影器拒绝的绝不提交。
				commit_seqs.append(int(event.get("seq", 0)))
		if not last_stored.is_empty():
			changed.append(last_stored)
	if not changed.is_empty():
		var accepted_ids: Array = _transcript_viewport.apply_batch(changed)
		_advance_rendered_watermark()
		_notify_visible_progress()
		_scroll_to_bottom()
		if accepted_ids.size() == changed.size():
			for seq_value in commit_seqs:
				_commit_event_by_seq(int(seq_value))
		# 有条目被视口拒绝时不提交：renderer_rejected 已路由恢复（任务 2.2），
		# 水合会重建全部游标；未提交序号由从提交游标的重放弥补。
	elif not commit_seqs.is_empty():
		# 整批均为去重/未前进补丁：没有新的呈现内容需要视口确认，直接提交。
		for seq_value in commit_seqs:
			_commit_event_by_seq(int(seq_value))
	if not _patch_batcher.is_empty():
		# 超出单窗口条目上限的部分顺延到下一个窗口。
		_projection_window_pending = true
	FrontendLogger.debug(editor_interface, "ChatPanel", "Projection window applied.", {
		"entries": batches.size(),
		"events": applied_events,
		"changed": changed.size(),
		"committed": commit_seqs.size(),
		"duration_ms": float(Time.get_ticks_usec() - window_start_usec) / 1000.0,
	})


## 按序号提交（窗口批处理路径不持有事件字典时使用）。
func _commit_event_by_seq(seq: int) -> void:
	if _event_socket != null:
		_event_socket.commit_seq(seq)


## 已读文件缓存钩子：由 resolved 的 read_file/read_script 条目驱动。
func _remember_server_file_read_from_entry(payload: Dictionary) -> void:
	var tool_name := str(payload.get("tool", ""))
	if tool_name != "read_file" and tool_name != "read_script":
		return
	if bool(payload.get("is_error", false)):
		return
	var path := ""
	var summary_value = payload.get("result_summary")
	if summary_value is Dictionary:
		path = str(summary_value.get("path", ""))
	if path == "":
		var args_value = payload.get("args")
		if args_value is Dictionary:
			path = str(args_value.get("path", ""))
	if path != "" and _tool_executor != null:
		_tool_executor.remember_server_file_read(path)


## 非展示稿的传输事件：只驱动状态栏/压缩状态，不产生聊天记录。
func _handle_transport_event(event: Dictionary) -> void:
	var event_type := str(event.get("type", ""))
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	match event_type:
		"agent_model_selected":
			_active_model_name = str(payload.get("model", "")).strip_edges()
			_refresh_status_text()
		"agent_model_fallback":
			_active_model_name = str(payload.get("fallback_model", "")).strip_edges()
			_refresh_status_text()
		"context_usage":
			_update_context_usage_status(
				int(payload.get("used_tokens", 0)),
				int(payload.get("token_limit", 0))
			)
		"compact_started":
			# `compact_started`/`compact_boundary` 成对到达；压缩窗口内状态栏显示
			# "正在压缩"，结束后还原压缩前状态，而不是固定回到 IDLE。
			if _state != AgentState.IDLE:
				_state_before_compact = _state
				_set_state(AgentState.COMPACTING)
		"compact_boundary":
			if _state == AgentState.COMPACTING:
				_set_state(_state_before_compact)
		"turn_keepalive":
			# 活跃轮次进展信号（任务 4.1）：只说明服务端仍持有活跃轮次，
			# 与传输心跳语义不同；不产生聊天记录，仅驱动空闲续期/恢复判定。
			FrontendLogger.debug(editor_interface, "ChatPanel", "Turn keepalive received.", {
				"last_event_seq": int(payload.get("last_event_seq", 0)),
				"phase": str(payload.get("phase", ""))
			})
		_:
			FrontendLogger.debug(editor_interface, "ChatPanel", "Transport event ignored for display.", {
				"type": event_type
			})


func _on_event_history_gap(details: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "WebSocket history gap; routing to recovery.", details)
	# 服务端类型化缺口信号：保留窗口无法闭合，直接走权威快照（任务 2.4）。
	_route_recovery(str(details.get("reason", "retention_gap")), "hydrate", details)


## 视口接近已加载历史的起点时请求一个去重的旧页。
func _on_older_page_requested(before_ordinal: int) -> void:
	if _http_client != null:
		_http_client.fetch_older_session_history(before_ordinal)


## 仅记录脱敏导航计数与状态，避免日志写入完整 transcript 文本。
func _on_viewport_diagnostics(values: Dictionary) -> void:
	FrontendLogger.debug(editor_interface, "TranscriptViewport", "Navigation diagnostics.", values)


func _on_viewport_follow_mode_changed(enabled: bool) -> void:
	if _return_to_latest_btn != null:
		_return_to_latest_btn.visible = not enabled


func _on_return_to_latest() -> void:
	if _transcript_viewport != null:
		_transcript_viewport.return_to_latest()


## 投影器只提供脱敏字段；切勿把 payload/正文写入前端日志。
func _on_transcript_patch_rejected(diagnostic: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "TranscriptViewport", "Transcript patch rejected.", diagnostic)


func _on_transcript_renderer_rejected(diagnostic: Dictionary) -> void:
	var entry_id := str(diagnostic.get("entry_id", ""))
	# 渲染路由失败也是可恢复的同步失败（任务 2.2）：脱敏诊断 + 统一恢复入口。
	var redacted := {
		"reason": "renderer_rejection",
		"entry_id": entry_id,
		"renderer_reason": str(diagnostic.get("renderer_reason", "")),
		"generation": _hydration_generation,
	}
	FrontendLogger.warn(editor_interface, "ChatPanel", "Transcript render failed; routing to recovery.", redacted)
	_route_recovery("renderer_rejection", "hydrate", redacted)


func _on_event_resync_required(details: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "WebSocket resync required; routing to recovery.", details)
	# 背压重同步：订阅缓冲已丢弃未发送事件，直接走权威快照（任务 2.4）。
	_route_recovery("resync_required", "hydrate", details)


## 请求一次历史水合。活跃轮次中主请求队列被 /chat 占用、且 `_handle_session_history`
## 会拒绝非 IDLE 状态下的历史响应，因此改走旁路探针通道并在探针响应中完成水合，
## 避免投影器在整个轮次内卡在 HYDRATING（任务 3.1 / 4.2）。
##
## `full_snapshot` 为真时请求 `limit=0` 的完整快照：恢复路径必须原子包含所有
## 已持久化可见条目（任务 1.4），不受分页裁剪影响。
func _request_hydration_history(full_snapshot := false) -> void:
	if _http_client == null:
		return
	var limit := 0 if full_snapshot else 200
	if _state != AgentState.IDLE or _http_client.is_idle_recovery_active():
		_hydrating_for_recovery = true
		_http_client.probe_get("/sessions/%s/history?limit=%d" % [_current_session_id().uri_encode(), limit])
		return
	_http_client.fetch_session_history(limit)


## 空闲恢复开始（任务 4.2）：从已确认游标重连事件流，并用探针确认活跃轮次。
func _on_idle_recovery_requested(details: Dictionary) -> void:
	_idle_recovery_active = true
	FrontendLogger.warn(editor_interface, "ChatPanel", "Idle recovery requested.", {
		"turn_id": str(details.get("turn_id", "")),
		"window_s": float(details.get("window_s", 0.0)),
		"socket_seq": _transcript_store.upto_event_seq
	})
	if _event_socket != null:
		_event_socket.recover_from_acknowledged_cursor()
	# 主队列被 /chat 占用，活跃轮次确认走旁路探针。
	if _http_client != null:
		_http_client.probe_get("/recovery-pointer")


## 空闲恢复结束：失败时向用户明确展示原因（任务 4.3）。
func _on_idle_recovery_finished(result: Dictionary) -> void:
	_idle_recovery_active = false
	_hydrating_for_recovery = false
	var success := bool(result.get("success", false))
	var detail := str(result.get("detail", ""))
	if success:
		FrontendLogger.info(editor_interface, "ChatPanel", "Idle recovery restored event flow.", {"detail": detail})
		return
	FrontendLogger.error(editor_interface, "ChatPanel", "Idle recovery failed; turn interrupted.", {"detail": detail})
	_show_notice("error", _ui("idle_recovery_failed") % detail)


## 恢复探针响应：恢复指针确认、静默连接探针与恢复期历史水合（任务 4.2 / 2.10）。
func _on_probe_response(response: Dictionary) -> void:
	if response.has("exists"):
		if _socket_silence_probe_msec > 0:
			_handle_silence_probe_response(response)
			return
		if not _idle_recovery_active and not _hydrating_for_recovery:
			return
		# 活跃轮次确认：恢复指针存在即说明服务端仍持有 pending turn。
		if bool(response.get("exists", false)):
			if _http_client != null:
				_http_client.finish_idle_recovery(true, "active_turn_confirmed")
		return
	if not _idle_recovery_active and not _hydrating_for_recovery:
		return
	if response.has("transcript"):
		# 恢复窗口内的历史水合：允许在活跃轮次中替换 Store。
		_apply_recovery_history(response)
		return


## 静默连接探针结果（任务 2.10）：服务端活跃且游标领先 → 从已提交游标续传；
## 活跃但游标未领先 → 仅重连订阅；无活跃轮次 → 交给 HTTP 空闲超时机制。
## 全程只触发只读探测与订阅重连，绝不重放命令。
func _handle_silence_probe_response(response: Dictionary) -> void:
	_socket_silence_probe_msec = 0
	var pointer_value: Variant = response.get("pointer", {})
	var pointer: Dictionary = pointer_value if pointer_value is Dictionary else {}
	var server_seq := int(pointer.get("last_event_seq", 0))
	var committed: int = _event_socket.committed_seq() if _event_socket != null else 0
	var details := transcript_watermarks()
	details["probe_last_event_seq"] = server_seq
	if bool(response.get("exists", false)):
		if server_seq > committed:
			FrontendLogger.warn(editor_interface, "ChatPanel", "Silent socket: server ahead of committed cursor; recovering.", details)
			_route_recovery("socket_silent_server_ahead", "resume", details)
		else:
			FrontendLogger.warn(editor_interface, "ChatPanel", "Silent socket: active turn confirmed; resubscribing.", details)
			if _event_socket != null:
				_event_socket.recover_from_acknowledged_cursor()
	else:
		FrontendLogger.warn(editor_interface, "ChatPanel", "Silent socket: no active turn on server; deferring to idle timeout.", details)


## 恢复期历史水合：应用快照并从快照游标重新订阅（任务 4.2）。
func _apply_recovery_history(response: Dictionary) -> void:
	var generation := _hydration_generation
	if not _projector.apply_snapshot(response, generation):
		FrontendLogger.info(editor_interface, "ChatPanel", "Ignored stale recovery snapshot.", {})
		return
	_hydrating_for_recovery = false
	_clear_messages()
	_transcript_viewport.replace_from_store()
	_advance_rendered_watermark()
	_notify_visible_progress()
	_recovery.finish("snapshot_hydrated")
	FrontendLogger.info(editor_interface, "ChatPanel", "Recovery hydration applied.", {
		"entries": _transcript_store.entry_count(),
		"upto_event_seq": _transcript_store.upto_event_seq
	})
	if _event_socket != null:
		_event_socket.start(_current_session_id(), _transcript_store.upto_event_seq)
	if _http_client != null:
		_http_client.finish_idle_recovery(true, "hydrated_after_gap")


## 投影器检测到修订/表示缺口时请求重新水合（任务 3.1）。
func _on_projector_hydration_required(reason: String) -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Projector requested re-hydration.", {"reason": reason})
	# 修订/表示缺口无法靠重放同一条补丁闭合：直接权威快照（任务 2.4）。
	_route_recovery("projector_" + reason, "hydrate", {"projector_reason": reason})


func _on_event_protocol_error(details: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "WebSocket protocol error.", details)


## 客户端检测到序列缺口（任务 2.2）：先按连续游标续传；续传无法闭合时
## 由恢复状态机自动升级为权威快照水合（任务 2.4）。
func _on_event_sequence_gap(details: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Transcript sequence gap detected.", details)
	_route_recovery("sequence_gap", "resume", details)


## 客户端在解析前拒收了超尺寸报文（任务 5.3）：未提交游标不受影响，先按
## 已提交游标续传；重放仍无法闭合时由状态机升级为权威快照水合——快照条目
## 已在服务端净化，不会再次携带超尺寸载荷。
func _on_event_oversized_packet(details: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Oversized WebSocket packet rejected before parsing.", details)
	_route_recovery("oversized_packet", "resume", details)


## 统一恢复路由（任务 2.4）：所有可见转录断档入口都收敛到这里。
##
## 恢复只触发订阅重连与只读历史水合，绝不重新提交聊天、审批或中断命令
## （任务 2.5）；快照由 Projector 按 session_id + generation 校验，旧会话
## 或旧世代响应无法覆盖当前活跃转录。
func _route_recovery(trigger: String, preferred: String, details: Dictionary) -> void:
	var action: String = _recovery.begin(trigger, preferred, transcript_watermarks())
	match action:
		"resume":
			FrontendLogger.info(editor_interface, "ChatPanel", "Recovery: resuming from committed cursor.", {"trigger": trigger})
			# 未投影暂存会由重放从已提交游标重新送达：先清空，避免重放的
			# 增量与旧暂存重复合并（任务 2.12）。
			_patch_batcher.clear()
			_projection_window_pending = false
			if _event_socket != null:
				_event_socket.recover_from_acknowledged_cursor()
		"hydrate":
			FrontendLogger.info(editor_interface, "ChatPanel", "Recovery: hydrating authoritative snapshot.", {"trigger": trigger})
			_begin_hydration(_current_session_id())
			_request_hydration_history(true)
		_:
			# 已在恢复中或预算耗尽：保持当前动作，不叠加新的恢复。
			pass


## 可见停滞看门狗（任务 2.3 / 2.8）：服务端可见水位领先于客户端
## 接收/提交/投影/渲染四级水位的最小值，且活跃轮次超过停滞阈值没有任何
## 可见进度时触发有界恢复。接收水位只证明传输层到达；心跳与非可见的传输
## 流量不算进度，避免把工具执行等待误判为健康。
func _check_visible_stall() -> void:
	if _recovery == null:
		return
	# 水合超时兜底（任务 2.4）：探针/历史请求失败或响应迟到时不允许永久
	# 停在 HYDRATING；在预算内重试快照请求，预算耗尽则留下脱敏诊断。
	if _recovery.state == TranscriptRecovery.State.HYDRATING:
		var hydrate_timeout_ms := int(_recovery.stall_threshold_s * 1000.0)
		if _recovery.time_in_state_msec(Time.get_ticks_msec()) >= hydrate_timeout_ms:
			if _recovery.retry_hydration("hydration_timeout", transcript_watermarks()) == "hydrate":
				FrontendLogger.warn(editor_interface, "ChatPanel", "Recovery: retrying snapshot hydration.", transcript_watermarks())
				_request_hydration_history(true)
		return
	if _recovery.state != TranscriptRecovery.State.IDLE:
		return
	if _state == AgentState.IDLE:
		return
	if _event_socket == null:
		return
	var now_msec := Time.get_ticks_msec()
	_check_projection_backlog()
	if _recovery.state != TranscriptRecovery.State.IDLE:
		# 投影积压已触发恢复：本帧不再叠加其他触发，交由状态机推进。
		return
	_check_socket_freshness(now_msec)
	var progress: Dictionary = _event_socket.server_progress()
	_recovery.notify_server_progress(int(progress.get("visible_seq", 0)))
	# 以最慢的可见水位判定同步健康（任务 2.8）：
	# min(received, committed, projected, rendered)。
	var received: int = _event_socket.highest_contiguous_seq()
	var committed: int = _event_socket.committed_seq()
	var projected: int = _transcript_store.upto_event_seq if _transcript_store != null else 0
	var rendered: int = _rendered_upto_event_seq
	var floor_seq := mini(mini(received, committed), mini(projected, rendered))
	if not _recovery.server_ahead_of(floor_seq):
		return
	if not _recovery.stalled(now_msec):
		return
	var watermarks := transcript_watermarks()
	FrontendLogger.warn(
		editor_interface, "ChatPanel",
		"Visible transcript stall detected (lagging stage: %s); starting bounded recovery." % str(watermarks.get("lagging_stage", "")),
		watermarks
	)
	_route_recovery("visible_stall", "resume", watermarks)


## 投影积压看门狗（任务 2.9）：暂存队首补丁等待超过停滞阈值仍未被投影窗口
## 应用时，路由到既有恢复状态机，绝不让已接收的补丁无限期不可见。
func _check_projection_backlog() -> void:
	if _patch_batcher.is_empty():
		return
	var oldest_usec: int = _patch_batcher.oldest_pending_usec()
	if oldest_usec <= 0:
		return
	var age_msec := float(Time.get_ticks_usec() - oldest_usec) / 1000.0
	if age_msec < _recovery.stall_threshold_s * 1000.0:
		return
	var watermarks := transcript_watermarks()
	FrontendLogger.warn(editor_interface, "ChatPanel", "Projection backlog stall; routing to bounded recovery.", watermarks)
	_route_recovery("projection_backlog", "resume", watermarks)


## 半开订阅新鲜度检测（任务 2.10）：活跃轮次中连接保持 Open 但超过
## 新鲜度窗口没有收到任何心跳/事件时，用旁路探针确认服务端状态；
## 探针表明服务端领先或无法确认时走续传/快照恢复，绝不重放命令。
func _check_socket_freshness(now_msec: int) -> void:
	if not _event_socket.is_subscribed():
		_socket_silence_probe_msec = 0
		return
	var last_packet: int = _event_socket.last_packet_msec()
	if last_packet > 0 and now_msec - last_packet < SOCKET_SILENCE_THRESHOLD_MS:
		# 新报文到达说明连接恢复健康：撤销在途探针标记。
		if _socket_silence_probe_msec > 0 and last_packet > _socket_silence_probe_msec:
			_socket_silence_probe_msec = 0
		return
	if _socket_silence_probe_msec > 0:
		# 探针有界：响应超时后允许下一个静默回合重新探测。
		if now_msec - _socket_silence_probe_msec < SOCKET_PROBE_TIMEOUT_MS:
			return
		_socket_silence_probe_msec = 0
	_socket_silence_probe_msec = now_msec
	FrontendLogger.warn(editor_interface, "ChatPanel", "Socket silent beyond freshness window; probing recovery pointer.", {
		"silent_ms": maxi(0, now_msec - last_packet),
		"watermarks": transcript_watermarks(),
	})
	if _http_client != null:
		_http_client.probe_get("/recovery-pointer")


## 可见进度推进（任务 2.1）：任何被 Store 接受的可见条目都会刷新停滞计时，
## 并在恢复进行中把预算重置（缺口已被闭合）。
func _notify_visible_progress() -> void:
	if _recovery == null:
		return
	var server_seq := -1
	if _event_socket != null:
		server_seq = int(_event_socket.server_progress().get("visible_seq", 0))
	_recovery.notify_visible_progress(server_seq)


## 已渲染水位跟随 projected 水位：视口已从 Store 接受整批条目的渲染
## （任务 2.1）。单个条目渲染失败由 renderer_rejected 走恢复入口。
func _advance_rendered_watermark() -> void:
	if _transcript_store != null:
		_rendered_upto_event_seq = maxi(_rendered_upto_event_seq, _transcript_store.upto_event_seq)


## received/committed/projected/rendered 四级水位与恢复状态（任务 2.1 / 2.8 / 2.12）。
## 仅用于脱敏诊断；调用方不得写入条目正文。
func transcript_watermarks() -> Dictionary:
	var received: int = _event_socket.highest_contiguous_seq() if _event_socket != null else 0
	var committed: int = _event_socket.committed_seq() if _event_socket != null else 0
	var projected: int = _transcript_store.upto_event_seq if _transcript_store != null else 0
	var rendered: int = _rendered_upto_event_seq
	return {
		"received_seq": received,
		"committed_seq": committed,
		"projected_seq": projected,
		"rendered_seq": rendered,
		"visible_floor_seq": mini(mini(received, committed), mini(projected, rendered)),
		"lagging_stage": _lagging_stage(received, committed, projected, rendered),
		"pending_patch_count": _patch_batcher.pending_event_count(),
		"oldest_pending_usec": _patch_batcher.oldest_pending_usec(),
		"recovery_state": _recovery.state_name() if _recovery != null else "idle",
	}


## 水位最低的可见阶段（任务 2.8 脱敏诊断）：同级时报告更上游的阶段——
## 上游水位不前时，下游（如渲染）也不可能推进，瓶颈在最早卡住的一环。
func _lagging_stage(received: int, committed: int, projected: int, rendered: int) -> String:
	var floor_seq := mini(mini(received, committed), mini(projected, rendered))
	if received == floor_seq:
		return "received"
	if committed == floor_seq:
		return "committed"
	if projected == floor_seq:
		return "projected"
	return "rendered"


func _on_recovery_started(action: String, details: Dictionary) -> void:
	var log_details := details.duplicate(true)
	log_details["action"] = action
	FrontendLogger.warn(editor_interface, "ChatPanel", "Transcript recovery started.", log_details)


func _on_recovery_escalated(details: Dictionary) -> void:
	FrontendLogger.warn(editor_interface, "ChatPanel", "Transcript recovery escalated to snapshot hydration.", details)


func _on_recovery_finished(detail: String) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Transcript recovery finished.", {"detail": detail, "watermarks": transcript_watermarks()})


func _on_recovery_exhausted(details: Dictionary) -> void:
	# 有界预算耗尽：保持静默不打扰用户（任务 Why），只留下可定位的脱敏诊断。
	FrontendLogger.error(editor_interface, "ChatPanel", "Transcript recovery budget exhausted.", details)


## 开始一次水合：递增 generation、清空 Store、停止接受实时补丁。
func _begin_hydration(session_id: String) -> int:
	_hydration_generation = _projector.begin_hydration(session_id)
	_assistant_completed_this_turn = false
	_event_queue.clear()
	_draining_events = false
	_patch_batcher.clear()
	_projection_window_pending = false
	# 呈现水位随 Store 清零重置；快照应用后由视口重建重新抬升。
	_rendered_upto_event_seq = 0
	return _hydration_generation


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
		_show_notice("system", _format_extensions_report(payload))


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
		# 确认框里的预览是执行前渲染的，搬给展示稿渲染器，由 approval/tool_activity
		# 条目在补丁到达时复用，避免执行后再从磁盘读 before_text 产生错误 diff。
		var is_workflow := EventFormatter.is_workflow_tool(str(call.get("name", "")))
		var preview := _inline_confirm.take_preview_for(index, is_workflow)
		var stats := _inline_confirm.diff_stats_for(index, is_workflow)
		if preview != null:
			_transcript_renderer.register_preview(str(call.get("id", "")), preview, stats)
		if should_apply:
			if _interrupted_locally:
				return
			_set_state(AgentState.EXECUTING)
			var raw_result: Variant = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			var result := AgentDTO.normalize_execution_result(call, raw_result)
			result["decision_source"] = "execute"
			result["grant_session_allow"] = _inline_confirm.grant_session_allow()
			FrontendLogger.info(editor_interface, "ChatPanel", "Confirmed tool execution completed.", {
				"tool": str(call.get("name", "")),
				"status": str(result.get("status", "error")),
				"decision_source": "execute",
				"error_code": str(result.get("error_code", "")),
			})
			results.append(result)
		else:
			var rejected := AgentDTO.rejected_result(call)
			rejected["decision_source"] = "unselected"
			FrontendLogger.info(editor_interface, "ChatPanel", "Confirmed tool call left unselected.", {
				"tool": str(call.get("name", "")),
				"status": "rejected",
				"decision_source": "unselected",
			})
			results.append(rejected)
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
		rejected["decision_source"] = "explicit_reject"
		FrontendLogger.info(editor_interface, "ChatPanel", "Confirmed tool call explicitly rejected.", {
			"tool": str(call.get("name", "")),
			"status": "rejected",
			"decision_source": "explicit_reject",
		})
		results.append(rejected)
		var is_workflow := EventFormatter.is_workflow_tool(str(call.get("name", "")))
		var preview := _inline_confirm.take_preview_for(index, is_workflow)
		var stats := _inline_confirm.diff_stats_for(index, is_workflow)
		if preview != null:
			_transcript_renderer.register_preview(str(call.get("id", "")), preview, stats)
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



## 面板级瞬时本地提示——不属于权威展示稿，重载/水合后消失；聊天记录只由
## transcript 条目渲染（任务 3.4/4.1）。提示节点由瞬时宿主创建并可被直接丢弃，
## 绝不从快照、WebSocket 或 Viewport remount 重新渲染。
func _show_notice(role: String, text: String) -> void:
	if _transient_host == null:
		return
	var style := "system"
	if role == "user":
		style = "user"
	elif role == "error":
		style = "error"
	elif role == "report":
		style = "report"
	_transient_host.show_notice(text, style)
	_scroll_to_bottom()


## 错误条目声明可重试时的回调：当前没有通用重试通道，提示用户手动重发。
func _on_transcript_retry_requested(entry_id: String) -> void:
	FrontendLogger.info(editor_interface, "ChatPanel", "Retry requested for error entry.", {"entry_id": entry_id})
	_show_notice("system", "该错误暂不支持自动重试，请重新发送消息。")


func _clear_messages() -> void:
	if _transcript_renderer != null:
		_transcript_renderer.clear_all()
	if _transient_host != null:
		_transient_host.clear_all()


func _scroll_to_bottom() -> void:
	_trim_message_list()
	if not _auto_scroll and not _force_scroll_once:
		return
	if _scroll_request_pending:
		return
	_scroll_request_pending = true
	call_deferred("_scroll_to_bottom_deferred")


func _trim_message_list() -> void:
	if _transcript_renderer != null:
		# Viewport 维护 renderer 根的严格窗口上限；瞬时提示不计入其中。
		pass


func _on_scroll_value_changed(value: float) -> void:
	if _suppress_scroll_check:
		return

	var bar := _scroll.get_v_scroll_bar()
	if _transcript_viewport != null:
		_transcript_viewport.notify_scroll(value)
	var scroll_max := bar.max_value - bar.page
	var is_at_bottom := scroll_max <= 0 or value >= scroll_max - 80

	if is_at_bottom:
		_auto_scroll = true
	else:
		# 如果自动滚动已开启且正在流式输出（末尾助手条目仍处 streaming），此次偏移
		# 是内容增长引起的，不要关闭自动滚动，直接同步把滚动条推到底。
		if _auto_scroll and _is_live_streaming():
			_suppress_scroll_check = true
			_scroll.scroll_vertical = 999999
			call_deferred("_reset_scroll_suppress_deferred")
			return
		_auto_scroll = false


## 末尾条目是否仍处于流式中间态（助手正文流或思考中，用于区分"内容增长"与"用户上滑"）。
func _is_live_streaming() -> bool:
	if _transcript_store == null:
		return false
	var ids: Array = _transcript_store.ordered_entry_ids()
	for index in range(ids.size() - 1, -1, -1):
		var entry: Dictionary = _transcript_store.get_entry(str(ids[index]))
		if entry.is_empty():
			continue
		var kind := str(entry.get("kind", ""))
		if kind == "assistant":
			return str(entry.get("state", "")) == "streaming"
		if kind == "thought":
			return str(entry.get("state", "")) == "thinking"
		return false
	return false


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
	_interrupted_locally = false
	_event_queue.clear()
	_draining_events = false
	_patch_batcher.clear()
	_projection_window_pending = false
	_clear_inline_confirmation()
	if undo_manager != null:
		undo_manager.abort_batch()
	ConfigMigrations.set_value(editor_interface, "ai_agent/session_id", session_id)
	if _http_client != null:
		_http_client.switch_to_session(previous_session_id)
	if _event_socket != null:
		_event_socket.stop()
	if state_store != null:
		state_store.reset()
		state_store.set_value("session_id", session_id)
	_clear_messages()
	_update_context_usage_status(0, _context_token_limit)
	_set_state(AgentState.IDLE)
	_begin_hydration(session_id)
	_http_client.fetch_session_history()
	_save_session_to_history(session_id)


func _current_session_id() -> String:
	if editor_interface == null:
		return "default"
	return str(ConfigMigrations.get_value(editor_interface, "ai_agent/session_id"))
