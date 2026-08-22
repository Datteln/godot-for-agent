## 只读渲染 context：主题、文案、单条展示预算、节点工厂与条目解析。
##
## 所有 renderer 只能读取 context，不得修改；`entry_for` 是通往 Store 的唯一
## 只读查询通道，供挂载后的规范内容访问（复制、显示完整内容）使用，确保内容
## 始终来自持久化 payload，而不是 RichTextLabel 内部状态或 UI metadata。
@tool
extends RefCounted

## 默认单条初始展示字符预算：超过该长度的持久化内容初次挂载只渲染预览，
## 用户明确点击"显示完整内容"后才为该条创建完整富文本。
const DEFAULT_DISPLAY_BUDGET_CHARS := 4000

var theme_colors: Dictionary = {}
## UI 文案函数：(key: String) -> String。
var ui_text: Callable
## 单条初始展示字符预算（对所有长内容 kind 统一生效）。
var display_budget_chars: int = DEFAULT_DISPLAY_BUDGET_CHARS
## 节点工厂（LogEntryRenderer）：提供 RichTextLabel/面板等构建方法。
var node_factory: RefCounted
## (entry_id: String) -> Dictionary：返回条目最新持久化状态；不存在返回空字典。
var resolve_entry: Callable
## (entry_id: String) -> void：错误条目声明可重试时的重试请求通道（只读发起）。
var retry_entry: Callable
## (text: String) -> void：复制落地通道；未设置时使用 DisplayServer 剪贴板。
## 测试/无剪贴板环境可注入自定义接收器。
var clipboard_set: Callable


func theme_color(key: String) -> Color:
	var value = theme_colors.get(key)
	if value is Color:
		return value
	return Color(0.55, 0.55, 0.55)


func color_tag(key: String) -> String:
	return "#" + theme_color(key).to_html(false)


func ui_or(key: String, fallback: String) -> String:
	if ui_text.is_valid():
		var value = ui_text.call(key)
		if typeof(value) == TYPE_STRING and str(value) != "" and str(value) != key:
			return str(value)
	return fallback


## 按 entry_id 解析最新持久化条目（只读）。
func entry_for(entry_id: String) -> Dictionary:
	if resolve_entry.is_valid():
		var value = resolve_entry.call(entry_id)
		if value is Dictionary:
			return value
	return {}


func payload_of(entry: Dictionary) -> Dictionary:
	var payload: Variant = entry.get("payload", {})
	return payload if payload is Dictionary else {}
