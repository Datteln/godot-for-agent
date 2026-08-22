## 本地瞬时提示宿主（任务 3.4 / 决定 7）。
##
## "等待模型""命令执行中"、命令回显、诊断/记忆报告等只反映本地瞬时状态的提示
## 由本宿主创建：不进入 transcript Store、entry ID、ordinal、测量或分页，也不
## 参与历史重建。请求完成/失败、会话切换、水合替换、提示被覆盖时，宿主直接
## `queue_free` 对应节点；绝不从快照、WebSocket 或 Viewport remount 重新渲染。
## 服务端持久化的 `kind=error` 与 `kind=progress` 不属于 transient，仍按 typed
## entry 渲染。
@tool
extends RefCounted

## 瞬时提示的硬渲染上限（与展示预算不同：这不是预览，仅防御极端长度）。
const MAX_NOTICE_RENDER_CHARS := 90000

var theme_colors: Dictionary = {}
## UI 文案函数：(key: String) -> String。
var ui_text: Callable
## 节点工厂（LogEntryRenderer）。
var node_factory: RefCounted

var _container: VBoxContainer
## key -> Control：带键提示（等待/执行中）被同名新提示覆盖。
var _keyed: Dictionary = {}


func attach(container: VBoxContainer) -> void:
	_container = container


## 显示一条瞬时提示；返回节点。style: system/error/user/report。
func show_notice(text: String, style := "system") -> Control:
	if _container == null:
		return null
	var node := _build_notice(text, style)
	if node != null:
		_container.add_child(node)
	return node


## 显示带键提示：同键旧提示先被丢弃（覆盖语义）。
func show_keyed(key: String, text: String, style := "system") -> Control:
	discard_keyed(key)
	var node := show_notice(text, style)
	if node != null and key != "":
		_keyed[key] = node
	return node


## 丢弃带键提示（请求完成/失败/被打断时调用）。
func discard_keyed(key: String) -> void:
	var node: Control = _keyed.get(key)
	if node != null:
		_keyed.erase(key)
		_free_node(node)


## 会话切换/水合替换/重置：丢弃全部瞬时提示，且不再重挂载。
func clear_all() -> void:
	_keyed.clear()
	if _container == null:
		return
	for child in _container.get_children():
		_container.remove_child(child)
		child.queue_free()


# ─── 构建 ────────────────────────────────────────────────────────────────────


func _build_notice(text: String, style: String) -> Control:
	var bounded := _limit_text(text)
	match style:
		"user":
			return _build_panel_notice(bounded, "user")
		"error":
			return _build_panel_notice(bounded, "error")
		"report":
			return _build_panel_notice(bounded, "report")
		_:
			return node_factory.make_log_rich_text(bounded, _theme_color("muted_text"))


func _build_panel_notice(text: String, style: String) -> Control:
	var row := HBoxContainer.new()
	row.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	if style == "user":
		var spacer := Control.new()
		spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
		spacer.size_flags_stretch_ratio = 0.35
		row.add_child(spacer)
	var panel: PanelContainer
	if style == "user":
		panel = node_factory.make_message_panel("user")
		panel.custom_minimum_size = Vector2(320, 0)
	elif style == "error":
		panel = node_factory.make_message_panel("error")
	else:
		panel = node_factory.make_panel(_theme_color("panel_alt_bg"), _theme_color("panel_alt_border"))
	panel.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.size_flags_stretch_ratio = 0.65 if style == "user" else 1.0
	row.add_child(panel)
	var body := VBoxContainer.new()
	body.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	panel.add_child(body)
	body.add_child(node_factory.make_rich_text(text))
	return row


func _limit_text(text: String) -> String:
	if MAX_NOTICE_RENDER_CHARS <= 0 or text.length() <= MAX_NOTICE_RENDER_CHARS:
		return text
	return text.left(MAX_NOTICE_RENDER_CHARS) + "\n\n... (display truncated)"


func _free_node(node: Control) -> void:
	if node == null or not is_instance_valid(node):
		return
	if node.get_parent() != null:
		node.get_parent().remove_child(node)
	node.queue_free()


func _theme_color(key: String) -> Color:
	var value = theme_colors.get(key)
	if value is Color:
		return value
	return Color(0.55, 0.55, 0.55)
