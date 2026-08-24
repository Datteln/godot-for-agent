## 可合并流式补丁的按条目批处理器（任务 3.2）。
##
## 接收确认由 socket 层负责；本对象只负责把增长型流式补丁按条目暂存，供
## 投影窗口按到达顺序逐条应用，确保同一渲染窗口内每个条目只做一次视觉更新：
##
## - 完整补丁自含条目最新状态，直接取代该条目全部旧暂存；
## - 连续的追加增量就地合并（保留最早 `base_revision`、拼接 `append_text`），
##   重放/突发到达的大量增量不会无界堆积；
## - 首个完整补丁（建立条目）绝不会被其后的增量覆盖：窗口内严格按到达顺序
##   应用，增量始终能先找到它依赖的基础修订。
##
## 纯数据容器，不做渲染、投影与网络访问，便于无编辑器环境下测试。
extends RefCounted

## 单条目暂存事件数的防御上限；超过说明到达流异常，调用方应请求重同步。
const MAX_PENDING_PER_ENTRY := 32

## 最近一次 `enqueue` 是否突破了单条目上限。
var overflowed := false

var _pending := {}    # entry_id -> Array[Dictionary]（按到达/合并顺序）
var _order: Array = []  # entry_id 首次到达顺序


func is_empty() -> bool:
	return _order.is_empty()


func clear() -> void:
	_pending.clear()
	_order.clear()
	overflowed = false


## 暂存一条可合并流式补丁；返回该条目当前暂存事件数。
func enqueue(entry_id: String, event: Dictionary) -> int:
	if entry_id == "":
		return 0
	var payload_value: Variant = event.get("payload", {})
	var payload: Dictionary = payload_value if payload_value is Dictionary else {}
	var patch_format := str(payload.get("patch_format", "full"))
	if patch_format == "full":
		# 完整补丁自含最新状态：取代该条目全部暂存修订。
		if not _pending.has(entry_id):
			_order.append(entry_id)
		_pending[entry_id] = [event]
		return 1
	if not _pending.has(entry_id):
		_pending[entry_id] = []
		_order.append(entry_id)
	var list: Array = _pending[entry_id]
	if patch_format == "append_delta" and not list.is_empty():
		var last_value: Variant = list[list.size() - 1]
		if last_value is Dictionary and _try_merge_delta(list, last_value, payload, event):
			return list.size()
	list.append(event)
	if list.size() > MAX_PENDING_PER_ENTRY:
		overflowed = true
	return list.size()


## 连续增量就地合并：新增量的 base_revision 等于上一条增量产出的修订时，
## 用新事件包络承载合并结果（保留最早 base_revision、拼接正文）。
func _try_merge_delta(list: Array, last: Dictionary, payload: Dictionary, event: Dictionary) -> bool:
	var last_payload_value: Variant = last.get("payload", {})
	var last_payload: Dictionary = last_payload_value if last_payload_value is Dictionary else {}
	if str(last_payload.get("patch_format", "")) != "append_delta":
		return false
	if int(last_payload.get("revision", -1)) != int(payload.get("base_revision", -2)):
		return false
	var merged: Dictionary = event.duplicate(true)
	var merged_payload_value: Variant = merged.get("payload", {})
	if not (merged_payload_value is Dictionary):
		return false
	var merged_payload: Dictionary = merged_payload_value
	merged_payload["base_revision"] = int(last_payload.get("base_revision", 0))
	merged_payload["append_text"] = str(last_payload.get("append_text", "")) + str(payload.get("append_text", ""))
	list[list.size() - 1] = merged
	return true


## 按条目首次到达顺序取出至多 `max_entries` 个条目的暂存并从暂存中移除；
## 返回 `[{"entry_id": String, "events": Array[Dictionary]}, ...]`。
## 未取出的条目保留到下一个窗口，保证有界处理不丢修订。
func take(max_entries: int) -> Array:
	var result: Array = []
	while result.size() < max_entries and not _order.is_empty():
		var entry_id: String = str(_order.pop_front())
		var list_value: Variant = _pending.get(entry_id, null)
		_pending.erase(entry_id)
		if list_value is Array and not (list_value as Array).is_empty():
			result.append({"entry_id": entry_id, "events": list_value})
	return result


## 取出全部暂存并清空。
func take_all() -> Array:
	return take(_order.size())


## 丢弃某条目的全部暂存（终态/结构补丁立即应用后，防止迟到中间态回退）。
func discard(entry_id: String) -> void:
	if _pending.erase(entry_id):
		_order.erase(entry_id)
