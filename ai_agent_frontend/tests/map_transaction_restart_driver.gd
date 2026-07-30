extends SceneTree

const Fixture = preload("res://tests/map_transaction_fixture.gd")


func _init() -> void:
	var journal_dir := ""
	var expected_code := ""
	for argument in OS.get_cmdline_user_args():
		if argument.begins_with("--journal-dir="):
			journal_dir = argument.trim_prefix("--journal-dir=")
		elif argument.begins_with("--expected-code="):
			expected_code = argument.trim_prefix("--expected-code=")
	if not journal_dir.begins_with("user://"):
		push_error("restart driver requires an isolated user:// journal directory")
		quit(2)
		return
	var manager = Fixture.make_manager(journal_dir)
	if manager == null:
		quit(3)
		return
	manager._recover_incomplete_map_transaction()
	var status: Dictionary = manager.map_recovery_status()
	print(JSON.stringify(status))
	var actual_code := str(status.get("details", {}).get("error_code", ""))
	if expected_code != "" and actual_code != expected_code:
		push_error(
			"restart recovery code mismatch: expected=%s actual=%s"
			% [expected_code, actual_code]
		)
		quit(4)
		return
	quit()
