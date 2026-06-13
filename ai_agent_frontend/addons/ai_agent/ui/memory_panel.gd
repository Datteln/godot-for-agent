@tool
extends Window

signal save_requested(text: String)
signal delete_requested(id: String)
signal clear_requested

var _text: TextEdit
var _new_memory: TextEdit
var _delete_id: LineEdit


func _ready() -> void:
	title = "AI Agent Memory"

	var root := VBoxContainer.new()
	add_child(root)

	_text = TextEdit.new()
	_text.editable = false
	_text.custom_minimum_size = Vector2(640, 280)
	root.add_child(_text)

	_new_memory = TextEdit.new()
	_new_memory.custom_minimum_size = Vector2(640, 90)
	_new_memory.placeholder_text = "Memory text to save after user confirmation..."
	root.add_child(_new_memory)

	var save_btn := Button.new()
	save_btn.text = "Save Memory"
	save_btn.pressed.connect(func(): save_requested.emit(_new_memory.text))
	root.add_child(save_btn)

	var delete_row := HBoxContainer.new()
	root.add_child(delete_row)
	_delete_id = LineEdit.new()
	_delete_id.placeholder_text = "memory id"
	_delete_id.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	delete_row.add_child(_delete_id)

	var delete_btn := Button.new()
	delete_btn.text = "Delete"
	delete_btn.pressed.connect(func(): delete_requested.emit(_delete_id.text.strip_edges()))
	delete_row.add_child(delete_btn)

	var clear_btn := Button.new()
	clear_btn.text = "Clear All"
	clear_btn.pressed.connect(func(): clear_requested.emit())
	root.add_child(clear_btn)


func open() -> void:
	popup_centered(Vector2i(680, 520))


func show_memory_response(response: Dictionary) -> void:
	_text.text = JSON.stringify(response, "\t")
	open()
