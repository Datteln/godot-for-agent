@tool
extends Window

var _text: TextEdit


func _ready() -> void:
	title = "AI Agent Doctor"
	_text = TextEdit.new()
	_text.editable = false
	_text.custom_minimum_size = Vector2(720, 520)
	add_child(_text)


func show_report(report: Dictionary) -> void:
	_text.text = JSON.stringify(report, "\t")
	popup_centered(Vector2i(760, 560))
