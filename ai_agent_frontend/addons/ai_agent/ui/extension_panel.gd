@tool
extends Window

var _text: TextEdit


func _ready() -> void:
	title = "AI Agent Extensions"
	_text = TextEdit.new()
	_text.editable = false
	_text.custom_minimum_size = Vector2(720, 520)
	add_child(_text)


func show_from_doctor(report: Dictionary) -> void:
	var payload := {
		"skills": report.get("skills", []),
		"warnings": report.get("warnings", [])
	}
	_text.text = JSON.stringify(payload, "\t")
	popup_centered(Vector2i(760, 560))
