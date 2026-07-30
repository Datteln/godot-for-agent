extends RefCounted

const MapTransactionIO = preload("res://addons/ai_agent/undo/map_transaction_io.gd")
const UnifiedUndoManager = preload("res://addons/ai_agent/undo/unified_undo_manager.gd")


static func make_manager(
	journal_dir: String,
	failpoints: Dictionary = {}
) -> Node:
	var adapter = MapTransactionIO.new()
	adapter.configure_test_failpoints(failpoints)
	var manager = UnifiedUndoManager.new()
	var configure_error: Error = manager.configure_test_transaction_io(
		adapter,
		journal_dir
	)
	if configure_error != OK:
		return null
	var dir_error: Error = adapter.make_dir_recursive(
		ProjectSettings.globalize_path(journal_dir)
	)
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		return null
	return manager


static func journal(
	status: String,
	transaction_id: String,
	operations: Array = []
) -> Dictionary:
	return {
		"schema_version": 2,
		"transaction_id": transaction_id,
		"target": "Map/Main",
		"base_revision": 10,
		"latest_revision": 11,
		"started_at_ms": 1,
		"tool_count": 1,
		"snapshot_bytes": 0,
		"sequence": 1,
		"status": status,
		"approval_records": [],
		"scene_snapshot": {},
		"operations": operations,
		"before_fingerprint": "",
		"after_fingerprint": "",
	}


static func clean_directory(path: String) -> void:
	var absolute := ProjectSettings.globalize_path(path)
	if not DirAccess.dir_exists_absolute(absolute):
		return
	for file_name in DirAccess.get_files_at(absolute):
		DirAccess.remove_absolute(absolute.path_join(str(file_name)))
	for directory_name in DirAccess.get_directories_at(absolute):
		clean_directory(path.path_join(str(directory_name)))
	DirAccess.remove_absolute(absolute)
