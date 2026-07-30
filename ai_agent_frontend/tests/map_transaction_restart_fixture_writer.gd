extends SceneTree

const Fixture = preload("res://tests/map_transaction_fixture.gd")


func _init() -> void:
	var journal_dir := ""
	var state := "committing"
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--journal-dir="):
			journal_dir = argument.trim_prefix("--journal-dir=")
		elif argument.begins_with("--state="):
			state = argument.trim_prefix("--state=")
	if not journal_dir.begins_with("user://"):
		push_error("fixture writer requires an isolated user:// journal directory")
		quit(2)
		return
	var manager = Fixture.make_manager(journal_dir)
	if manager == null:
		quit(3)
		return
	var write_error: Error = manager._write_map_transaction_journal(
		Fixture.journal(state, "tx-separate-process")
	)
	quit(0 if write_error == OK else 4)
