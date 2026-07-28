@tool
extends Node

# 地图 revision 的唯一前端状态实现。
# - 每 POLL_INTERVAL_SECONDS 秒对当前编辑场景做轻量兜底扫描；
# - 连接 Undo/Redo 的 version_changed / history_changed 信号提前触发扫描；
# - 为 TileMapLayer、旧 TileMap 每层、GridMap 生成稳定内容指纹
#   （canonical 节点路径 + 地图资源身份 + 排序后的 cell 内容）；
# - 通过 _controlled_write_depth 守卫区分 agent 受控写入与外部手工编辑，
#   避免把同一次工具写入重复计为 revision 变化。
# 外部手工修改或用户 Undo/Redo 推进 revision 后，旧 planning contract
# 会在下一次写入时发生冲突并要求重读。

const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

const REVISIONS_PATH := "res://.ai_agent_service/map_agent/revisions.json"
const POLL_INTERVAL_SECONDS := 0.5

var editor_interface: EditorInterface

var _revisions: Dictionary = {}
var _scene_token := ""
var _fingerprints: Dictionary = {}
var _snapshot_initialized := false
var _poll_elapsed := 0.0
var _scan_requested := true
var _controlled_write_depth := 0


func configure(interface: EditorInterface, undo_redo_manager: Object = null) -> void:
	editor_interface = interface
	_connect_undo_signals(undo_redo_manager)
	_scan_requested = true
	set_process(true)


func _process(delta: float) -> void:
	if not Engine.is_editor_hint() or editor_interface == null:
		return
	_poll_elapsed += delta
	if not _scan_requested and _poll_elapsed < POLL_INTERVAL_SECONDS:
		return
	_poll_elapsed = 0.0
	_scan_requested = false
	_scan_for_external_changes()


func begin_controlled_write() -> void:
	_controlled_write_depth += 1


func end_controlled_write() -> void:
	_controlled_write_depth = maxi(0, _controlled_write_depth - 1)
	if _controlled_write_depth == 0:
		_capture_current_scene()


func current_revision(key: String) -> int:
	_load_revisions()
	return int(_revisions.get(key, 0))


func advance_controlled_write(key: String, undo_manager: Node) -> Dictionary:
	_load_revisions()
	var previous_revision := int(_revisions.get(key, 0))
	var next_revision := previous_revision + 1
	var previous_revisions := _revisions.duplicate(true)
	_revisions[key] = next_revision
	var error := _persist_revisions(undo_manager)
	if error != OK:
		_revisions = previous_revisions
		return {
			"ok": false,
			"error": error,
			"previous_revision": previous_revision,
			"next_revision": previous_revision,
		}
	return {
		"ok": true,
		"error": OK,
		"previous_revision": previous_revision,
		"next_revision": next_revision,
	}


func _connect_undo_signals(undo_redo_manager: Object) -> void:
	if undo_redo_manager == null:
		return
	for signal_name in ["version_changed", "history_changed"]:
		if not undo_redo_manager.has_signal(signal_name):
			continue
		var callback := Callable(self, "_request_scan")
		if not undo_redo_manager.is_connected(signal_name, callback):
			undo_redo_manager.connect(signal_name, callback)


func _request_scan() -> void:
	_scan_requested = true


func _scan_for_external_changes() -> void:
	if _controlled_write_depth > 0:
		return
	var snapshot := _scene_snapshot()
	var token := str(snapshot.get("scene_token", ""))
	var current_fingerprints: Dictionary = snapshot.get("fingerprints", {})
	if token == "":
		_scene_token = ""
		_fingerprints.clear()
		_snapshot_initialized = false
		return
	if token != _scene_token or not _snapshot_initialized:
		_scene_token = token
		_fingerprints = current_fingerprints
		_snapshot_initialized = true
		return
	var changed_keys := _changed_keys(_fingerprints, current_fingerprints)
	if changed_keys.is_empty():
		return
	_load_revisions()
	var previous_revisions := _revisions.duplicate(true)
	for key in changed_keys:
		_revisions[key] = int(_revisions.get(key, 0)) + 1
	var error := _persist_revisions(null)
	if error != OK:
		_revisions = previous_revisions
		_scan_requested = true
		FrontendLogger.warn(
			editor_interface,
			"MapRevisionTracker",
			"Failed to persist revisions for an external map edit.",
			{"error": error, "keys": changed_keys}
		)
		return
	_fingerprints = current_fingerprints
	FrontendLogger.info(
		editor_interface,
		"MapRevisionTracker",
		"Detected an external map edit and advanced revisions.",
		{"keys": changed_keys}
	)


func _capture_current_scene() -> void:
	var snapshot := _scene_snapshot()
	_scene_token = str(snapshot.get("scene_token", ""))
	_fingerprints = snapshot.get("fingerprints", {})
	_snapshot_initialized = _scene_token != ""
	_scan_requested = false
	_poll_elapsed = 0.0


func _scene_snapshot() -> Dictionary:
	if editor_interface == null:
		return {"scene_token": "", "fingerprints": {}}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"scene_token": "", "fingerprints": {}}
	var scene_path := str(root.scene_file_path)
	var token := scene_path if scene_path != "" else "instance:%d" % root.get_instance_id()
	var map_nodes: Array = []
	_collect_map_nodes(root, map_nodes)
	var fingerprints := {}
	for node_value in map_nodes:
		var node: Node = node_value
		var path := str(root.get_path_to(node))
		match node.get_class():
			"TileMapLayer":
				fingerprints[path] = _tile_map_layer_fingerprint(node, path)
			"TileMap":
				var layer_count := int(node.call("get_layers_count"))
				for layer in range(layer_count):
					var key := "%s::map_layer=%d" % [path, layer]
					fingerprints[key] = _tile_map_fingerprint(node, path, layer)
			"GridMap":
				fingerprints[path] = _grid_map_fingerprint(node, path)
	return {"scene_token": token, "fingerprints": fingerprints}


func _collect_map_nodes(node: Node, output: Array) -> void:
	if node.get_class() in ["TileMapLayer", "TileMap", "GridMap"]:
		output.append(node)
	for child in node.get_children():
		_collect_map_nodes(child, output)


func _tile_map_layer_fingerprint(node: Node, path: String) -> String:
	var rows: PackedStringArray = []
	for coords_value in node.call("get_used_cells"):
		var coords: Vector2i = coords_value
		rows.append(
			"%d,%d:%d:%d,%d:%d" % [
				coords.x,
				coords.y,
				int(node.call("get_cell_source_id", coords)),
				int(node.call("get_cell_atlas_coords", coords).x),
				int(node.call("get_cell_atlas_coords", coords).y),
				int(node.call("get_cell_alternative_tile", coords)),
			]
		)
	rows.sort()
	return _hash_rows([
		"class=TileMapLayer",
		"path=" + path,
		"tile_set=" + _resource_identity(node.get("tile_set")),
		"cells=" + "|".join(rows),
	])


func _tile_map_fingerprint(node: Node, path: String, layer: int) -> String:
	var rows: PackedStringArray = []
	for coords_value in node.call("get_used_cells", layer):
		var coords: Vector2i = coords_value
		var atlas: Vector2i = node.call("get_cell_atlas_coords", layer, coords)
		rows.append(
			"%d,%d:%d:%d,%d:%d" % [
				coords.x,
				coords.y,
				int(node.call("get_cell_source_id", layer, coords)),
				atlas.x,
				atlas.y,
				int(node.call("get_cell_alternative_tile", layer, coords)),
			]
		)
	rows.sort()
	return _hash_rows([
		"class=TileMap",
		"path=" + path,
		"layer=%d" % layer,
		"tile_set=" + _resource_identity(node.get("tile_set")),
		"cells=" + "|".join(rows),
	])


func _grid_map_fingerprint(node: Node, path: String) -> String:
	var rows: PackedStringArray = []
	for coords_value in node.call("get_used_cells"):
		var coords: Vector3i = coords_value
		rows.append(
			"%d,%d,%d:%d:%d" % [
				coords.x,
				coords.y,
				coords.z,
				int(node.call("get_cell_item", coords)),
				int(node.call("get_cell_item_orientation", coords)),
			]
		)
	rows.sort()
	return _hash_rows([
		"class=GridMap",
		"path=" + path,
		"mesh_library=" + _resource_identity(node.get("mesh_library")),
		"cells=" + "|".join(rows),
	])


func _resource_identity(value: Variant) -> String:
	if not (value is Resource):
		return ""
	var resource: Resource = value
	if resource.resource_path != "":
		return resource.resource_path
	return "instance:%d" % resource.get_instance_id()


func _hash_rows(rows: Array) -> String:
	var context := HashingContext.new()
	context.start(HashingContext.HASH_SHA256)
	context.update("\n".join(rows).to_utf8_buffer())
	return context.finish().hex_encode()


func _changed_keys(before: Dictionary, after: Dictionary) -> Array[String]:
	var key_set := {}
	for key in before.keys():
		key_set[str(key)] = true
	for key in after.keys():
		key_set[str(key)] = true
	var keys: Array[String] = []
	for key in key_set.keys():
		if str(before.get(key, "")) != str(after.get(key, "")):
			keys.append(str(key))
	keys.sort()
	return keys


func _load_revisions() -> void:
	_revisions.clear()
	var absolute := ProjectSettings.globalize_path(REVISIONS_PATH)
	if not FileAccess.file_exists(absolute):
		return
	var parsed = JSON.parse_string(FileAccess.get_file_as_string(absolute))
	if not (parsed is Dictionary):
		return
	for key in parsed.keys():
		var value = parsed.get(key, 0)
		if value is int or value is float:
			_revisions[str(key)] = int(value)


func _persist_revisions(undo_manager: Node) -> Error:
	var absolute := ProjectSettings.globalize_path(REVISIONS_PATH)
	var before_exists := FileAccess.file_exists(absolute)
	var before_text := FileAccess.get_file_as_string(absolute) if before_exists else ""
	var after_text := JSON.stringify(_revisions, "\t")
	if undo_manager != null:
		return undo_manager.record_file_write(REVISIONS_PATH, before_text, after_text, before_exists)
	var dir_error := DirAccess.make_dir_recursive_absolute(absolute.get_base_dir())
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		return dir_error
	var file := FileAccess.open(absolute, FileAccess.WRITE)
	if file == null:
		var open_error := FileAccess.get_open_error()
		return open_error if open_error != OK else FAILED
	file.store_string(after_text)
	file.flush()
	var write_error := file.get_error()
	file.close()
	return write_error if write_error != ERR_FILE_EOF else OK
