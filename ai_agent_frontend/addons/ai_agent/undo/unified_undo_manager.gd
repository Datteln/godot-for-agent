@tool
extends Node

const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")
const MapTransactionPolicy = preload("res://addons/ai_agent/undo/map_transaction_policy.gd")

const MAP_TRANSACTION_JOURNAL_DIR := "res://.ai_agent_service/map_agent/transactions"

var undo_redo: EditorUndoRedoManager
var editor_interface: EditorInterface

var _batch_desc := ""
var _ops: Array[Dictionary] = []
var _active := false
var _map_transaction_id := ""
var _map_transaction_target := ""
var _map_transaction_base_revision := 0
var _map_transaction_started_at_ms := 0
var _map_transaction_tool_count := 0
var _map_transaction_snapshot_bytes := 0
var _map_transaction_journal_sequence := 0
var _map_transaction_latest_revision := 0
var _map_transaction_scene_snapshot := {}
var _map_transaction_error := {}
var _map_recovery_blocked := false
var _map_recovery_details := {}


func configure(interface: EditorInterface) -> void:
	editor_interface = interface
	call_deferred("_recover_incomplete_map_transaction")


func begin_batch(description: String) -> void:
	abort_batch()
	_batch_desc = description
	_ops.clear()
	_active = true


func has_active_batch() -> bool:
	return _active


func has_active_map_transaction() -> bool:
	return _active and _map_transaction_id != ""


func active_map_transaction_id() -> String:
	return _map_transaction_id


func map_recovery_status() -> Dictionary:
	return {
		"ok": not _map_recovery_blocked,
		"blocked": _map_recovery_blocked,
		"details": _map_recovery_details.duplicate(true),
	}


func ensure_map_recovery_ready() -> Dictionary:
	if _map_recovery_blocked:
		_recover_incomplete_map_transaction()
	if _map_recovery_blocked:
		return {
			"ok": false,
			"error_code": "map_transaction_recovery_required",
			"message": (
				"Automatic map writes are blocked because an incomplete transaction "
				+ "could not be recovered safely."
			),
			"recovery": _map_recovery_details.duplicate(true),
		}
	return {"ok": true}


func prepare_map_write_group(
	transaction_id: String,
	description: String,
	target: String,
	base_revision: int
) -> Dictionary:
	var recovery := ensure_map_recovery_ready()
	if not bool(recovery.get("ok", false)):
		return recovery
	var normalized_id := transaction_id.strip_edges()
	if normalized_id == "":
		return {
			"ok": false,
			"error_code": "map_transaction_id_required",
			"message": "Approved map write groups require map_transaction_id.",
		}
	if has_active_map_transaction():
		if _map_transaction_id != normalized_id:
			return {
				"ok": false,
				"error_code": "map_transaction_conflict",
				"message": "A different map write group is still open.",
				"active_map_transaction_id": _map_transaction_id,
			}
		if _map_transaction_target != target:
			return {
				"ok": false,
				"error_code": "map_transaction_target_mismatch",
				"message": "A map write group cannot span different targets.",
				"expected_target": _map_transaction_target,
				"actual_target": target,
			}
	else:
		if _active:
			abort_batch()
		_batch_desc = description
		_ops.clear()
		_active = true
		_map_transaction_id = normalized_id
		_map_transaction_target = target
		_map_transaction_base_revision = base_revision
		_map_transaction_started_at_ms = Time.get_ticks_msec()
		_map_transaction_tool_count = 0
		_map_transaction_snapshot_bytes = 0
		_map_transaction_journal_sequence = 0
		_map_transaction_latest_revision = base_revision
		_map_transaction_scene_snapshot = _capture_scene_before_snapshot(normalized_id)
		_map_transaction_error = {}
		var journal_error := _persist_map_transaction_journal("prepared")
		if journal_error != OK:
			_map_recovery_blocked = true
			_map_recovery_details = {
				"transaction_id": normalized_id,
				"error_code": "map_transaction_journal_write_failed",
				"error": journal_error,
				"journal_dir": MAP_TRANSACTION_JOURNAL_DIR,
			}
			_clear()
			return {
				"ok": false,
				"error_code": "map_transaction_journal_write_failed",
				"message": "Could not persist the map transaction before snapshot.",
				"error": journal_error,
				"recovery": _map_recovery_details.duplicate(true),
			}
	_map_transaction_tool_count += 1
	var limit_error := MapTransactionPolicy.validate_group_limits(
		_map_transaction_started_at_ms,
		_map_transaction_tool_count,
		_map_transaction_snapshot_bytes
	)
	if not limit_error.is_empty():
		_map_transaction_error = limit_error
		abort_batch()
		return limit_error
	return {"ok": true, "map_transaction_id": _map_transaction_id}


func update_map_transaction_revision(transaction_id: String, revision: int) -> void:
	if has_active_map_transaction() and transaction_id == _map_transaction_id:
		_map_transaction_latest_revision = revision


func map_transaction_error() -> Dictionary:
	return _map_transaction_error.duplicate(true)


func set_batch_description(description: String) -> void:
	if _active:
		_batch_desc = description


func record_file_write(path: String, before_text: String, after_text: String, before_exists: bool = true) -> Error:
	if not _active:
		begin_batch("AI file changes")
	# 必须先确认底层写入真的成功了，再把这次修改记入 undo batch。否则只读目录、
	# 磁盘满、文件被占用时 `_write_file_text` 只会写一行日志就返回，上层却照样
	# 报告 ok，模型会基于一个并不存在的改动继续工作（§工具写失败被当成 applied）。
	var op := {
		"type": "file_write",
		"path": path,
		"before": before_text,
		"after": after_text,
		"before_exists": before_exists
	}
	var snapshot_error := _append_operation(op)
	if snapshot_error != OK:
		return snapshot_error
	var error := _write_file_text(path, after_text)
	if error != OK:
		return error
	return OK


func record_binary_file_write(path: String, before_bytes: PackedByteArray, after_bytes: PackedByteArray, before_exists: bool) -> Error:
	if not _active:
		begin_batch("AI resource changes")
	var op := {
		"type": "binary_file_write",
		"path": path,
		"before": before_bytes,
		"after": after_bytes,
		"before_exists": before_exists
	}
	var snapshot_error := _append_operation(op)
	if snapshot_error != OK:
		return snapshot_error
	var error := _write_file_bytes(path, after_bytes, true)
	if error != OK:
		return error
	return OK


func record_node_added(parent: Node, node: Node, owner: Node) -> void:
	if not _active:
		begin_batch("AI scene changes")
	_append_operation({
		"type": "node_add",
		"parent": parent,
		"node": node,
		"owner": owner
	})


func record_node_property(node: Object, property: String, before_value: Variant, after_value: Variant) -> void:
	if not _active:
		begin_batch("AI scene property changes")
	node.set(property, after_value)
	_append_operation({
		"type": "node_property",
		"node": node,
		"property": property,
		"before": before_value,
		"after": after_value
	})


func record_tile_cells(layer: Node, before_cells: Array, after_cells: Array) -> void:
	if not _active:
		begin_batch("AI tilemap changes")
	var op := {
		"type": "tile_cells",
		"layer": layer,
		"before": before_cells,
		"after": after_cells
	}
	var snapshot_error := _append_operation(op)
	if snapshot_error != OK:
		_map_transaction_error = {
			"ok": false,
			"error_code": "map_transaction_journal_write_failed",
			"message": "Map mutation was not applied because its before snapshot could not be journaled.",
			"error": snapshot_error,
		}
		return
	_set_tile_cells(layer, after_cells)


func record_node_removed(parent: Node, node: Node, index: int) -> void:
	if not _active:
		begin_batch("AI scene changes")
	var owner := node.owner
	_remove_node(parent, node)
	_append_operation({
		"type": "node_remove",
		"parent": parent,
		"node": node,
		"owner": owner,
		"index": index
	})


func record_node_reparented(node: Node, old_parent: Node, old_index: int, new_parent: Node, owner: Node) -> void:
	if not _active:
		begin_batch("AI scene changes")
	_reparent_node(node, new_parent, owner)
	_append_operation({
		"type": "node_reparent",
		"node": node,
		"old_parent": old_parent,
		"old_index": old_index,
		"new_parent": new_parent,
		"owner": owner
	})


func record_signal_connected(source: Object, signal_name: String, target: Object, method_name: String) -> void:
	if not _active:
		begin_batch("AI signal changes")
	_connect_signal(source, signal_name, target, method_name)
	_append_operation({
		"type": "signal_connect",
		"source": source,
		"signal": signal_name,
		"target": target,
		"method": method_name
	})


func record_signal_disconnected(source: Object, signal_name: String, target: Object, method_name: String) -> void:
	if not _active:
		begin_batch("AI signal changes")
	_disconnect_signal(source, signal_name, target, method_name)
	_append_operation({
		"type": "signal_disconnect",
		"source": source,
		"signal": signal_name,
		"target": target,
		"method": method_name
	})


func record_group_added(node: Node, group: String) -> void:
	if not _active:
		begin_batch("AI group changes")
	_add_to_group(node, group)
	_append_operation({"type": "group_add", "node": node, "group": group})


func record_group_removed(node: Node, group: String) -> void:
	if not _active:
		begin_batch("AI group changes")
	_remove_from_group(node, group)
	_append_operation({"type": "group_remove", "node": node, "group": group})


func record_animation_track(animation: Resource, track_path: String, before_snapshot: Variant, after_snapshot: Variant) -> void:
	if not _active:
		begin_batch("AI animation changes")
	_append_operation({
		"type": "animation_track",
		"animation": animation,
		"track_path": track_path,
		"before": before_snapshot,
		"after": after_snapshot
	})


func record_project_setting(key: String, before_value: Variant, after_value: Variant) -> void:
	if not _active:
		begin_batch("AI project setting changes")
	_set_project_setting(key, after_value)
	_append_operation({
		"type": "project_setting",
		"key": key,
		"before": before_value,
		"after": after_value
	})


func record_node_renamed(node: Node, before_name: String, after_name: String) -> void:
	if not _active:
		begin_batch("AI scene changes")
	_rename_node(node, after_name)
	_append_operation({
		"type": "node_rename",
		"node": node,
		"before": before_name,
		"after": after_name
	})


func commit_batch() -> void:
	## approved write group 只能由同 target/revision 的 validator 显式提交。
	## ChatPanel 的轮次结束 commit 不得越过这个边界。
	if has_active_map_transaction():
		return
	_commit_active_batch()


func commit_map_write_group(
	transaction_id: String,
	target: String,
	revision: int
) -> Dictionary:
	if not has_active_map_transaction():
		return {
			"ok": false,
			"error_code": "map_transaction_not_open",
			"message": "No approved map write group is open.",
		}
	if transaction_id != _map_transaction_id:
		var mismatch := {
			"ok": false,
			"error_code": "map_transaction_id_mismatch",
			"message": "Validator transaction id does not match the open write group.",
			"expected_map_transaction_id": _map_transaction_id,
			"actual_map_transaction_id": transaction_id,
		}
		_map_transaction_error = mismatch
		abort_batch()
		return mismatch
	if target != _map_transaction_target:
		var target_mismatch := {
			"ok": false,
			"error_code": "map_transaction_target_mismatch",
			"message": "Validator target does not match the open write group.",
			"expected_target": _map_transaction_target,
			"actual_target": target,
		}
		_map_transaction_error = target_mismatch
		abort_batch()
		return target_mismatch
	var latest_revision := _transaction_latest_revision()
	if revision != latest_revision:
		var revision_mismatch := {
			"ok": false,
			"error_code": "map_transaction_revision_mismatch",
			"message": "Validator revision does not match the write group's final revision.",
			"expected_revision": latest_revision,
			"actual_revision": revision,
		}
		_map_transaction_error = revision_mismatch
		abort_batch()
		return revision_mismatch
	var committed_id := _map_transaction_id
	var journal_error := _persist_map_transaction_journal("committing")
	if journal_error != OK:
		var persistence_error := {
			"ok": false,
			"error_code": "map_transaction_journal_write_failed",
			"message": "Map write group was reverted because commit state could not be persisted.",
			"error": journal_error,
		}
		_map_transaction_error = persistence_error
		abort_batch()
		return persistence_error
	_commit_active_batch()
	_delete_map_transaction_journals(committed_id)
	return {
		"ok": true,
		"map_transaction_id": committed_id,
		"map_transaction_status": "committed",
		"map_revision": revision,
	}


func abort_map_write_group(transaction_id: String, reason: String) -> Dictionary:
	if not has_active_map_transaction():
		return {
			"ok": true,
			"map_transaction_id": transaction_id,
			"map_transaction_status": "rolled_back",
			"reason": reason,
		}
	if transaction_id != "" and transaction_id != _map_transaction_id:
		return {
			"ok": false,
			"error_code": "map_transaction_id_mismatch",
			"message": "Cannot abort a different map transaction.",
			"active_map_transaction_id": _map_transaction_id,
		}
	var aborted_id := _map_transaction_id
	abort_batch()
	return {
		"ok": true,
		"map_transaction_id": aborted_id,
		"map_transaction_status": "rolled_back",
		"reason": reason,
	}


func _commit_active_batch() -> void:
	if not _active:
		return
	if undo_redo == null or _ops.is_empty():
		if undo_redo == null and not _ops.is_empty():
			FrontendLogger.warn(editor_interface, "UndoManager", "Discarding batch: EditorUndoRedoManager unavailable.", {
				"description": _batch_desc,
				"ops": _ops.size(),
			})
		_clear()
		return

	FrontendLogger.info(editor_interface, "UndoManager", "Committing undo batch.", {
		"description": _batch_desc,
		"ops": _ops.size(),
	})
	undo_redo.create_action(_batch_desc)
	for op in _ops:
		match op.get("type", ""):
			"file_write":
				undo_redo.add_do_method(self, "_write_file_text", op["path"], op["after"])
				## undo 时使用 _write_file_text_state 而非 _write_file_text，
				## 这样 before_exists=false（本批次新建的文件）会执行删除而非写入空内容，
				## 确保撤销操作真正恢复到批次开始前的文件系统状态。
				undo_redo.add_undo_method(
					self,
					"_write_file_text_state",
					op["path"],
					op["before"],
					op["before_exists"]
				)
			"binary_file_write":
				undo_redo.add_do_method(self, "_write_file_bytes", op["path"], op["after"], true)
				undo_redo.add_undo_method(self, "_write_file_bytes", op["path"], op["before"], op["before_exists"])
			"node_add":
				undo_redo.add_do_method(self, "_add_node", op["parent"], op["node"], op["owner"])
				undo_redo.add_undo_method(self, "_remove_node", op["parent"], op["node"])
				undo_redo.add_do_reference(op["node"])
			"node_property":
				undo_redo.add_do_method(op["node"], "set", op["property"], op["after"])
				undo_redo.add_undo_method(op["node"], "set", op["property"], op["before"])
			"tile_cells":
				undo_redo.add_do_method(self, "_set_tile_cells", op["layer"], op["after"])
				undo_redo.add_undo_method(self, "_set_tile_cells", op["layer"], op["before"])
			"node_remove":
				undo_redo.add_do_method(self, "_remove_node", op["parent"], op["node"])
				undo_redo.add_undo_method(self, "_add_node_at", op["parent"], op["node"], op["owner"], op["index"])
				undo_redo.add_undo_reference(op["node"])
			"node_reparent":
				undo_redo.add_do_method(self, "_reparent_node", op["node"], op["new_parent"], op["owner"])
				undo_redo.add_undo_method(self, "_reparent_node_to", op["node"], op["old_parent"], op["old_index"], op["owner"])
			"node_rename":
				undo_redo.add_do_method(self, "_rename_node", op["node"], op["after"])
				undo_redo.add_undo_method(self, "_rename_node", op["node"], op["before"])
			"signal_connect":
				undo_redo.add_do_method(self, "_connect_signal", op["source"], op["signal"], op["target"], op["method"])
				undo_redo.add_undo_method(self, "_disconnect_signal", op["source"], op["signal"], op["target"], op["method"])
			"signal_disconnect":
				undo_redo.add_do_method(self, "_disconnect_signal", op["source"], op["signal"], op["target"], op["method"])
				undo_redo.add_undo_method(self, "_connect_signal", op["source"], op["signal"], op["target"], op["method"])
			"group_add":
				undo_redo.add_do_method(self, "_add_to_group", op["node"], op["group"])
				undo_redo.add_undo_method(self, "_remove_from_group", op["node"], op["group"])
			"group_remove":
				undo_redo.add_do_method(self, "_remove_from_group", op["node"], op["group"])
				undo_redo.add_undo_method(self, "_add_to_group", op["node"], op["group"])
			"project_setting":
				undo_redo.add_do_method(self, "_set_project_setting", op["key"], op["after"])
				undo_redo.add_undo_method(self, "_set_project_setting", op["key"], op["before"])
			"animation_track":
				undo_redo.add_do_method(self, "_apply_animation_track", op["animation"], op["track_path"], op["after"])
				undo_redo.add_undo_method(self, "_apply_animation_track", op["animation"], op["track_path"], op["before"])
	undo_redo.commit_action(false)
	_clear()


func abort_batch() -> void:
	if not _active:
		return
	FrontendLogger.warn(editor_interface, "UndoManager", "Aborting undo batch; reverting recorded ops.", {
		"description": _batch_desc,
		"ops": _ops.size(),
	})
	for index in range(_ops.size() - 1, -1, -1):
		var op: Dictionary = _ops[index]
		match op.get("type", ""):
			"file_write":
				## abort 时同样使用 _write_file_text_state，对新建文件执行删除而非写空。
				_write_file_text_state(
					str(op["path"]),
					str(op["before"]),
					bool(op.get("before_exists", true))
				)
			"binary_file_write":
				_write_file_bytes(str(op["path"]), op["before"], bool(op["before_exists"]))
			"node_add":
				var parent: Object = op["parent"]
				var node: Object = op["node"]
				if is_instance_valid(parent) and is_instance_valid(node):
					_remove_node(parent, node)
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of node_add: parent or node is no longer valid.")
			"node_property":
				var node: Object = op["node"]
				if is_instance_valid(node) and node.has_method("set"):
					node.set(op["property"], op["before"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of node_property: node is no longer valid.")
			"tile_cells":
				var layer: Object = op["layer"]
				if is_instance_valid(layer) and (layer.has_method("set_cell") or layer.has_method("set_cell_item")):
					_set_tile_cells(layer, op["before"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of tile_cells: layer is no longer valid.")
			"node_remove":
				var parent: Object = op["parent"]
				var node: Object = op["node"]
				if is_instance_valid(parent) and is_instance_valid(node):
					_add_node_at(parent, node, op["owner"], op["index"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of node_remove: parent or node is no longer valid.")
			"node_reparent":
				var node: Object = op["node"]
				var old_parent: Object = op["old_parent"]
				if is_instance_valid(node) and is_instance_valid(old_parent):
					_reparent_node_to(node, old_parent, op["old_index"], op["owner"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of node_reparent: node or old parent is no longer valid.")
			"node_rename":
				var node: Object = op["node"]
				if is_instance_valid(node):
					_rename_node(node, op["before"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of node_rename: node is no longer valid.")
			"signal_connect":
				var conn_source: Object = op["source"]
				var conn_target: Object = op["target"]
				if is_instance_valid(conn_source) and is_instance_valid(conn_target):
					_disconnect_signal(conn_source, op["signal"], conn_target, op["method"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of signal_connect: source or target is no longer valid.")
			"signal_disconnect":
				var dconn_source: Object = op["source"]
				var dconn_target: Object = op["target"]
				if is_instance_valid(dconn_source) and is_instance_valid(dconn_target):
					_connect_signal(dconn_source, op["signal"], dconn_target, op["method"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of signal_disconnect: source or target is no longer valid.")
			"group_add":
				var add_node_obj: Object = op["node"]
				if is_instance_valid(add_node_obj):
					_remove_from_group(add_node_obj, op["group"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of group_add: node is no longer valid.")
			"group_remove":
				var remove_node_obj: Object = op["node"]
				if is_instance_valid(remove_node_obj):
					_add_to_group(remove_node_obj, op["group"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of group_remove: node is no longer valid.")
			"project_setting":
				_set_project_setting(str(op["key"]), op["before"])
			"animation_track":
				var anim_obj: Object = op["animation"]
				if is_instance_valid(anim_obj):
					_apply_animation_track(anim_obj, str(op["track_path"]), op["before"])
				else:
					FrontendLogger.warn(editor_interface, "UndoManager", "Skipping undo of animation_track: animation is no longer valid.")
	var transaction_id := _map_transaction_id
	_clear()
	if transaction_id != "":
		_delete_map_transaction_journals(transaction_id)


func _append_operation(op: Dictionary) -> Error:
	_ops.append(op)
	if not has_active_map_transaction():
		return OK
	_map_transaction_snapshot_bytes = _journal_snapshot_size()
	var limit_error := MapTransactionPolicy.validate_group_limits(
		_map_transaction_started_at_ms,
		_map_transaction_tool_count,
		_map_transaction_snapshot_bytes
	)
	if not limit_error.is_empty():
		_ops.pop_back()
		_map_transaction_error = limit_error
		return ERR_OUT_OF_MEMORY
	var journal_error := _persist_map_transaction_journal("prepared")
	if journal_error != OK:
		_ops.pop_back()
		_map_transaction_error = {
			"ok": false,
			"error_code": "map_transaction_journal_write_failed",
			"message": "Could not persist a map before snapshot.",
			"error": journal_error,
		}
	return journal_error


func _transaction_latest_revision() -> int:
	return _map_transaction_latest_revision


func _capture_scene_before_snapshot(transaction_id: String) -> Dictionary:
	if editor_interface == null:
		return {}
	var root := editor_interface.get_edited_scene_root()
	if root == null or str(root.scene_file_path).strip_edges() == "":
		return {}
	var dir_error := DirAccess.make_dir_recursive_absolute(
		ProjectSettings.globalize_path(MAP_TRANSACTION_JOURNAL_DIR)
	)
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		return {}
	var packed := PackedScene.new()
	if packed.pack(root) != OK:
		return {}
	var safe_id := transaction_id.validate_filename()
	var snapshot_path := "%s/%s.scene-before.tscn" % [
		MAP_TRANSACTION_JOURNAL_DIR,
		safe_id,
	]
	var save_error := ResourceSaver.save(packed, snapshot_path)
	if save_error != OK:
		return {}
	var snapshot_bytes := FileAccess.get_file_as_bytes(
		ProjectSettings.globalize_path(snapshot_path)
	)
	return {
		"scene_path": str(root.scene_file_path),
		"snapshot_path": snapshot_path,
		"sha256": _sha256_bytes(snapshot_bytes),
		"bytes": snapshot_bytes.size(),
	}


func _journal_snapshot_size() -> int:
	var total := int(_map_transaction_scene_snapshot.get("bytes", 0))
	for op in _ops:
		match str(op.get("type", "")):
			"file_write":
				total += str(op.get("before", "")).to_utf8_buffer().size()
			"binary_file_write":
				var before_bytes: PackedByteArray = op.get("before", PackedByteArray())
				total += before_bytes.size()
			"tile_cells":
				total += var_to_bytes(op.get("before", [])).size()
			"node_property":
				total += var_to_bytes(op.get("before")).size()
	return total


func _persist_map_transaction_journal(status: String) -> Error:
	if _map_transaction_id == "":
		return OK
	var absolute_dir := ProjectSettings.globalize_path(MAP_TRANSACTION_JOURNAL_DIR)
	var dir_error := DirAccess.make_dir_recursive_absolute(absolute_dir)
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		return dir_error
	_map_transaction_journal_sequence += 1
	var payload := {
		"schema_version": 1,
		"transaction_id": _map_transaction_id,
		"target": _map_transaction_target,
		"base_revision": _map_transaction_base_revision,
		"latest_revision": _map_transaction_latest_revision,
		"started_at_ms": _map_transaction_started_at_ms,
		"tool_count": _map_transaction_tool_count,
		"snapshot_bytes": _map_transaction_snapshot_bytes,
		"sequence": _map_transaction_journal_sequence,
		"status": status,
		"scene_snapshot": _map_transaction_scene_snapshot.duplicate(true),
		"operations": _serialize_recovery_operations(),
	}
	var envelope := payload.duplicate(true)
	envelope["checksum"] = _sha256_text(JSON.stringify(payload))
	var safe_id := _map_transaction_id.validate_filename()
	var journal_path := "%s/%s.%08d.json" % [
		MAP_TRANSACTION_JOURNAL_DIR,
		safe_id,
		_map_transaction_journal_sequence,
	]
	var file := FileAccess.open(
		ProjectSettings.globalize_path(journal_path),
		FileAccess.WRITE
	)
	if file == null:
		var open_error := FileAccess.get_open_error()
		return open_error if open_error != OK else FAILED
	file.store_string(JSON.stringify(envelope, "\t"))
	file.flush()
	var write_error := file.get_error()
	file.close()
	return write_error if write_error != ERR_FILE_EOF else OK


func _serialize_recovery_operations() -> Array:
	var serialized: Array = []
	for op in _ops:
		var op_type := str(op.get("type", ""))
		match op_type:
			"file_write":
				serialized.append({
					"type": op_type,
					"path": str(op.get("path", "")),
					"before": str(op.get("before", "")),
					"before_exists": bool(op.get("before_exists", true)),
				})
			"binary_file_write":
				serialized.append({
					"type": op_type,
					"path": str(op.get("path", "")),
					"before_base64": Marshalls.raw_to_base64(
						op.get("before", PackedByteArray())
					),
					"before_exists": bool(op.get("before_exists", true)),
				})
			"tile_cells":
				serialized.append({
					"type": op_type,
					"node_path": _node_path_from_scene(op.get("layer")),
					"before_base64": Marshalls.raw_to_base64(
						var_to_bytes(op.get("before", []))
					),
				})
			"node_add":
				serialized.append({
					"type": op_type,
					"node_path": _node_path_from_scene(op.get("node")),
				})
			"node_property", "node_rename", "group_add", "group_remove":
				var before_value = op.get("before")
				serialized.append({
					"type": op_type,
					"node_path": _node_path_from_scene(op.get("node")),
					"property": str(op.get("property", "")),
					"group": str(op.get("group", "")),
					"before_base64": Marshalls.raw_to_base64(var_to_bytes(before_value)),
				})
			_:
				serialized.append({
					"type": op_type,
					"requires_scene_snapshot": true,
				})
	return serialized


func _node_path_from_scene(value: Variant) -> String:
	if editor_interface == null or not (value is Node):
		return ""
	var root := editor_interface.get_edited_scene_root()
	var node: Node = value
	if root == null or not root.is_ancestor_of(node):
		return ""
	return str(root.get_path_to(node))


func _recover_incomplete_map_transaction() -> void:
	var journal_result := _latest_map_transaction_journal()
	if not bool(journal_result.get("found", false)):
		_map_recovery_blocked = false
		_map_recovery_details = {}
		return
	if not bool(journal_result.get("ok", false)):
		_map_recovery_blocked = true
		_map_recovery_details = journal_result
		return
	var journal: Dictionary = journal_result.get("journal", {})
	var transaction_id := str(journal.get("transaction_id", ""))
	var status := str(journal.get("status", ""))
	if status in ["committed", "rolled_back"]:
		_delete_map_transaction_journals(transaction_id)
		_map_recovery_blocked = false
		_map_recovery_details = {}
		return
	var restore_error := _restore_map_transaction_journal(journal)
	if not restore_error.is_empty():
		_map_recovery_blocked = true
		_map_recovery_details = restore_error
		return
	_delete_map_transaction_journals(transaction_id)
	_map_recovery_blocked = false
	_map_recovery_details = {
		"transaction_id": transaction_id,
		"status": "rolled_back_after_restart",
	}


func _latest_map_transaction_journal() -> Dictionary:
	var absolute_dir := ProjectSettings.globalize_path(MAP_TRANSACTION_JOURNAL_DIR)
	if not DirAccess.dir_exists_absolute(absolute_dir):
		return {"ok": true, "found": false}
	var files := DirAccess.get_files_at(absolute_dir)
	var journal_files: Array[String] = []
	for file_name in files:
		if str(file_name).ends_with(".json"):
			journal_files.append(str(file_name))
	journal_files.sort()
	if journal_files.is_empty():
		return {"ok": true, "found": false}
	var latest_path := MAP_TRANSACTION_JOURNAL_DIR + "/" + journal_files[-1]
	var text := FileAccess.get_file_as_string(
		ProjectSettings.globalize_path(latest_path)
	)
	var parsed = JSON.parse_string(text)
	if not (parsed is Dictionary):
		return {
			"ok": false,
			"found": true,
			"error_code": "map_transaction_journal_corrupt",
			"message": "The latest map transaction journal is not valid JSON.",
			"journal_path": latest_path,
		}
	var envelope: Dictionary = parsed
	var supplied_checksum := str(envelope.get("checksum", ""))
	var payload := envelope.duplicate(true)
	payload.erase("checksum")
	var actual_checksum := _sha256_text(JSON.stringify(payload))
	if supplied_checksum == "" or supplied_checksum != actual_checksum:
		return {
			"ok": false,
			"found": true,
			"error_code": "map_transaction_journal_checksum_mismatch",
			"message": "The latest map transaction journal checksum is invalid.",
			"journal_path": latest_path,
			"expected_checksum": supplied_checksum,
			"actual_checksum": actual_checksum,
		}
	return {
		"ok": true,
		"found": true,
		"journal": payload,
		"journal_path": latest_path,
	}


func _restore_map_transaction_journal(journal: Dictionary) -> Dictionary:
	var scene_snapshot = journal.get("scene_snapshot", {})
	if scene_snapshot is Dictionary and not scene_snapshot.is_empty():
		var snapshot_path := str(scene_snapshot.get("snapshot_path", ""))
		var absolute_snapshot := ProjectSettings.globalize_path(snapshot_path)
		if not FileAccess.file_exists(absolute_snapshot):
			return _recovery_error(
				journal,
				"map_transaction_snapshot_missing",
				"The scene before-snapshot is missing."
			)
		var snapshot_bytes := FileAccess.get_file_as_bytes(absolute_snapshot)
		if _sha256_bytes(snapshot_bytes) != str(scene_snapshot.get("sha256", "")):
			return _recovery_error(
				journal,
				"map_transaction_snapshot_checksum_mismatch",
				"The scene before-snapshot checksum is invalid."
			)
		var scene_path := str(scene_snapshot.get("scene_path", ""))
		var scene_error := _write_file_bytes(scene_path, snapshot_bytes, true)
		if scene_error != OK:
			return _recovery_error(
				journal,
				"map_transaction_scene_restore_failed",
				"Could not restore the scene before-snapshot.",
				scene_error
			)
	var operations_value = journal.get("operations", [])
	if not (operations_value is Array):
		return _recovery_error(
			journal,
			"map_transaction_operations_missing",
			"Transaction recovery operations are missing."
		)
	var operations: Array = operations_value
	for index in range(operations.size() - 1, -1, -1):
		var op_value = operations[index]
		if not (op_value is Dictionary):
			return _recovery_error(
				journal,
				"map_transaction_operation_corrupt",
				"A transaction recovery operation is invalid."
			)
		var op: Dictionary = op_value
		var restore_error := _restore_journal_operation(op, not scene_snapshot.is_empty())
		if restore_error != OK:
			return _recovery_error(
				journal,
				"map_transaction_operation_restore_failed",
				"Could not restore a transaction before snapshot.",
				restore_error
			)
	if (
		editor_interface != null
		and scene_snapshot is Dictionary
		and not scene_snapshot.is_empty()
		and editor_interface.has_method("reload_scene_from_path")
	):
		editor_interface.call(
			"reload_scene_from_path",
			str(scene_snapshot.get("scene_path", ""))
		)
	return {}


func _restore_journal_operation(op: Dictionary, scene_was_restored: bool) -> Error:
	match str(op.get("type", "")):
		"file_write":
			return _write_file_text_state(
				str(op.get("path", "")),
				str(op.get("before", "")),
				bool(op.get("before_exists", true))
			)
		"binary_file_write":
			return _write_file_bytes(
				str(op.get("path", "")),
				Marshalls.base64_to_raw(str(op.get("before_base64", ""))),
				bool(op.get("before_exists", true))
			)
		"tile_cells", "node_add", "node_property", "node_rename", "group_add", "group_remove":
			if scene_was_restored:
				return OK
			return ERR_UNAVAILABLE
		_:
			return OK if scene_was_restored else ERR_UNAVAILABLE


func _recovery_error(
	journal: Dictionary,
	error_code: String,
	message: String,
	error: int = OK
) -> Dictionary:
	return {
		"ok": false,
		"found": true,
		"error_code": error_code,
		"message": message,
		"error": error,
		"transaction_id": str(journal.get("transaction_id", "")),
		"target": str(journal.get("target", "")),
		"base_revision": journal.get("base_revision"),
		"latest_revision": journal.get("latest_revision"),
		"journal_dir": MAP_TRANSACTION_JOURNAL_DIR,
		"recovery_action": (
			"Restore the listed before snapshot manually, then remove only the "
			+ "matching transaction journal files."
		),
	}


func _delete_map_transaction_journals(transaction_id: String) -> void:
	if transaction_id == "":
		return
	var absolute_dir := ProjectSettings.globalize_path(MAP_TRANSACTION_JOURNAL_DIR)
	if not DirAccess.dir_exists_absolute(absolute_dir):
		return
	var safe_id := transaction_id.validate_filename()
	for file_name_value in DirAccess.get_files_at(absolute_dir):
		var file_name := str(file_name_value)
		if not file_name.begins_with(safe_id + "."):
			continue
		var absolute_path := absolute_dir.path_join(file_name)
		var remove_error := DirAccess.remove_absolute(absolute_path)
		if remove_error != OK:
			FrontendLogger.warn(
				editor_interface,
				"UndoManager",
				"Failed to remove a completed map transaction journal.",
				{"path": absolute_path, "error": remove_error}
			)


func _sha256_text(value: String) -> String:
	return _sha256_bytes(value.to_utf8_buffer())


func _sha256_bytes(value: PackedByteArray) -> String:
	var context := HashingContext.new()
	context.start(HashingContext.HASH_SHA256)
	context.update(value)
	return context.finish().hex_encode()


func _clear() -> void:
	_batch_desc = ""
	_ops.clear()
	_active = false
	_map_transaction_id = ""
	_map_transaction_target = ""
	_map_transaction_base_revision = 0
	_map_transaction_started_at_ms = 0
	_map_transaction_tool_count = 0
	_map_transaction_snapshot_bytes = 0
	_map_transaction_journal_sequence = 0
	_map_transaction_latest_revision = 0
	_map_transaction_scene_snapshot = {}


func _write_file_text(path: String, text: String) -> Error:
	var absolute := ProjectSettings.globalize_path(path)
	var dir_path := absolute.get_base_dir()
	var dir_error := DirAccess.make_dir_recursive_absolute(dir_path)
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		FrontendLogger.error(editor_interface, "UndoManager", "Failed to create directory.", {"path": path, "error": dir_error})
		return dir_error
	var file := FileAccess.open(absolute, FileAccess.WRITE)
	if file == null:
		var open_error := FileAccess.get_open_error()
		FrontendLogger.error(editor_interface, "UndoManager", "Failed to write file.", {"path": path, "error": open_error})
		return open_error if open_error != OK else FAILED
	file.store_string(text)
	file.flush()
	var write_error := file.get_error()
	file.close()
	if write_error != OK and write_error != ERR_FILE_EOF:
		FrontendLogger.error(editor_interface, "UndoManager", "Failed to write file contents.", {"path": path, "error": write_error})
		return write_error
	if ResourceLoader.exists(path):
		ResourceLoader.load(path, "", ResourceLoader.CACHE_MODE_REPLACE)
	return OK


## 带状态感知的文件文本恢复：exists=true 时覆写文件内容（恢复到修改前的文本）；
## exists=false 时说明该文件是本批次新建的，撤销应直接删除而非写入空字符串。
func _write_file_text_state(path: String, text: String, exists: bool) -> Error:
	if exists:
		return _write_file_text(path, text)
	var absolute := ProjectSettings.globalize_path(path)
	if not FileAccess.file_exists(absolute):
		return OK
	var remove_error := DirAccess.remove_absolute(absolute)
	if remove_error != OK:
		FrontendLogger.error(
			editor_interface,
			"UndoManager",
			"Failed to remove file.",
			{"path": path, "error": remove_error}
		)
	return remove_error


func _write_file_bytes(path: String, bytes: PackedByteArray, exists: bool) -> Error:
	var absolute := ProjectSettings.globalize_path(path)
	if not exists:
		if FileAccess.file_exists(absolute):
			var remove_error := DirAccess.remove_absolute(absolute)
			if remove_error != OK:
				FrontendLogger.error(editor_interface, "UndoManager", "Failed to remove file.", {"path": path, "error": remove_error})
				return remove_error
		return OK
	var dir_error := DirAccess.make_dir_recursive_absolute(absolute.get_base_dir())
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		FrontendLogger.error(editor_interface, "UndoManager", "Failed to create directory.", {"path": path, "error": dir_error})
		return dir_error
	var file := FileAccess.open(absolute, FileAccess.WRITE)
	if file == null:
		var open_error := FileAccess.get_open_error()
		FrontendLogger.error(editor_interface, "UndoManager", "Failed to write file.", {"path": path, "error": open_error})
		return open_error if open_error != OK else FAILED
	file.store_buffer(bytes)
	file.flush()
	var write_error := file.get_error()
	file.close()
	if write_error != OK and write_error != ERR_FILE_EOF:
		FrontendLogger.error(editor_interface, "UndoManager", "Failed to write file contents.", {"path": path, "error": write_error})
		return write_error
	return OK


func _add_node(parent: Node, node: Node, owner: Node) -> void:
	if parent == null or node == null:
		return
	if node.get_parent() != null:
		return
	parent.add_child(node)
	node.owner = owner


func _remove_node(parent: Node, node: Node) -> void:
	if parent == null or node == null:
		return
	if node.get_parent() == parent:
		parent.remove_child(node)


func _add_node_at(parent: Node, node: Node, owner: Node, index: int) -> void:
	if parent == null or node == null:
		return
	if node.get_parent() != null:
		return
	parent.add_child(node)
	node.owner = owner
	if index >= 0 and index < parent.get_child_count():
		parent.move_child(node, index)


func _reparent_node(node: Node, new_parent: Node, owner: Node) -> void:
	if node == null or new_parent == null:
		return
	var old_parent := node.get_parent()
	if old_parent == new_parent:
		return
	if old_parent != null:
		old_parent.remove_child(node)
	new_parent.add_child(node)
	node.owner = owner


func _reparent_node_to(node: Node, parent: Node, index: int, owner: Node) -> void:
	_reparent_node(node, parent, owner)
	if parent != null and node != null and index >= 0 and index < parent.get_child_count():
		parent.move_child(node, index)


func _rename_node(node: Node, new_name: String) -> void:
	if node == null:
		return
	node.name = new_name


func _connect_signal(source: Object, signal_name: String, target: Object, method_name: String) -> void:
	if source == null or target == null:
		return
	var callable := Callable(target, method_name)
	if not source.is_connected(signal_name, callable):
		source.connect(signal_name, callable, CONNECT_PERSIST)


func _disconnect_signal(source: Object, signal_name: String, target: Object, method_name: String) -> void:
	if source == null or target == null:
		return
	var callable := Callable(target, method_name)
	if source.is_connected(signal_name, callable):
		source.disconnect(signal_name, callable)


func _add_to_group(node: Node, group: String) -> void:
	if node == null:
		return
	node.add_to_group(group, true)


func _remove_from_group(node: Node, group: String) -> void:
	if node == null:
		return
	node.remove_from_group(group)


func _apply_animation_track(animation: Animation, track_path: String, snapshot: Variant) -> void:
	if animation == null:
		return
	for i in range(animation.get_track_count() - 1, -1, -1):
		if animation.track_get_type(i) == Animation.TYPE_VALUE and str(animation.track_get_path(i)) == track_path:
			animation.remove_track(i)
	if snapshot == null:
		return
	var data: Dictionary = snapshot
	var index := animation.add_track(Animation.TYPE_VALUE)
	animation.track_set_path(index, NodePath(str(data.get("path", track_path))))
	animation.track_set_interpolation_type(index, int(data.get("interpolation", Animation.INTERPOLATION_LINEAR)))
	for key in data.get("keys", []):
		animation.track_insert_key(index, float(key.get("time", 0.0)), key.get("value"), float(key.get("transition", 1.0)))


func _set_project_setting(key: String, value: Variant) -> void:
	ProjectSettings.set_setting(key, value)
	ProjectSettings.save()


func _set_tile_cells(layer: Node, cells: Array) -> void:
	if layer == null or (not layer.has_method("set_cell") and not layer.has_method("set_cell_item")):
		return
	for cell in cells:
		if not (cell is Dictionary):
			continue
		var coords = cell.get("coords", Vector3i.ZERO)
		if layer.get_class() == "GridMap":
			layer.call("set_cell_item", coords, int(cell.get("item", -1)), int(cell.get("orientation", 0)))
		elif layer.get_class() == "TileMap":
			layer.call(
				"set_cell",
				int(cell.get("map_layer", 0)),
				Vector2i(coords.x, coords.y),
				int(cell.get("source_id", -1)),
				cell.get("atlas_coords", Vector2i(-1, -1)),
				int(cell.get("alternative_tile", 0))
			)
		else:
			layer.call(
				"set_cell",
				Vector2i(coords.x, coords.y),
				int(cell.get("source_id", -1)),
				cell.get("atlas_coords", Vector2i(-1, -1)),
				int(cell.get("alternative_tile", 0))
			)
