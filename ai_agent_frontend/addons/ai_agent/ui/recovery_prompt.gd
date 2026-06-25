@tool
extends ConfirmationDialog

signal accepted_recovery(pointer: Dictionary)
signal rejected_recovery

var _pointer: Dictionary = {}


func _ready() -> void:
	# 默认 exclusive=true 会在弹出时抢父窗口（/root）的独占子窗口位——如果这时
	# 用户正好开着 Project Settings 之类同样独占的编辑器窗口，会触发引擎警告
	# "parent window already has another exclusive child"。这个恢复确认框不需要
	# 真正独占整个编辑器，关掉这个标记即可消除警告，行为不受影响。
	exclusive = false
	title = "Recover AI Agent Session"
	confirmed.connect(func(): accepted_recovery.emit(_pointer))
	canceled.connect(func(): rejected_recovery.emit())


func show_pointer(pointer: Dictionary) -> void:
	_pointer = pointer
	dialog_text = "Recover session %s from %s?" % [
		pointer.get("session_id", ""),
		pointer.get("updated_at", "")
	]
	popup_centered()
