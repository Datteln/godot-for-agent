@tool
extends VBoxContainer

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const AgentHttpClient = preload("res://addons/ai_agent/service/agent_http_client.gd")
const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const ContextCollector = preload("res://addons/ai_agent/context/context_collector.gd")
const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")
const LogEntryRenderer = preload("res://addons/ai_agent/ui/log_entry_renderer.gd")
const MarkdownRenderer = preload("res://addons/ai_agent/ui/markdown_renderer.gd")
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
		"waiting_model": "等待模型响应...",
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
		"recovery_dismissed": "已忽略恢复信息，会话已重置。",
		"service_manual": "AI 服务未自动启动。请连接 %s，令牌：%s",
		"service_failed": "服务启动失败：%s",
		"service_manual_full": "请手动启动服务。Base URL：%s  Token：%s",
		"event_user": "消息已提交%s。",
		"event_with_context": "，包含项目上下文",
		"event_error": "错误：%s",
		"event_reset": "会话已重置。",
		"event_config": "配置已更新（%s）。",
		"event_compact": "已压缩会话历史：%s 个 frame，移除 %s 条消息，保留最近 %s 条，保留待处理：%s。",
		"event_unknown": "事件：%s %s",
		"history_restored": "已恢复上次会话记录：%s 条。",
		"event_delegate": "Task(%s)",
		"event_model_fallback": "主模型不可用，已切换到备用模型：%s -> %s",
		"event_tool_start": "%s(%s)",
		"event_tool_done": "%s 完成",
		"event_tool_done_count": "%s 完成（%d 个结果）",
		"event_tool_failed": "%s 出错",
		"tool_read_lines": "读取了 %s 行",
		"tool_wrote_lines": "已写入 `%s`（%s 行）",
		"tool_run_result": "%s（exit=%s）",
		"tool_items_count": "返回 %s 条结果",
		"tool_done": "完成",
		"tool_done_path": "完成：`%s`",
		"tool_rejected": "已拒绝",
		"tool_error_detail": "出错：%s",
		"tool_unknown_error": "未知错误",
		"rejected_turn_ended": "已拒绝本次工具调用，会话已结束，可以继续发送新消息。"
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
		"waiting_model": "Waiting for model...",
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
		"recovery_dismissed": "Recovery dismissed; session was reset.",
		"service_manual": "AI service was not auto-started. Connect to %s with token: %s",
		"service_failed": "Service failed to start: %s",
		"service_manual_full": "Start the service manually. Base URL: %s  Token: %s",
		"event_user": "Message submitted%s.",
		"event_with_context": " with project context",
		"event_error": "Error: %s",
		"event_reset": "Session was reset.",
		"event_config": "Configuration changed (%s).",
		"event_compact": "Compacted conversation history: %s frame(s), %s message(s) removed, %s recent kept, pending preserved: %s.",
		"event_unknown": "Event: %s %s",
		"history_restored": "Restored previous session history: %s item(s).",
		"event_delegate": "Task(%s)",
		"event_model_fallback": "Primary model unavailable, switched to fallback model: %s -> %s",
		"event_tool_start": "%s(%s)",
		"event_tool_done": "%s done",
		"event_tool_done_count": "%s done (%d result(s))",
		"event_tool_failed": "%s failed",
		"tool_read_lines": "Read %s lines",
		"tool_wrote_lines": "Wrote `%s` (%s lines)",
		"tool_run_result": "%s (exit=%s)",
		"tool_items_count": "Returned %s item(s)",
		"tool_done": "Done",
		"tool_done_path": "Done: `%s`",
		"tool_rejected": "Rejected",
		"tool_error_detail": "Error: %s",
		"tool_unknown_error": "Unknown error",
		"rejected_turn_ended": "Rejected this tool call. The turn has ended; you can send a new message."
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
var _inline_checkboxes: Array[CheckBox] = []
var _inline_previews: Array[Control] = []
var _inline_diff_stats: Array[Dictionary] = []
var _inline_always_allow: CheckBox
var _inline_apply_btn: Button
var _inline_reject_btn: Button
var _inline_confirm_box: Control
var _inline_busy := false
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
var _reasoning_key := ""
var _reasoning_toggle: Button
var _reasoning_detail_rich: RichTextLabel
var _reasoning_text := ""
var _reasoning_started_ms: int = -1
var _rendered_assistant_keys := {}
var _live_response_keys := {}   # 仅追踪本轮实时响应，避免历史加载的指纹误判为重复
var _closed_stream_keys := {}
var _closed_reasoning_keys := {}   # 新增：专门追踪已关闭的 reasoning stream
var _theme_colors: Dictionary = {}
var _auto_scroll := true
var _suppress_scroll_check := false   # 程序滚动时抑制 value_changed 误判
var _post_final_scroll_frames := 0   # final 响应后持续滚动到底部的剩余帧数
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
		_stream_content_rich.clear()
		_stream_content_rich.append_text(MarkdownRenderer.markdown_to_bbcode(_stream_display_text, _theme_colors))
		_stream_text_dirty = false
	if _auto_scroll and _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		_do_scroll_to_bottom()
	# final 响应后连续多帧强制滚动到底部，等待 fit_content RichTextLabel 完成布局
	if _post_final_scroll_frames > 0:
		_post_final_scroll_frames -= 1
		_do_scroll_to_bottom()
	if _reasoning_toggle != null and is_instance_valid(_reasoning_toggle) and _reasoning_started_ms >= 0:
		_reasoning_toggle.text = "✻  " + _format_reasoning_header()
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
	var base := _get_editor_theme_color("base_color", Color(0.14, 0.14, 0.14))
	var font := _get_editor_theme_color("font_color", Color(0.875, 0.875, 0.875))
	var accent := _get_editor_theme_color("accent_color", Color(0.34, 0.62, 1.0))
	var error := _get_editor_theme_color("error_color", Color(0.95, 0.35, 0.35))
	var success := _get_editor_theme_color("success_color", Color(0.35, 0.82, 0.48))
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
		"syntax_comment": _get_editor_setting_color("text_editor/theme/highlighting/comment_color", Color(0.42, 0.72, 0.36) if is_dark else Color(0.25, 0.48, 0.18)),
		"syntax_string": _get_editor_setting_color("text_editor/theme/highlighting/string_color", Color(0.81, 0.57, 0.47) if is_dark else Color(0.62, 0.24, 0.12)),
		"syntax_number": _get_editor_setting_color("text_editor/theme/highlighting/number_color", Color(0.71, 0.81, 0.66) if is_dark else Color(0.48, 0.40, 0.08)),
		"syntax_keyword": _get_editor_setting_color("text_editor/theme/highlighting/keyword_color", Color(0.34, 0.61, 0.84) if is_dark else Color(0.13, 0.36, 0.77)),
	}
	# 原地更新，使 _log_renderer.theme_colors 引用自动同步
	_theme_colors.clear()
	_theme_colors.merge(new_colors)


func _refresh_live_theme_overrides() -> void:
	if _stream_content_rich != null and is_instance_valid(_stream_content_rich):
		_stream_content_rich.add_theme_color_override("default_color", _theme_color("text"))
	if _reasoning_toggle != null and is_instance_valid(_reasoning_toggle):
		_set_button_text_colors(_reasoning_toggle, _theme_color("muted_text"), _theme_color("hover_text"))
	if _reasoning_detail_rich != null and is_instance_valid(_reasoning_detail_rich):
		_reasoning_detail_rich.add_theme_color_override("default_color", _theme_color("muted_text"))


func _get_editor_theme_color(name: String, fallback: Color) -> Color:
	var editor_theme: Theme = null
	if editor_interface != null:
		editor_theme = editor_interface.get_editor_theme()
	if editor_theme != null and editor_theme.has_color(name, "Editor"):
		return editor_theme.get_color(name, "Editor")
	if has_theme_color(name, "Editor"):
		return get_theme_color(name, "Editor")
	return fallback


func _get_editor_setting_color(path: String, fallback: Color) -> Color:
	if editor_interface == null:
		return fallback
	var settings := editor_interface.get_editor_settings()
	if settings == null or not settings.has_setting(path):
		return fallback
	var value = settings.get_setting(path)
	return value if value is Color else fallback


func _fallback_theme_color(key: String) -> Color:
	match key:
		"text":
			return Color(0.875, 0.875, 0.875)
		"muted_text", "subtle_text", "marker_text":
			return Color(0.55, 0.55, 0.55)
		"hover_text":
			return Color(1, 1, 1)
		"accent_text", "marker_action":
			return Color(0.34, 0.62, 1.0)
		"success_text":
			return Color(0.35, 0.82, 0.48)
		"error_text":
			return Color(0.95, 0.35, 0.35)
		"code_bg":
			return Color(0.12, 0.12, 0.12)
		"syntax_comment":
			return Color(0.42, 0.72, 0.36)
		"syntax_string":
			return Color(0.81, 0.57, 0.47)
		"syntax_number":
			return Color(0.71, 0.81, 0.66)
		"syntax_keyword":
			return Color(0.34, 0.61, 0.84)
		"user_panel_bg":
			return Color(0.15, 0.22, 0.27)
		"user_panel_border":
			return Color(0.27, 0.38, 0.44)
		"panel_bg":
			return Color(0.16, 0.16, 0.16)
		"panel_border":
			return Color(0.25, 0.25, 0.25)
		"error_panel_bg":
			return Color(0.23, 0.14, 0.14)
		"error_panel_border":
			return Color(0.50, 0.27, 0.27)
		"panel_alt_bg":
			return Color(0.14, 0.14, 0.14)
		"panel_alt_border":
			return Color(0.36, 0.36, 0.36)
		_:
			return Color(0.16, 0.16, 0.16)


func _theme_color(key: String) -> Color:
	var value = _theme_colors.get(key, _fallback_theme_color(key))
	return value if value is Color else _fallback_theme_color(key)


func _resolve_color(value, fallback_key: String) -> Color:
	if value is Color:
		return value
	if value is String and str(value) != "":
		return Color(str(value))
	return _theme_color(fallback_key)


func _color_tag(color: Color) -> String:
	return "#" + color.to_html(color.a < 1.0)


func _theme_color_tag(key: String) -> String:
	return _color_tag(_theme_color(key))


func _marker_color(marker_text: String) -> Color:
	return _theme_color("marker_action") if marker_text == "●" else _theme_color("marker_text")


func _marker_color_tag(marker_text: String) -> String:
	return _color_tag(_marker_color(marker_text))


func _set_button_text_colors(button: Button, font_color: Color, hover_color: Color) -> void:
	button.add_theme_color_override("font_color", font_color)
	button.add_theme_color_override("font_hover_color", hover_color)


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
		"response": response
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
			_stream_content_rich.append_text(MarkdownRenderer.markdown_to_bbcode(render_text, _theme_colors))
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
		var text := _strip_think_xml(str(item.get("text", ""))).strip_edges()
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
	if not items.is_empty():
		_append_message("system", _ui("history_restored") % str(items.size()))


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
	var session_id := "session_%d" % int(Time.get_unix_time_from_system())
	FrontendLogger.info(editor_interface, "ChatPanel", "New session requested.", {"session_id": session_id})
	_auto_scroll = true
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
	FrontendLogger.debug(editor_interface, "ChatPanel", "Handling events.", {"count": events.size()})
	for event in events:
		if event is Dictionary:
			var event_type := str(event.get("type", "<unknown>"))
			var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
			FrontendLogger.debug(editor_interface, "ChatPanel", "Event", {
				"type": event_type, "payload": payload
			})
			_event_queue.append(event)
	if not _draining_events:
		_drain_event_queue()


func _drain_event_queue() -> void:
	if _event_queue.is_empty():
		_draining_events = false
		return
	_draining_events = true
	if _interrupted_locally:
		_event_queue.clear()
		_draining_events = false
		return
	var event: Dictionary = _event_queue.pop_front()
	var event_type := str(event.get("type", ""))
	if state_store != null:
		state_store.add_event(event)
	if event_type == "agent_reasoning_delta":
		_on_reasoning_delta(event)
	elif event_type == "agent_text_delta":
		_on_text_delta(event)
	elif event_type == "final":
		# SSE event 路径的 final 也需要路由到 _handle_final
		FrontendLogger.debug(editor_interface, "ChatPanel", "[event] -> route: final (via event stream)", {})
		_handle_final(event.get("payload", event))
	else:
		var previous_state := _state
		var is_compacting := event_type == "compact_boundary" and previous_state != AgentState.IDLE
		if is_compacting:
			_set_state(AgentState.COMPACTING)
		var description := EventFormatter.describe_event(event, _ui_table())
		if description != "":
			FrontendLogger.debug(editor_interface, "ChatPanel", "-> rendered", {
				"type": event_type, "description": description
			})
			_append_message("system", description)
		if is_compacting:
			_set_state(previous_state)
	call_deferred("_drain_event_queue")


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
	_clear_inline_confirmation_ui()
	_inline_checkboxes.clear()
	_inline_previews.clear()
	_inline_diff_stats.clear()

	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var panel := _log_renderer.make_panel(_theme_color("panel_alt_bg"), _theme_color("panel_alt_border"))
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
		var preview := ToolPreviewRenderer.render_call(call, _theme_colors)
		preview.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		item.add_child(preview)
		_inline_previews.append(preview)
		# 此时文件还未被改动，是计算 diff 行数统计的唯一正确时机——执行后文件
		# 内容已变成 after_text，"before" 就读不到了。
		_inline_diff_stats.append(ToolPreviewRenderer.diff_stats(call))
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
	# 确保确认面板出现时自动滚动到底部，让用户看到需要操作的内容
	_auto_scroll = true
	_force_scroll_once = true
	_do_scroll_to_bottom()
	# 布局可能需要额外帧才能稳定，用 _process 多帧兜底
	_post_final_scroll_frames = max(_post_final_scroll_frames, 5)


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
		var preview: Control = _inline_previews[index] if index < _inline_previews.size() else null
		var stats: Dictionary = _inline_diff_stats[index] if index < _inline_diff_stats.size() else {}
		if should_apply:
			if _interrupted_locally:
				return
			_set_state(AgentState.EXECUTING)
			var result: Dictionary = await _tool_executor.execute(call)
			if _interrupted_locally:
				return
			result["grant_session_allow"] = _inline_always_allow != null and _inline_always_allow.button_pressed
			results.append(result)
			_append_tool_result(call, result, preview, stats)
		else:
			var rejected := AgentDTO.rejected_result(call)
			results.append(rejected)
			_append_tool_result(call, rejected, preview, stats)
	_set_inline_busy(false)
	_on_decision(results)


func _on_inline_reject() -> void:
	if _inline_busy:
		return
	_set_inline_busy(true)
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
		var preview: Control = _inline_previews[index] if index < _inline_previews.size() else null
		var stats: Dictionary = _inline_diff_stats[index] if index < _inline_diff_stats.size() else {}
		_append_tool_result(call, rejected, preview, stats)
	_set_inline_busy(false)
	_on_decision(results)


## 仅拆除确认框的 UI（旧的 checkbox/diff 预览/按钮），不触碰 `_pending_calls` /
## `_pending_silent_results`。`_show_inline_confirmation` 在构建新一轮确认框
## 前调用它来清掉上一轮遗留的控件——如果改用下面这个会清空 pending 数据的
## 完整版本，就会把调用者刚刚（在它之前一行）写入的 `_pending_calls` 清空，
## 导致确认框显示正常，但用户点"应用"/"拒绝"时已经没有数据可回传。
func _clear_inline_confirmation_ui() -> void:
	if _inline_confirm_box != null and is_instance_valid(_inline_confirm_box):
		_inline_confirm_box.queue_free()
	_inline_confirm_box = null
	_inline_checkboxes.clear()
	_inline_previews.clear()
	_inline_diff_stats.clear()
	_inline_busy = false


func _clear_inline_confirmation() -> void:
	_clear_inline_confirmation_ui()
	_pending_calls.clear()
	_pending_silent_results.clear()


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
	if _reasoning_detail_rich != null and is_instance_valid(_reasoning_detail_rich):
		_reasoning_detail_rich.clear()
		_reasoning_detail_rich.append_text(MarkdownRenderer.markdown_to_bbcode(_reasoning_text, _theme_colors))
	_scroll_to_bottom()


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


func _finish_reasoning_stream() -> void:
	_reasoning_key = ""
	_reasoning_toggle = null
	_reasoning_detail_rich = null
	_reasoning_text = ""
	_reasoning_started_ms = -1


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

	var rich := _log_renderer.make_rich_text(text, color)
	body.add_child(rich)

	_message_list.add_child(row)
	_scroll_to_bottom()


func _message_fingerprint(text: String) -> String:
	return " ".join(text.strip_edges().split())


func _append_log_stream_message(text: String, color = null, mark_text: bool = false) -> void:
	_log_renderer.append_log_stream_message(_message_list, text, color, mark_text, _indent_current_text)
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
	call_deferred("_scroll_to_bottom_deferred")


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
	if not UI_TEXT.has(lang):
		lang = "zh"
	var table: Dictionary = UI_TEXT.get(lang, UI_TEXT["zh"])
	return str(table.get(key, key))


func _ui_table() -> Dictionary:
	var lang := "zh"
	if editor_interface != null:
		lang = str(ConfigMigrations.get_value(editor_interface, "ai_agent/ui_language"))
	if not UI_TEXT.has(lang):
		lang = "zh"
	return UI_TEXT.get(lang, UI_TEXT["zh"])
