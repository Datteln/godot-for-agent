extends SceneTree

const Fixture = preload("res://tests/map_transaction_fixture.gd")
const ToolExecutor = preload("res://addons/ai_agent/tools/tool_executor.gd")


class FakeRevisionTracker:
	extends Node

	var revision := 0

	func current_revision(_key: String) -> int:
		return revision


func _init() -> void:
	var directory := "user://map_transaction_e2e"
	Fixture.clean_directory(directory)
	var failure := _run_task(directory)
	Fixture.clean_directory(directory)
	if failure != "":
		push_error(failure)
		quit(1)
		return
	quit()


func _run_task(directory: String) -> String:
	var manager = Fixture.make_manager(directory.path_join("journals"))
	manager.undo_redo = UndoRedo.new()
	var map_path := directory.path_join("map.txt")
	var revision_path := directory.path_join("revision.json")
	if manager._write_file_text(map_path, "before-map") != OK:
		return "could not create E2E map fixture"
	if manager._write_file_text(revision_path, "{\"revision\":10}") != OK:
		return "could not create E2E revision fixture"

	var approved: Dictionary = manager.prepare_map_write_group(
		"tx-e2e",
		"validated approved map batch",
		"Map/Main",
		10,
		"approval-e2e",
		"fingerprint-e2e",
		10
	)
	if not bool(approved.get("ok", false)):
		return "validate/approve prepare failed: %s" % approved
	if manager.record_file_write(
		map_path,
		"before-map",
		"after-map",
		true
	) != OK:
		return "map operation could not join approved transaction"
	if manager._write_file_text(map_path, "after-map") != OK:
		return "map mutation failed"
	if manager.record_file_write(
		revision_path,
		"{\"revision\":10}",
		"{\"revision\":11}",
		true
	) != OK:
		return "revision operation could not join approved transaction"
	if manager._write_file_text(revision_path, "{\"revision\":11}") != OK:
		return "revision mutation failed"
	manager.update_map_transaction_revision("tx-e2e", 11)

	var committed: Dictionary = manager.commit_map_write_group(
		"tx-e2e",
		"Map/Main",
		11
	)
	if not bool(committed.get("ok", false)):
		return "approved E2E transaction did not commit: %s" % committed
	if (
		str(committed.get("map_transaction_status", "")) != "committed"
		or int(committed.get("committed_revision", -1)) != 11
	):
		return "commit result omitted durable revision identity"
	var records_value = committed.get("approval_records", [])
	if not (records_value is Array) or records_value.size() != 1:
		return "commit result omitted approval identity"

	manager.undo_redo.undo()
	if FileAccess.get_file_as_string(
		ProjectSettings.globalize_path(map_path)
	) != "before-map":
		return "Undo did not restore map content"
	if FileAccess.get_file_as_string(
		ProjectSettings.globalize_path(revision_path)
	) != "{\"revision\":10}":
		return "Undo did not restore revision metadata with map content"

	manager.undo_redo.redo()
	if FileAccess.get_file_as_string(
		ProjectSettings.globalize_path(map_path)
	) != "after-map":
		return "Redo did not restore committed map content"
	if FileAccess.get_file_as_string(
		ProjectSettings.globalize_path(revision_path)
	) != "{\"revision\":11}":
		return "Redo did not restore committed revision metadata"

	var restarted = Fixture.make_manager(directory.path_join("journals"))
	restarted._recover_incomplete_map_transaction()
	if bool(restarted.map_recovery_status().get("blocked", false)):
		return "clean committed transaction blocked restart"
	if bool(restarted._latest_map_transaction_journal().get("found", false)):
		return "clean committed transaction retained a journal"

	var executor = ToolExecutor.new()
	var tracker := FakeRevisionTracker.new()
	tracker.revision = 11
	executor.map_revision_tracker = tracker
	var retry: Dictionary = executor._validate_map_write_revision(
		{"expected_revision": 10},
		"Map/Main"
	)
	if str(retry.get("error_code", "")) != "map_revision_conflict":
		return "restart retry could double-apply revision 11"
	return ""
