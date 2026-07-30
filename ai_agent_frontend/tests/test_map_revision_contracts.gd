extends SceneTree

const Fixture = preload("res://tests/map_transaction_fixture.gd")
const MapRevisionTracker = preload("res://addons/ai_agent/tools/map_revision_tracker.gd")
const ToolExecutor = preload("res://addons/ai_agent/tools/tool_executor.gd")


class FakeRevisionTracker:
	extends Node

	var revision := 0

	func current_revision(_key: String) -> int:
		return revision


func _init() -> void:
	var failure := _run_all()
	if failure != "":
		push_error(failure)
		quit(1)
		return
	quit()


func _run_all() -> String:
	var stale_error := _stale_revision_is_rejected()
	if stale_error != "":
		return stale_error
	var commit_error := _approval_commit_and_retry()
	if commit_error != "":
		return commit_error
	var boundary_error := _commit_boundaries_remain_ambiguous()
	if boundary_error != "":
		return boundary_error
	var fingerprint_error := _external_change_fingerprint_advances_once()
	if fingerprint_error != "":
		return fingerprint_error
	return _execution_order_contract()


func _stale_revision_is_rejected() -> String:
	var executor = ToolExecutor.new()
	var tracker := FakeRevisionTracker.new()
	tracker.revision = 11
	executor.map_revision_tracker = tracker
	var input := {"expected_revision": 10}
	var conflict: Dictionary = executor._validate_map_write_revision(
		input,
		"Map/Main"
	)
	if str(conflict.get("error_code", "")) != "map_revision_conflict":
		return "stale write was not rejected before batch creation"
	if int(conflict.get("actual_revision", -1)) != 11:
		return "revision conflict omitted authoritative revision"
	if input.has("map_transaction_id"):
		return "revision validation mutated transaction identity"
	return ""


func _approval_commit_and_retry() -> String:
	var directory := "user://map_revision_contracts/approval"
	Fixture.clean_directory(directory)
	var manager = Fixture.make_manager(directory)
	var prepared: Dictionary = manager.prepare_map_write_group(
		"tx-approval",
		"approved batch",
		"Map/Main",
		10,
		"approval-1",
		"fingerprint-1",
		10
	)
	if not bool(prepared.get("ok", false)):
		return "approved transaction could not prepare: %s" % prepared
	var committed: Dictionary = manager.commit_map_write_group(
		"tx-approval",
		"Map/Main",
		10
	)
	if not bool(committed.get("ok", false)):
		return "approved transaction did not commit"
	var records_value = committed.get("approval_records", [])
	if not (records_value is Array) or records_value.size() != 1:
		return "committed result omitted exact approval identity"
	var record: Dictionary = records_value[0]
	if (
		str(record.get("approval_id", "")) != "approval-1"
		or str(record.get("batch_fingerprint", "")) != "fingerprint-1"
	):
		return "committed approval identity changed"
	var executor = ToolExecutor.new()
	var tracker := FakeRevisionTracker.new()
	tracker.revision = 11
	executor.map_revision_tracker = tracker
	var retry_conflict: Dictionary = executor._validate_map_write_revision(
		{"expected_revision": 10},
		"Map/Main"
	)
	if str(retry_conflict.get("error_code", "")) != "map_revision_conflict":
		return "service-exit retry could double-apply committed batch"
	Fixture.clean_directory(directory)
	return ""


func _commit_boundaries_remain_ambiguous() -> String:
	for boundary in ["commit_before_apply", "commit_after_apply"]:
		var directory := "user://map_revision_contracts/%s" % boundary
		Fixture.clean_directory(directory)
		var manager = Fixture.make_manager(directory, {boundary: 1})
		var prepared: Dictionary = manager.prepare_map_write_group(
			"tx-%s" % boundary,
			"boundary",
			"Map/Main",
			4
		)
		if not bool(prepared.get("ok", false)):
			return "commit boundary fixture could not prepare"
		var result: Dictionary = manager.commit_map_write_group(
			"tx-%s" % boundary,
			"Map/Main",
			4
		)
		if str(result.get("map_transaction_status", "")) != "committing":
			return "%s did not retain ambiguous committing state" % boundary
		var restarted = Fixture.make_manager(directory)
		restarted._recover_incomplete_map_transaction()
		var code := str(
			restarted.map_recovery_status().get("details", {}).get(
				"error_code",
				""
			)
		)
		if code != "map_transaction_commit_outcome_ambiguous":
			return "%s restart guessed a commit outcome" % boundary
		Fixture.clean_directory(directory)
	return ""


func _external_change_fingerprint_advances_once() -> String:
	var tracker = MapRevisionTracker.new()
	var changed: Array[String] = tracker._changed_keys(
		{"Map/Main": "before"},
		{"Map/Main": "after"}
	)
	if changed != ["Map/Main"]:
		return "external fingerprint drift was not detected exactly once"
	var unchanged: Array[String] = tracker._changed_keys(
		{"Map/Main": "after"},
		{"Map/Main": "after"}
	)
	if not unchanged.is_empty():
		return "recaptured Undo/Redo fingerprint would double-bump revision"
	if not tracker.has_method("_synchronize_history_change"):
		return "keyboard/programmatic history do not share synchronization callback"
	return ""


func _execution_order_contract() -> String:
	var source := FileAccess.get_file_as_string(
		"res://addons/ai_agent/tools/tool_executor.gd"
	)
	var recovery_index := source.find("ensure_map_recovery_ready")
	var synchronization_index := source.find(
		"synchronize_mutation_boundary",
		recovery_index
	)
	var revision_index := source.find(
		"_validate_map_write_revision",
		synchronization_index
	)
	var batch_index := source.find("_begin_map_write_batch", revision_index)
	if (
		recovery_index < 0
		or synchronization_index <= recovery_index
		or revision_index <= synchronization_index
		or batch_index <= revision_index
	):
		return (
			"map mutation order must be recovery -> authoritative sync -> "
			+ "revision CAS -> transaction begin"
		)
	if "failpoint" in source.to_lower():
		return "tool request execution surface references test failpoints"
	return ""
