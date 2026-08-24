## 展示稿渲染宿主（TranscriptViewport；任务 1.2 / 决定 2）。
##
## 唯一决定根控件 mount/unmount 的宿主：以 `entry_id → Control` 索引当前已挂载
## 根控件，按条目 `kind` 经注册表选择 renderer；较新 revision 调用该控件的
## `update` 原地更新，不追加第二个根控件；不高于已接受 revision 的更新保持
## 控件不变。卸载/复用时调用 `reset` 断开回调并清空选择/展开/可操作状态。
##
## 渲染器不读取原始 HTTP/WebSocket payload，不解析文本前缀，不做任何基于显示
## 文本的去重；条目身份与状态完全来自 Store 里的 typed entry。缺少 `kind` 的
## 输入一律拒绝渲染。
@tool
extends RefCounted

const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")
const TranscriptRendererRegistry = preload("res://addons/ai_agent/transcript/transcript_renderer_registry.gd")
const TranscriptRenderContext = preload("res://addons/ai_agent/transcript/transcript_render_context.gd")

## 错误条目声明可重试时的重试请求（宿主转发给面板）。
signal error_retry_requested(entry_id: String)

var theme_colors: Dictionary = {}:
	set(value):
		theme_colors = value
		if _ctx != null:
			_ctx.theme_colors = value

## UI 文案函数：(key: String) -> String。
var ui_text: Callable:
	set(value):
		ui_text = value
		if _ctx != null:
			_ctx.ui_text = value

## 节点工厂（LogEntryRenderer）。
var log_renderer: RefCounted:
	set(value):
		log_renderer = value
		if _ctx != null:
			_ctx.node_factory = value

## 单条初始展示字符预算（所有长内容 kind 统一；完整内容始终保留在 Store）。
var display_budget_chars: int = TranscriptRenderContext.DEFAULT_DISPLAY_BUDGET_CHARS:
	set(value):
		display_budget_chars = value
		if _ctx != null:
			_ctx.display_budget_chars = value

var _message_list: VBoxContainer
var _registry: RefCounted
var _ctx: RefCounted
## entry_id -> {root: Control, revision: int, entry: Dictionary}
var _mounted: Dictionary = {}
## tool_call_id -> {preview: Control, stats: Dictionary}。
## 工具执行前预渲染的 diff 预览在此登记；条目创建/更新时消费复用，避免在
## 文件已被改写后再从磁盘读取 before_text 造成错误 diff。
var _preview_cache: Dictionary = {}
var _last_failure_reason := ""


func _init() -> void:
	_registry = TranscriptRendererRegistry.new()
	_ctx = TranscriptRenderContext.new()
	_ctx.theme_colors = theme_colors
	_ctx.resolve_entry = mounted_entry
	_ctx.retry_entry = func(entry_id: String) -> void:
		error_retry_requested.emit(entry_id)


func attach(message_list: VBoxContainer) -> void:
	_message_list = message_list


## Viewport 是唯一的 durable 条目宿主；它可在热重载或外部 UI 重建后重新确认。
func ensure_mount_container(message_list: VBoxContainer) -> void:
	if _message_list != message_list:
		_message_list = message_list


## 注入自定义复制落地通道（如测试捕获）；默认使用系统剪贴板。
func set_clipboard_sink(sink: Callable) -> void:
	_ctx.clipboard_set = sink


## 登记某个 tool_call_id 的执行前预览（含 diff 统计），供条目渲染复用。
func register_preview(tool_call_id: String, preview: Control, stats: Dictionary) -> void:
	if tool_call_id == "" or preview == null or not is_instance_valid(preview):
		return
	# 只接受已从确认宿主 detach 的 live Control；否则确认框释放后会留下悬垂引用。
	if preview.get_parent() != null:
		return
	var previous: Dictionary = _preview_cache.get(tool_call_id, {})
	var previous_preview: Control = previous.get("preview")
	if previous_preview != null and is_instance_valid(previous_preview):
		previous_preview.queue_free()
	_preview_cache[tool_call_id] = {"preview": preview, "stats": stats}


## 清空全部条目节点（快照替换/会话切换时调用）；展开/预览等视图状态随之清除，
## 重新挂载一律回到默认折叠/预览形态。
func clear_all() -> void:
	for entry_id_value in _mounted.keys():
		var entry_id := str(entry_id_value)
		var root: Control = mounted_root(entry_id)
		if root != null and is_instance_valid(root):
			_registry.reset(root)
			if root.get_parent() != null:
				root.get_parent().remove_child(root)
			root.queue_free()
	_mounted.clear()
	for tool_call_id in _preview_cache.keys():
		var cached: Dictionary = _preview_cache[tool_call_id]
		var preview: Control = cached.get("preview")
		if preview != null and is_instance_valid(preview) and preview.get_parent() == null:
			preview.queue_free()
	_preview_cache.clear()


## 按 Store 当前顺序整体重绘（历史水合/重连后）。
func render_all(ordered_entries: Array) -> void:
	clear_all()
	for entry in ordered_entries:
		if entry is Dictionary:
			apply_entry(entry, false)


## 创建或原地更新单个条目节点；返回是否发生了可见变化。
## 缺少 entry_id 或 kind 的输入一律拒绝（任务 4.2）。
func apply_entry(entry: Dictionary, _scroll_hint := true) -> bool:
	_last_failure_reason = ""
	if _message_list == null:
		_last_failure_reason = "mount_container_missing"
		return false
	var entry_id := str(entry.get("entry_id", ""))
	var kind := str(entry.get("kind", ""))
	if entry_id == "" or kind == "":
		_last_failure_reason = "invalid_entry_identity_or_kind"
		return false
	var revision := int(entry.get("revision", 1))
	if not _mounted.has(entry_id):
		var root: Control = _registry.create(entry, _ctx, _take_extras(entry))
		if root == null:
			_last_failure_reason = "renderer_factory_returned_null:" + kind
			return false
		_mount(entry_id, root, entry)
		return true
	var root: Control = mounted_root(entry_id)
	if root == null:
		_last_failure_reason = "mounted_root_was_freed"
		return apply_entry(entry, _scroll_hint)
	var record: Dictionary = _mounted.get(entry_id, {})
	if revision <= int(record.get("revision", 1)):
		# 不高于已接受修订：控件保持不变。
		return false
	var mounted_state: Dictionary = record.get("entry", {})
	if kind == "thought" \
			and str(mounted_state.get("state", "")) == "complete" \
			and str(entry.get("state", "")) == "thinking":
		# Thought 终态单向：更晚修订的 thinking 补丁也不得回退（决定 4）。
		return false
	_registry.update(root, entry, _ctx, _take_extras(entry))
	record["revision"] = revision
	record["entry"] = entry
	return true


## 最近一次 `apply_entry` 失败的结构化原因，不含正文或 payload。
func last_failure_reason() -> String:
	return _last_failure_reason


## 卸载某条目节点（乐观条目被权威条目接管、快照替换后清理孤儿节点用）。
## 卸载即 reset + 释放：完整内容节点随之释放，重新挂载回到默认状态。
func forget_entry(entry_id: String) -> void:
	var root: Control = mounted_root(entry_id)
	if root == null:
		return
	_mounted.erase(entry_id)
	_registry.reset(root)
	if root.get_parent() != null:
		root.get_parent().remove_child(root)
	root.queue_free()


## 虚拟窗口外的释放入口：与 forget_entry 同语义，语义上强调"离屏即释放，
## 重新挂载回到预览状态"（决定 5）。
func evict_entry(entry_id: String) -> void:
	forget_entry(entry_id)


## 超出挂载上限时从最旧条目开始释放（宿主裁剪，保持渲染器索引一致）。
func trim_to(max_entries: int) -> void:
	if _message_list == null or max_entries <= 0:
		return
	while _message_list.get_child_count() > max_entries:
		var oldest: Node = _message_list.get_child(0)
		var entry_id := str(oldest.get_meta("transcript_entry_id", "")) if oldest.has_meta("transcript_entry_id") else ""
		if entry_id != "" and _mounted.has(entry_id):
			forget_entry(entry_id)
			continue
		_message_list.remove_child(oldest)
		oldest.queue_free()


## 节点所属条目的最新持久化状态（Store 应用过的内容；供复制/展示完整内容）。
func mounted_entry(entry_id: String) -> Dictionary:
	var record: Dictionary = _mounted.get(entry_id, {})
	var entry: Dictionary = record.get("entry", {})
	return entry


func is_mounted(entry_id: String) -> bool:
	return mounted_root(entry_id) != null


## 当前已挂载 entry id 的快照，供 Viewport 判定窗口外 eviction。
func mounted_entry_ids() -> Array:
	_prune_freed_mounts()
	return _mounted.keys()


## 当前 renderer 根节点数量（不含 spacer/瞬时提示）。
func mounted_count() -> int:
	_prune_freed_mounts()
	return _mounted.size()


## 返回仍由 renderer 拥有的根控件；Viewport 仅用于测量，绝不更改其 parent。
func mounted_root(entry_id: String) -> Control:
	var record: Dictionary = _mounted.get(entry_id, {})
	# Dictionary 可能在父级析构后的同一帧仍保留已释放对象；必须先以 Variant
	# 读取，不能直接赋给 `Control`（那一步本身会触发“previously freed”错误）。
	var root_value: Variant = record.get("root", null)
	# 不要先做 `is Control`：对已释放 Object 的类型判断本身会报错。
	if root_value != null and is_instance_valid(root_value):
		return root_value as Control
	if not record.is_empty():
		_mounted.erase(entry_id)
	return null


## 统计已挂载富文本字符，供长会话资源诊断；不读取 Store 的完整正文。
func mounted_rich_text_characters() -> int:
	var total := 0
	for entry_id in _mounted.keys():
		total += _rich_text_characters(mounted_root(str(entry_id)))
	return total


## 每个已挂载条目的预览/完整模式，供诊断与高度缓存键使用。
func mounted_content_modes() -> Dictionary:
	var modes := {}
	for entry_id in _mounted.keys():
		modes[entry_id] = content_mode_for(str(entry_id))
	return modes


func content_mode_for(entry_id: String) -> String:
	var root := mounted_root(entry_id)
	if root == null:
		return "preview"
	if root.has_meta("tmr_full_mode") and bool(root.get_meta("tmr_full_mode", false)):
		return "complete"
	if root.has_meta("tmr_display_complete") and bool(root.get_meta("tmr_display_complete", false)):
		return "complete"
	if root.has_meta("tr_detail_full") and bool(root.get_meta("tr_detail_full", false)):
		return "complete"
	return "preview"


## 从任意后代节点解析所属条目 id。
func entry_id_for_node(node: Control) -> String:
	var current: Node = node
	while current != null:
		if current.has_meta("transcript_entry_id"):
			return str(current.get_meta("transcript_entry_id"))
		current = current.get_parent()
	return ""


## 规范复制：复制值来自条目持久化 payload，与展示截断/预览状态无关。
func copy_text_for_node(node: Control) -> String:
	var entry_id := entry_id_for_node(node)
	if entry_id == "":
		return ""
	var entry := mounted_entry(entry_id)
	if entry.is_empty():
		return ""
	return TranscriptCopy.canonical_text(entry)


# ─── 内部 ────────────────────────────────────────────────────────────────────


func _mount(entry_id: String, root: Control, entry: Dictionary) -> void:
	_mounted[entry_id] = {
		"root": root,
		"revision": int(entry.get("revision", 1)),
		"entry": entry,
	}
	_insert_root(root, int(entry.get("ordinal", -1)))


## 按 ordinal 插入；乐观条目（ordinal=-1）追加在末尾，真实条目不插到其后。
func _insert_root(root: Control, ordinal: int) -> void:
	if ordinal < 0:
		_message_list.add_child(root)
		return
	var insert_index := _message_list.get_child_count()
	for index in range(_message_list.get_child_count()):
		var child := _message_list.get_child(index)
		# 瞬时提示和内联确认与 transcript 共用时间线，但没有 durable ordinal。
		# 它们是位置锚点，水合较晚的 durable 条目不得把它们当成 ordinal=-1
		# 而插到其前面。
		if not child.has_meta("transcript_ordinal"):
			continue
		var child_ordinal := int(child.get_meta("transcript_ordinal"))
		if child_ordinal < 0 or child_ordinal > ordinal:
			insert_index = index
			break
	_message_list.add_child(root)
	_message_list.move_child(root, insert_index)


## 消费该条目登记过的执行前预览（仅首次创建/更新时复用一次）。
func _take_extras(entry: Dictionary) -> Dictionary:
	var kind := str(entry.get("kind", ""))
	if kind != "tool_activity" and kind != "approval":
		return {}
	var tool_call_id := str(entry.get("tool_call_id", ""))
	if tool_call_id == "" or not _preview_cache.has(tool_call_id):
		return {}
	var cached: Dictionary = _preview_cache[tool_call_id]
	_preview_cache.erase(tool_call_id)
	var stats_value: Variant = cached.get("stats", {})
	return {
		"preview": cached.get("preview"),
		"stats": stats_value if stats_value is Dictionary else {},
	}


func _rich_text_characters(node: Node) -> int:
	if node == null or not is_instance_valid(node):
		return 0
	var total: int = node.get_parsed_text().length() if node is RichTextLabel else 0
	for child in node.get_children():
		total += _rich_text_characters(child)
	return total


## 清理被外部父节点释放的残留引用；renderer 不再把它们当作可更新挂载项。
func _prune_freed_mounts() -> void:
	for entry_id_value in _mounted.keys():
		var entry_id := str(entry_id_value)
		mounted_root(entry_id)
