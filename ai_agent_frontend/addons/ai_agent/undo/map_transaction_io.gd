@tool
extends RefCounted

## 地图事务 journal/snapshot 的窄文件系统边界。
##
## 生产实例不配置 failpoint，所有调用直接委派 Godot FileAccess、
## DirAccess 与 ResourceSaver。测试实例只能由代码构造并注入命名计数器；
## 工具请求和 journal 数据都不会被读取为 failpoint 配置。

const FAILPOINTS := [
	"snapshot_before_save",
	"snapshot_after_save",
	"snapshot_before_read",
	"snapshot_after_read",
	"journal_prepared_before_write",
	"journal_prepared_after_write",
	"journal_applying_before_write",
	"journal_applying_after_write",
	"journal_committing_before_write",
	"journal_committing_after_write",
	"journal_committed_before_write",
	"journal_committed_after_write",
	"journal_rolled_back_before_write",
	"journal_rolled_back_after_write",
	"commit_before_apply",
	"commit_after_apply",
	"cleanup_before_delete",
	"cleanup_after_delete",
	"restore_before_write",
	"restore_after_write",
]

var _test_failpoints: Dictionary = {}


func configure_test_failpoints(failpoints: Dictionary) -> void:
	## 配置测试专用的一次或多次命名故障；生产组合不调用此入口。
	_test_failpoints.clear()
	for name_value in failpoints:
		var name := str(name_value)
		var count := int(failpoints.get(name_value, 0))
		if name in FAILPOINTS and count > 0:
			_test_failpoints[name] = count


func production_failpoints_disabled() -> bool:
	return _test_failpoints.is_empty()


func hit(name: String) -> Error:
	if name not in FAILPOINTS:
		return ERR_INVALID_PARAMETER
	var remaining := int(_test_failpoints.get(name, 0))
	if remaining <= 0:
		return OK
	if remaining == 1:
		_test_failpoints.erase(name)
	else:
		_test_failpoints[name] = remaining - 1
	return ERR_CANT_CREATE


func make_dir_recursive(absolute_path: String) -> Error:
	return DirAccess.make_dir_recursive_absolute(absolute_path)


func dir_exists(absolute_path: String) -> bool:
	return DirAccess.dir_exists_absolute(absolute_path)


func list_files(absolute_path: String) -> PackedStringArray:
	return DirAccess.get_files_at(absolute_path)


func file_exists(absolute_path: String) -> bool:
	return FileAccess.file_exists(absolute_path)


func file_size(absolute_path: String) -> int:
	var file := FileAccess.open(absolute_path, FileAccess.READ)
	if file == null:
		return -1
	var length := file.get_length()
	file.close()
	return length


func read_text(absolute_path: String) -> Dictionary:
	var file := FileAccess.open(absolute_path, FileAccess.READ)
	if file == null:
		var open_error := FileAccess.get_open_error()
		return {"ok": false, "error": open_error if open_error != OK else FAILED}
	var text := file.get_as_text()
	var read_error := file.get_error()
	file.close()
	if read_error != OK and read_error != ERR_FILE_EOF:
		return {"ok": false, "error": read_error}
	return {"ok": true, "text": text}


func write_text(
	absolute_path: String,
	text: String,
	before_failpoint: String,
	after_failpoint: String
) -> Error:
	var before_error := hit(before_failpoint)
	if before_error != OK:
		return before_error
	var file := FileAccess.open(absolute_path, FileAccess.WRITE)
	if file == null:
		var open_error := FileAccess.get_open_error()
		return open_error if open_error != OK else FAILED
	file.store_string(text)
	file.flush()
	var write_error := file.get_error()
	file.close()
	if write_error != OK and write_error != ERR_FILE_EOF:
		return write_error
	return hit(after_failpoint)


func read_bytes(
	absolute_path: String,
	before_failpoint: String = "snapshot_before_read",
	after_failpoint: String = "snapshot_after_read"
) -> Dictionary:
	var before_error := hit(before_failpoint)
	if before_error != OK:
		return {"ok": false, "error": before_error}
	if not file_exists(absolute_path):
		return {"ok": false, "error": ERR_FILE_NOT_FOUND}
	var bytes := FileAccess.get_file_as_bytes(absolute_path)
	var after_error := hit(after_failpoint)
	if after_error != OK:
		return {"ok": false, "error": after_error, "bytes": bytes}
	return {"ok": true, "bytes": bytes}


func write_bytes(
	absolute_path: String,
	bytes: PackedByteArray,
	before_failpoint: String = "restore_before_write",
	after_failpoint: String = "restore_after_write"
) -> Error:
	var before_error := hit(before_failpoint)
	if before_error != OK:
		return before_error
	var dir_error := make_dir_recursive(absolute_path.get_base_dir())
	if dir_error != OK and dir_error != ERR_ALREADY_EXISTS:
		return dir_error
	var file := FileAccess.open(absolute_path, FileAccess.WRITE)
	if file == null:
		var open_error := FileAccess.get_open_error()
		return open_error if open_error != OK else FAILED
	file.store_buffer(bytes)
	file.flush()
	var write_error := file.get_error()
	file.close()
	if write_error != OK and write_error != ERR_FILE_EOF:
		return write_error
	return hit(after_failpoint)


func save_snapshot(scene: PackedScene, path: String) -> Error:
	var before_error := hit("snapshot_before_save")
	if before_error != OK:
		return before_error
	var save_error := ResourceSaver.save(scene, path)
	if save_error != OK:
		return save_error
	return hit("snapshot_after_save")


func remove_file(absolute_path: String) -> Error:
	var before_error := hit("cleanup_before_delete")
	if before_error != OK:
		return before_error
	var remove_error := DirAccess.remove_absolute(absolute_path)
	if remove_error != OK:
		return remove_error
	return hit("cleanup_after_delete")


func remove_plain(absolute_path: String) -> Error:
	return DirAccess.remove_absolute(absolute_path)
