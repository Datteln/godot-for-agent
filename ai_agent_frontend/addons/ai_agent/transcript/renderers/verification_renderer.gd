## 校验条目渲染器：按 `running`/`passed`/`failed` 呈现结构化校验状态（任务 2.3）。
@tool
extends RefCounted

const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")


func create(entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> Control:
	if str(entry.get("kind", "")) != "verification":
		return null
	var root := VBoxContainer.new()
	root.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	root.add_theme_constant_override("separation", 2)
	root.set_meta("transcript_entry_id", str(entry.get("entry_id", "")))
	root.set_meta("transcript_kind", "verification")
	root.set_meta("transcript_ordinal", int(entry.get("ordinal", -1)))
	_rebuild(root, entry, ctx)
	return root


func update(root: Control, entry: Dictionary, ctx: RefCounted, _extras: Dictionary = {}) -> void:
	if str(entry.get("kind", "")) != "verification":
		return
	_rebuild(root, entry, ctx)


func reset(root: Control) -> void:
	for key in ["transcript_ordinal", "transcript_entry_id", "transcript_kind"]:
		root.remove_meta(key)


func _rebuild(root: Control, entry: Dictionary, ctx: RefCounted) -> void:
	for child in root.get_children():
		root.remove_child(child)
		child.queue_free()
	var state := str(entry.get("state", ""))
	var payload: Dictionary = ctx.payload_of(entry)
	var factory: RefCounted = ctx.node_factory
	var text := TranscriptCopy.verification_plain_text(payload, state)
	var color: Color
	match state:
		"passed":
			color = ctx.theme_color("success_text")
		"failed":
			color = ctx.theme_color("error_text")
		_:
			color = ctx.theme_color("muted_text")
	root.add_child(factory.make_log_rich_text(text, color, "", true))
