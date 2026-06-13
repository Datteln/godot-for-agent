@tool
extends ConfirmationDialog

signal accepted_recovery(pointer: Dictionary)
signal rejected_recovery

var _pointer: Dictionary = {}


func _ready() -> void:
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
