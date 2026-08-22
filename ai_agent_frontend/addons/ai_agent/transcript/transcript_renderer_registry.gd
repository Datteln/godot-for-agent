## 渲染器注册表（任务 1.2 / 决定 1）。
##
## 只按条目 `kind` 分发到对应 renderer；任何 renderer 都不读取 WebSocket 原包、
## HTTP 响应或正文前缀。重复判定、状态与顺序归 Store，注册表不做任何推断。
## 缺少 `kind` 的输入一律拒绝（返回 null），不得创建节点。
##
## 宿主（TranscriptViewport）通过本注册表执行 mounted-control 契约：
## `create(entry)` 仅初次挂载调用；`update(root, entry)` 原地更新已挂载控件；
## `reset(root)` 在卸载/复用前断开回调并清空视图状态。
@tool
extends RefCounted

const TextMessageRenderer = preload("res://addons/ai_agent/transcript/renderers/text_message_renderer.gd")
const ThoughtRenderer = preload("res://addons/ai_agent/transcript/renderers/thought_renderer.gd")
const ToolRenderer = preload("res://addons/ai_agent/transcript/renderers/tool_renderer.gd")
const ApprovalRenderer = preload("res://addons/ai_agent/transcript/renderers/approval_renderer.gd")
const StatusRenderer = preload("res://addons/ai_agent/transcript/renderers/status_renderer.gd")
const VerificationRenderer = preload("res://addons/ai_agent/transcript/renderers/verification_renderer.gd")
const ErrorRenderer = preload("res://addons/ai_agent/transcript/renderers/error_renderer.gd")

var _renderers: Dictionary = {}


func _init() -> void:
	var text := TextMessageRenderer.new()
	var status := StatusRenderer.new()
	_renderers = {
		"user": text,
		"assistant": text,
		"thought": ThoughtRenderer.new(),
		"tool_activity": ToolRenderer.new(),
		"approval": ApprovalRenderer.new(),
		"plan": status,
		"progress": status,
		"system": status,
		"log": status,
		"verification": VerificationRenderer.new(),
		"error": ErrorRenderer.new(),
	}


func has_renderer(kind: String) -> bool:
	return _renderers.has(kind)


## 按 kind 创建根控件；缺少 kind 或未注册 kind 返回 null（拒绝渲染）。
func create(entry: Dictionary, ctx: RefCounted, extras: Dictionary = {}) -> Control:
	var kind := str(entry.get("kind", ""))
	var renderer: RefCounted = _renderers.get(kind)
	if renderer == null:
		return null
	return renderer.create(entry, ctx, extras)


## 原地更新已挂载根控件。
func update(root: Control, entry: Dictionary, ctx: RefCounted, extras: Dictionary = {}) -> void:
	var kind := str(entry.get("kind", ""))
	var renderer: RefCounted = _renderers.get(kind)
	if renderer == null:
		return
	renderer.update(root, entry, ctx, extras)


## 卸载/复用前复位：断开回调、清空选择/展开/可操作状态与身份标记。
func reset(root: Control) -> void:
	var kind := str(root.get_meta("transcript_kind", "")) if root.has_meta("transcript_kind") else ""
	var renderer: RefCounted = _renderers.get(kind)
	if renderer != null:
		renderer.reset(root)
