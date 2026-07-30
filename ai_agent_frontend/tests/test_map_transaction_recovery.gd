extends SceneTree

const Fixture = preload("res://tests/map_transaction_fixture.gd")
const MapTransactionIO = preload("res://addons/ai_agent/undo/map_transaction_io.gd")
const MapTransactionPolicy = preload("res://addons/ai_agent/undo/map_transaction_policy.gd")

var _root := ""


func _init() -> void:
	_root = "user://map_transaction_tests/%d" % Time.get_ticks_usec()
	var failure := _run_all()
	Fixture.clean_directory(_root)
	if failure != "":
		push_error(failure)
		quit(1)
		return
	quit()


func _run_all() -> String:
	var production_adapter = MapTransactionIO.new()
	if not production_adapter.production_failpoints_disabled():
		return "production adapter unexpectedly enabled failpoints"
	for state in ["prepared", "applying", "committed", "rolled_back"]:
		var state_error := _recover_state(str(state), false)
		if state_error != "":
			return state_error
	var committing_error := _recover_state("committing", true)
	if committing_error != "":
		return committing_error
	for terminal in ["committed", "rolled_back"]:
		var cleanup_error := _terminal_cleanup_retry(str(terminal))
		if cleanup_error != "":
			return cleanup_error
	var failpoint_error := _journal_after_write_is_durable()
	if failpoint_error != "":
		return failpoint_error
	var single_flight_error := _single_flight_guard()
	if single_flight_error != "":
		return single_flight_error
	return _benchmark_maximum_fixture()


func _recover_state(state: String, should_block: bool) -> String:
	var directory := _root.path_join("state_%s" % state)
	var manager = Fixture.make_manager(directory)
	var write_error: Error = manager._write_map_transaction_journal(
		Fixture.journal(state, "tx-%s" % state)
	)
	if write_error != OK:
		return "failed to write %s fixture: %d" % [state, write_error]
	manager._recover_incomplete_map_transaction()
	var recovery: Dictionary = manager.map_recovery_status()
	if bool(recovery.get("blocked", false)) != should_block:
		return "unexpected recovery classification for %s: %s" % [state, recovery]
	var latest: Dictionary = manager._latest_map_transaction_journal()
	if should_block:
		if not bool(latest.get("found", false)):
			return "ambiguous committing journal was deleted"
		if str(recovery.get("details", {}).get("error_code", "")) != (
			"map_transaction_commit_outcome_ambiguous"
		):
			return "committing journal did not report typed ambiguity"
	elif bool(latest.get("found", false)):
		return "clean recovery retained journal for %s" % state
	return ""


func _terminal_cleanup_retry(state: String) -> String:
	var directory := _root.path_join("cleanup_%s" % state)
	var writer = Fixture.make_manager(directory)
	var write_error: Error = writer._write_map_transaction_journal(
		Fixture.journal(state, "tx-cleanup-%s" % state)
	)
	if write_error != OK:
		return "terminal fixture write failed"
	var blocked = Fixture.make_manager(
		directory,
		{"cleanup_before_delete": 1}
	)
	blocked._recover_incomplete_map_transaction()
	if not bool(blocked.map_recovery_status().get("blocked", false)):
		return "cleanup failure did not block for %s" % state
	if not bool(blocked._latest_map_transaction_journal().get("found", false)):
		return "cleanup failure removed terminal marker for %s" % state
	var restarted = Fixture.make_manager(directory)
	restarted._recover_incomplete_map_transaction()
	if bool(restarted._latest_map_transaction_journal().get("found", false)):
		return "restart did not retry terminal cleanup for %s" % state
	return ""


func _journal_after_write_is_durable() -> String:
	var directory := _root.path_join("after_write")
	var manager = Fixture.make_manager(
		directory,
		{"journal_prepared_after_write": 1}
	)
	var write_error: Error = manager._write_map_transaction_journal(
		Fixture.journal("prepared", "tx-after-write")
	)
	if write_error == OK:
		return "after-write failpoint did not trigger"
	var latest: Dictionary = manager._latest_map_transaction_journal()
	if not bool(latest.get("ok", false)) or not bool(latest.get("found", false)):
		return "after-write failpoint lost durable journal"
	return ""


func _single_flight_guard() -> String:
	var manager = Fixture.make_manager(_root.path_join("single_flight"))
	manager._map_recovery_in_progress = true
	var result: Dictionary = manager.ensure_map_recovery_ready()
	manager._map_recovery_in_progress = false
	if str(result.get("error_code", "")) != (
		"map_transaction_recovery_in_progress"
	):
		return "concurrent recovery caller did not join/block on single flight"
	return ""


func _benchmark_maximum_fixture() -> String:
	var directory := _root.path_join("benchmark")
	var manager = Fixture.make_manager(directory)
	var operations: Array = []
	operations.resize(MapTransactionPolicy.MAX_RECOVERY_OPERATIONS)
	for index in range(operations.size()):
		operations[index] = {"type": "noop", "index": index}
	var write_error: Error = manager._write_map_transaction_journal(
		Fixture.journal("committing", "tx-benchmark", operations)
	)
	if write_error != OK:
		return "maximum recovery fixture could not be persisted"
	var started := Time.get_ticks_msec()
	var latest: Dictionary = manager._latest_map_transaction_journal()
	var elapsed := Time.get_ticks_msec() - started
	if not bool(latest.get("ok", false)):
		return "maximum recovery fixture failed bounded parsing"
	if elapsed > MapTransactionPolicy.MAX_RECOVERY_LATENCY_MS:
		return (
			"maximum recovery fixture exceeded latency policy: %dms > %dms"
			% [elapsed, MapTransactionPolicy.MAX_RECOVERY_LATENCY_MS]
		)
	print(
		"map recovery maximum fixture parsed in %dms (%d operations)"
		% [elapsed, operations.size()]
	)
	return ""
