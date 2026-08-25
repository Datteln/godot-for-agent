## 会话/水合世代作用域的展示稿 Store（任务 4.1）。
##
## 纯数据容器：按 entry_id 保存条目、按 ordinal 排序，维护已接受 event_id、
## 每条目最新 revision 与当前 session/generation。不做任何渲染与网络访问；
## 快照原子替换、补丁按 event_id 去重并拒绝不高于已接受 revision 的更新。
## 乐观用户条目仅通过 client_message_id 与服务端条目对账。
extends RefCounted

## 条目集合发生变化（changed_ids 为被创建/更新的条目 id；replaced 表示快照替换）。
signal entries_changed(changed_ids: Array, replaced: bool)

var session_id: String = ""
var generation: int = 0
var upto_event_seq: int = 0
var legacy: bool = false
var history_has_more: bool = false
var next_before_ordinal: int = -1

var _entries: Dictionary = {}          # entry_id -> entry Dictionary
var _order: Array = []                 # entry_id 列表，按 ordinal 升序
var _seen_event_ids: Dictionary = {}   # event_id -> true（幂等去重）
var _optimistic: Dictionary = {}       # client_message_id -> 本地乐观用户条目


## 原子替换整个会话展示态（快照水合）。
## 仅保留 client_message_id 未被快照确认的乐观用户条目；返回全部可见条目 id。
func replace_snapshot(snapshot: Dictionary, snapshot_session_id: String, snapshot_generation: int) -> Array:
	if snapshot_session_id != session_id or snapshot_generation != generation:
		return []
	var raw_entries: Variant = snapshot.get("entries", [])
	var entry_list: Array = raw_entries if raw_entries is Array else []
	_entries.clear()
	_order.clear()
	_seen_event_ids.clear()
	upto_event_seq = int(snapshot.get("upto_event_seq", upto_event_seq))
	legacy = bool(snapshot.get("legacy", false))
	history_has_more = bool(snapshot.get("has_more", false))
	var cursor_value: Variant = snapshot.get("next_before_ordinal", -1)
	next_before_ordinal = int(cursor_value) if cursor_value != null else -1
	var confirmed_client_ids := {}
	for raw_entry in entry_list:
		if not (raw_entry is Dictionary):
			continue
		var entry: Dictionary = raw_entry
		var entry_id := str(entry.get("entry_id", ""))
		if entry_id == "":
			continue
		_entries[entry_id] = entry
		_order.append(entry_id)
		var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}
		var client_message_id := str(payload.get("client_message_id", ""))
		if client_message_id != "":
			confirmed_client_ids[client_message_id] = true
	_order.sort_custom(func(a: String, b: String) -> bool:
		return int(_entries[a].get("ordinal", 0)) < int(_entries[b].get("ordinal", 0))
	)
	var pending_optimistic := {}
	for client_message_id in _optimistic.keys():
		if not confirmed_client_ids.has(client_message_id):
			pending_optimistic[client_message_id] = _optimistic[client_message_id]
	_optimistic = pending_optimistic
	var changed: Array = ordered_entry_ids()
	entries_changed.emit(changed, true)
	return changed


## 合并 READY 会话的一个更早历史页。页面只能补充未知条目或提升已有 revision；
## 返回实际变化的 entry_id，空数组表示页已被完全覆盖或不合法。
func merge_older_page(page: Dictionary, page_session_id: String, page_generation: int) -> Array:
	if page_session_id != session_id or page_generation != generation:
		return []
	var raw_entries: Variant = page.get("entries", [])
	if not (raw_entries is Array):
		return []
	var changed: Array = []
	for raw_entry in raw_entries:
		if not (raw_entry is Dictionary):
			continue
		var entry: Dictionary = raw_entry
		var entry_id := str(entry.get("entry_id", ""))
		if entry_id == "":
			continue
		var existing: Dictionary = _entries.get(entry_id, {})
		if existing.is_empty():
			_entries[entry_id] = entry
			_order.append(entry_id)
			changed.append(entry_id)
			continue
		if int(entry.get("ordinal", -1)) != int(existing.get("ordinal", -1)):
			continue
		var revision := int(entry.get("revision", 1))
		if revision <= int(existing.get("revision", 1)):
			continue
		if str(existing.get("kind", "")) == "thought" \
				and str(existing.get("state", "")) == "complete" \
				and str(entry.get("state", "")) == "thinking":
			continue
		_entries[entry_id] = entry
		changed.append(entry_id)
	_order.sort_custom(func(a: String, b: String) -> bool:
		return int(_entries[a].get("ordinal", 0)) < int(_entries[b].get("ordinal", 0))
	)
	history_has_more = bool(page.get("has_more", false))
	next_before_ordinal = int(page.get("next_before_ordinal", -1))
	if not changed.is_empty():
		entries_changed.emit(changed, false)
	return changed


## 应用一条补丁；按 event_id 去重、按 revision 拒绝过期更新。
## 返回该补丁是否真正改变了展示态。
func apply_patch(patch: Dictionary, event_id: String) -> bool:
	if event_id != "" and _seen_event_ids.has(event_id):
		return false
	var raw_entry: Variant = patch.get("entry", {})
	if not (raw_entry is Dictionary):
		return false
	var entry: Dictionary = raw_entry
	var entry_id := str(entry.get("entry_id", ""))
	if entry_id == "":
		return false
	var revision := int(entry.get("revision", 1))
	var existing: Dictionary = _entries.get(entry_id, {})
	if not existing.is_empty() and revision <= int(existing.get("revision", 1)):
		# 过期或同修订的重复投递：记录 event_id 防止反复处理，但不改变状态。
		if event_id != "":
			_seen_event_ids[event_id] = true
		return false
	if not existing.is_empty() \
			and str(existing.get("kind", "")) == "thought" \
			and str(existing.get("state", "")) == "complete" \
			and str(entry.get("state", "")) == "thinking":
		# Thought 终态单向（渲染契约同款守卫）：即使 revision 更高，
		# 把已完成 Thought 退回 thinking 的补丁也必须被拒绝。
		if event_id != "":
			_seen_event_ids[event_id] = true
		return false
	if event_id != "":
		_seen_event_ids[event_id] = true
	var is_new := existing.is_empty()
	_entries[entry_id] = entry
	if is_new:
		_order.append(entry_id)
		_order.sort_custom(func(a: String, b: String) -> bool:
			return int(_entries[a].get("ordinal", 0)) < int(_entries[b].get("ordinal", 0))
		)
	# 服务端条目到达即确认同 client_message_id 的乐观条目。
	var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}
	var client_message_id := str(payload.get("client_message_id", ""))
	if client_message_id != "" and _optimistic.has(client_message_id):
		_optimistic.erase(client_message_id)
	entries_changed.emit([entry_id], false)
	return true


## 记录一个尚未被服务端确认的乐观用户条目（仅凭 client_message_id 对账）。
func add_optimistic_user_entry(text: String, client_message_id: String) -> void:
	if client_message_id == "" or _optimistic.has(client_message_id):
		return
	_optimistic[client_message_id] = {
		"entry_id": "optimistic:" + client_message_id,
		"ordinal": -1,
		"kind": "user",
		"state": "complete",
		"revision": 1,
		"turn_id": null,
		"tool_call_id": null,
		"payload": {
			"text": text,
			"client_message_id": client_message_id,
			"has_context": false,
			"optimistic": true,
		},
	}
	entries_changed.emit([str("optimistic:" + client_message_id)], false)


## 丢弃指定乐观条目（如发送失败回滚）。
func remove_optimistic_entry(client_message_id: String) -> void:
	if _optimistic.erase(client_message_id):
		entries_changed.emit([], false)


## 会话切换/水合开始时清空全部状态。
func clear() -> void:
	session_id = ""
	upto_event_seq = 0
	legacy = false
	history_has_more = false
	next_before_ordinal = -1
	_entries.clear()
	_order.clear()
	_seen_event_ids.clear()
	_optimistic.clear()


## 返回按 ordinal 排序的条目 id（乐观条目追加在末尾）。
func ordered_entry_ids() -> Array:
	var ids: Array = _order.duplicate()
	for client_message_id in _optimistic.keys():
		ids.append(str("optimistic:" + client_message_id))
	return ids


## 返回条目字典；不存在时返回空字典。
func get_entry(entry_id: String) -> Dictionary:
	if entry_id.begins_with("optimistic:"):
		var client_message_id := entry_id.substr("optimistic:".length())
		var optimistic: Dictionary = _optimistic.get(client_message_id, {})
		return optimistic
	var entry: Dictionary = _entries.get(entry_id, {})
	return entry


## 该 event_id 是否已被 Store 处理过（幂等去重表；任务 2.12）。
##
## 供提交判定区分“补丁被去重/修订未前进”（已呈现，可提交）与
## “补丁被投影器拒绝”（未呈现，绝不能提交）。
func has_seen_event(event_id: String) -> bool:
	return event_id != "" and _seen_event_ids.has(event_id)


## 当前是否存在处于 complete 状态的助手条目（供 final 确认兜底判断）。
func has_complete_assistant_entry() -> bool:
	for entry_id in _order:
		var entry: Dictionary = _entries.get(entry_id, {})
		if str(entry.get("kind", "")) == "assistant" and str(entry.get("state", "")) == "complete":
			return true
	return false


## 条目总数（含乐观条目）。
func entry_count() -> int:
	return _order.size() + _optimistic.size()
