## 计划/进度/系统/日志条目的类型化状态渲染器（任务 2.3）。
##
## 只读取持久化 payload 中的结构化字段（step_index/total_steps/title/summary、
## marker/indent 等），不解析任何展示文本；`system`/`log` 直接呈现
## `payload.text`。
@tool
extends RefCounted

const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")


func create(entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> Control:
	var kind := str(entry.get("kind", ""))
	if not ["plan", "progress", "system", "log"].has(kind):
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	root.set_meta("transcript_entry_id", str(entry.get("entry_id", "")))
	root.set_meta("transcript_kind", kind)
	root.set_meta("transcript_ordinal", int(entry.get("ordinal", -1)))
	_rebuild(root, entry, ctx)
	return root


func update(root: Control, entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> void:
	if str(entry.get("kind", "")) != str(root.get_meta("transcript_kind", "")):
		return
	_rebuild(root, entry, ctx)


func reset(root: Control) -> void:
	for key in ["transcript_ordinal", "transcript_entry_id", "transcript_kind"]:
		root.remove_meta(key)


func _rebuild(root: Control, entry: Dictionary, ctx: RefCounted) -> void:
	for child in root.get_children():
		root.remove_child(child)
		child.queue_free()
	var kind := str(entry.get("kind", ""))
	var state := str(entry.get("state", ""))
	var payload: Dictionary = ctx.payload_of(entry)
	var factory: RefCounted = ctx.node_factory
	match kind:
		"plan", "progress":
			var text := TranscriptCopy.plan_plain_text(payload) if kind == "plan" else TranscriptCopy.progress_plain_text(payload, state)
			root.add_child(factory.make_log_rich_text(text, ctx.theme_color("muted_text"), "", true))
		"log":
			root.add_child(factory.make_log_rich_text(
				str(payload.get("text", "")),
				null,
				"●" if bool(payload.get("marker", false)) else "",
				bool(payload.get("indent", false))
			))
		_:
			root.add_child(factory.make_log_rich_text(str(payload.get("text", "")), ctx.theme_color("muted_text")))
