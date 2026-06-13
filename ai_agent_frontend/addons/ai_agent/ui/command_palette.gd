@tool
extends Window

signal run_requested(name: String, args: Dictionary)

var _command_input: LineEdit
var _args_input: TextEdit


func _ready() -> void:
	title = "AI Agent Commands"
	var root := VBoxContainer.new()
	add_child(root)

	_command_input = LineEdit.new()
	_command_input.placeholder_text = "doctor | compact | rebuild_index"
	root.add_child(_command_input)

	_args_input = TextEdit.new()
	_args_input.custom_minimum_size = Vector2(560, 180)
	_args_input.text = "{}"
	root.add_child(_args_input)

	var run_btn := Button.new()
	run_btn.text = "Run"
	run_btn.pressed.connect(_on_run)
	root.add_child(run_btn)


func open() -> void:
	popup_centered(Vector2i(600, 320))


func _on_run() -> void:
	var args := {}
	var parsed := JSON.parse_string(_args_input.text)
	if parsed is Dictionary:
		args = parsed
	run_requested.emit(_command_input.text.strip_edges(), args)
	hide()
