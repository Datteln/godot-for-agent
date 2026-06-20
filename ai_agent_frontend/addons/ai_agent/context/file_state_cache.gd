@tool
extends Node

var _states: Dictionary = {}


func snapshot(path: String, known_full_read: bool = false) -> Dictionary:
	var state := _make_state(path, known_full_read)
	_states[path] = state
	return state


func get_state(path: String) -> Dictionary:
	return _states.get(path, {})


## 是否曾经为该路径做过任意一次快照（read_file/write_file 都会打快照）。
## 用于强制"先 read 再 apply_text_edit"——没有任何已知状态时拒绝局部编辑，
## 避免模型凭空猜测 old_string 改出一个它从未真正看过的文件。
func has_state(path: String) -> bool:
	return _states.has(path)


## 只比较内容哈希，不比较 mtime：哈希相同就意味着字节内容完全没变，写入不会
## 覆盖任何人的修改，这种情况下没有"过期"可言。之前还要求 mtime 也相同，结果是
## 即使内容字节不差，只要文件系统/索引/同步进程之类的东西把 mtime 碰了一下
## （常见于 OneDrive/云同步、杀毒软件扫描、Windows 搜索索引），就会被误判为
## "磁盘上已改动"——曾经出现过 read_file 刚拿到的新鲜快照，几秒内紧接着的
## write 检查就被这条 mtime 比较判成 stale，而内容其实根本没变。
func is_stale(path: String) -> bool:
	if not _states.has(path):
		return false
	var old_state: Dictionary = _states[path]
	var current := _make_state(path, bool(old_state.get("known_full_read", false)))
	return old_state.get("hash", "") != current.get("hash", "")


func _make_state(path: String, known_full_read: bool) -> Dictionary:
	var absolute := ProjectSettings.globalize_path(path)
	var exists := FileAccess.file_exists(absolute)
	var content := ""
	if exists:
		content = FileAccess.get_file_as_string(absolute)
	return {
		"path": path,
		"exists": exists,
		"hash": content.sha256_text(),
		"mtime_ns": FileAccess.get_modified_time(absolute) * 1000000000,
		"known_full_read": known_full_read
	}
