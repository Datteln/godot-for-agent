class_name ChatItemRendererRegistry
extends RefCounted

const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")
const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")
const MAX_MESSAGE_RENDER_CHARS := 90000
const MAX_REASONING_RENDER_CHARS := 30000

## Timeline item 到 Godot Control 的唯一节点工厂。

var log_renderer: RefCounted
var theme_colors: Dictionary = {}
var ui_text: Dictionary = {}
var reveal_queue: RefCounted = null

const _STREAM_BLOCK_TYPES := ["markdown", "plain_text", "reasoning"]


func create_item_node(item: Dictionary) -> Control:
	if log_renderer == null:
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	var streaming := is_streaming(item) and reveal_queue != null
	var block_index := 0
	for raw_block in item.get("content_blocks", []):
		if raw_block is Dictionary:
			_render_block(root, item, raw_block, block_index if streaming else -1)
		block_index += 1
	root.set_meta("timeline_item_id", str(item.get("item_id", "")))
	root.set_meta("copy_text", str(item.get("copy_text", "")))
	root.set_meta("timeline_lifecycle", str(item.get("lifecycle", "")))
	root.set_meta("timeline_streaming", streaming)
	root.set_meta("timeline_finalized", false)
	return root


func create_tool_preview_node(call: Dictionary) -> Control:
	return ToolPreviewRenderer.render_call(call, theme_colors)


func refresh_stream_node(node: Control, item: Dictionary) -> void:
	## 流式 item 增量刷新：重填内层 label 内容并推进 visible_characters。
	## 不重建节点，因此折叠/展开状态与滚动位置保持不变。
	if not bool(node.get_meta("timeline_streaming", false)) or reveal_queue == null:
		return
	var item_id := str(item.get("item_id", ""))
	var block_index := 0
	for raw_block in item.get("content_blocks", []):
		if raw_block is Dictionary and str(raw_block.get("type", "")) in _STREAM_BLOCK_TYPES:
			var key := "stream_reveal_%d" % block_index
			var label: Variant = node.get_meta(key, null)
			if label is RichTextLabel:
				var limit := MAX_REASONING_RENDER_CHARS if str(raw_block.get("type", "")) == "reasoning" else MAX_MESSAGE_RENDER_CHARS
				log_renderer.write_rich_text(
					label,
					_limited_text(str(raw_block.get("text", "")), limit),
					str(node.get_meta("stream_marker_%d" % block_index, "")),
				)
				var total := int(label.get_total_character_count())
				reveal_queue.register(item_id, block_index, total)
				label.visible_characters = reveal_queue.shown(item_id, block_index)
			# 流式推进期间不重建节点，reasoning 块的 token 计数 header 需要增量刷新
			if str(raw_block.get("type", "")) == "reasoning":
				var toggle: Variant = node.get_meta("stream_toggle_%d" % block_index, null)
				if toggle is Button:
					toggle.text = _reasoning_header_text(raw_block, item)
		block_index += 1


func drop_stream_reveals(node: Control) -> void:
	## 节点销毁/丢弃时移除队列状态（finalize 就地切换后不再需要）。
	if reveal_queue == null:
		return
	var item_id := str(node.get_meta("timeline_item_id", ""))
	if not item_id.is_empty():
		reveal_queue.drain(item_id)
		node.set_meta("timeline_streaming", false)


func refresh_committed_node(node: Control, item: Dictionary) -> void:
	## finalize 轻量切换：不重建节点，仅把流式 UI 状态切为 committed 形态。
	## 流式期间 label 已是全量文本，只需更新 reasoning 折叠块的 header 并释放
	## reveal 队列；展开状态与滚动位置因此保持不变（不触发重新渲染）。
	var block_index := 0
	for raw_block in item.get("content_blocks", []):
		if raw_block is Dictionary and str(raw_block.get("type", "")) == "reasoning":
			var toggle: Variant = node.get_meta("stream_toggle_%d" % block_index, null)
			if toggle is Button:
				toggle.text = _reasoning_header_text(raw_block, item)
		block_index += 1
	drop_stream_reveals(node)
	node.set_meta("timeline_finalized", true)


func is_streaming(item: Dictionary) -> bool:
	return str(item.get("lifecycle", "")) == "provisional" or str(item.get("status", "")) == "streaming"


func advance_stream_node(node: Control, item: Dictionary) -> void:
	## 逐帧推进某流式节点的可见字符（轻量：只调 visible_characters）。
	if reveal_queue == null or not bool(node.get_meta("timeline_streaming", false)):
		return
	var item_id := str(item.get("item_id", ""))
	var block_index := 0
	for raw_block in item.get("content_blocks", []):
		if raw_block is Dictionary and str(raw_block.get("type", "")) in _STREAM_BLOCK_TYPES:
			var key := "stream_reveal_%d" % block_index
			var label: Variant = node.get_meta(key, null)
			if label is RichTextLabel:
				label.visible_characters = reveal_queue.shown(item_id, block_index)
		block_index += 1


func _render_block(root: VBoxContainer, item: Dictionary, block: Dictionary, reveal_block_index: int) -> void:
	match str(block.get("type", "")):
		"markdown", "plain_text":
			_render_text(root, item, _limited_text(str(block.get("text", "")), MAX_MESSAGE_RENDER_CHARS), reveal_block_index)
		"reasoning":
			_render_reasoning(root, item, block, reveal_block_index)
		"event":
			_render_event(root, item, block)
		"tool":
			_render_tool(root, item, block)
		"code":
			var language := str(block.get("language", ""))
			_render_text(root, item, "```%s\n%s\n```" % [language, str(block.get("text", ""))], reveal_block_index)


func _render_text(root: VBoxContainer, item: Dictionary, text: String, reveal_block_index: int = -1) -> void:
	var role := str(item.get("role", "system"))
	if role == "user":
		var panel: PanelContainer = log_renderer.make_message_panel(role)
		panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		var rich: RichTextLabel = log_renderer.make_rich_text(text)
		panel.add_child(rich)
		root.add_child(panel)
		if reveal_block_index >= 0:
			_register_stream_label(root, item, reveal_block_index, rich, "")
		return
	var color: Variant = null
	if role == "error":
		color = theme_colors.get("error_text", null)
	var marker_text := str(log_renderer.workflow_marker_text("text", str(item.get("kind", "")) in ["system", "tool_call", "tool_result"]))
	var rich: RichTextLabel = log_renderer.make_log_rich_text(text, color, marker_text, str(item.get("style_token", "")) == "indented")
	root.add_child(rich)
	if reveal_block_index >= 0:
		_register_stream_label(root, item, reveal_block_index, rich, marker_text)


func _render_reasoning(root: VBoxContainer, item: Dictionary, block: Dictionary, reveal_block_index: int = -1) -> void:
	var toggle: Button = log_renderer.make_workflow_toggle(
		_reasoning_header_text(block, item),
		theme_colors.get("muted_text", Color(0.6, 0.6, 0.6)),
	)
	var detail_rich: RichTextLabel = log_renderer.append_collapsible(root, toggle, _limited_text(str(block.get("text", "")), MAX_REASONING_RENDER_CHARS), "✻")
	if reveal_block_index >= 0:
		_register_stream_label(root, item, reveal_block_index, detail_rich, "")
		root.set_meta("stream_toggle_%d" % reveal_block_index, toggle)


func _reasoning_header_text(block: Dictionary, item: Dictionary) -> String:
	# 流式中：Thinking · 实时 token 计数；结束时：Thought for 思考耗时
	if str(item.get("lifecycle", "")) == "committed":
		var elapsed_ms := int(block.get("elapsed_ms", 0))
		if elapsed_ms > 0:
			return "● Thought for %.2fs ✓" % (float(elapsed_ms) / 1000.0)
		return "● Thought ✓"
	var token_count := int(block.get("token_count", 0))
	if token_count > 0:
		return "○ Thinking · %d tokens" % token_count
	return "○ Thinking"


func _register_stream_label(root: VBoxContainer, item: Dictionary, block_index: int, rich: RichTextLabel, marker_text: String) -> void:
	## 把流式文本 label 及其重建参数登记到节点 meta，供 refresh_stream_node 使用。
	root.set_meta("stream_reveal_%d" % block_index, rich)
	root.set_meta("stream_marker_%d" % block_index, marker_text)
	if reveal_queue == null:
		return
	var total := int(rich.get_total_character_count())
	reveal_queue.register(str(item.get("item_id", "")), block_index, total)
	rich.visible_characters = reveal_queue.shown(str(item.get("item_id", "")), block_index)


func _render_event(root: VBoxContainer, item: Dictionary, block: Dictionary) -> void:
	var event := {
		"type": str(block.get("event_type", "")),
		"payload": (block.get("payload", {}) as Dictionary).duplicate(true),
	}
	var text := EventFormatter.describe_event(event, ui_text)
	if text.is_empty():
		text = str((block.get("payload", {}) as Dictionary).get("text", ""))
	if text.is_empty():
		return
	_render_text(root, item, text)


func _render_tool(root: VBoxContainer, item: Dictionary, block: Dictionary) -> void:
	var call: Dictionary = block.get("call", {}) if block.get("call", {}) is Dictionary else {}
	var preview := ToolPreviewRenderer.render_call(call, theme_colors, true)
	root.add_child(preview)
	var result: Dictionary = block.get("result", {}) if block.get("result", {}) is Dictionary else {}
	var status := str(result.get("status", item.get("status", "")))
	if not status.is_empty():
		var status_color: Variant = theme_colors.get("text", null)
		if status == "error":
			status_color = theme_colors.get("error_text", null)
		elif status == "applied":
			status_color = theme_colors.get("success_text", null)
		root.add_child(log_renderer.make_log_rich_text(status, status_color))
	if not result.is_empty():
		var result_text := JSON.stringify(result, "\t")
		if result_text.length() > 12000:
			result_text = result_text.left(12000) + "\n... (truncated)"
		var result_toggle: Button = log_renderer.make_workflow_toggle("result · %d chars" % result_text.length(), theme_colors.get("muted_text", Color(0.6, 0.6, 0.6)))
		log_renderer.append_collapsible(root, result_toggle, result_text, "⚙")


func _limited_text(value: String, limit: int) -> String:
	if limit <= 0 or value.length() <= limit:
		return value
	return value.left(limit) + "\n\n... (display truncated)"
