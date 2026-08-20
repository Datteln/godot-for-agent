class_name StreamRevealQueue
extends RefCounted

## 流式文本块的逐帧字符揭示队列（打字机效果）。
##
## 渲染策略：流式 RichTextLabel 一次性接收完整 bbcode 目标文本，
## 用 Godot 内置的 `visible_characters` 渐进显示。本队列只维护
## "每个 item 的每个文本块已显示到第几个字符"，由宿主每帧推进。
## 目标文本变化（新 delta 到达）只更新总数，不重置已显示位置。
##
## 纯展示层：不接触事件协议、store 或滚动逻辑。

const CHARS_PER_FRAME := 2

var _entries: Dictionary = {}  # item_id -> { block_index -> { "shown": int, "total": int } }


func register(item_id: String, block_index: int, total_chars: int) -> void:
	## 记录目标总量；已存在的块保留 shown，只更新 total。
	var blocks: Dictionary = _entries.get(item_id, {})
	if not (blocks is Dictionary):
		blocks = {}
	var state := blocks.get(block_index, {"shown": 0, "total": 0})
	if not (state is Dictionary):
		state = {"shown": 0, "total": 0}
	state["total"] = total_chars
	blocks[block_index] = state
	_entries[item_id] = blocks


func shown(item_id: String, block_index: int) -> int:
	return _shown_state(item_id, block_index).get("shown", 0)


func total(item_id: String, block_index: int) -> int:
	return _shown_state(item_id, block_index).get("total", 0)


func has_block(item_id: String, block_index: int) -> bool:
	var blocks: Variant = _entries.get(item_id)
	return (blocks is Dictionary) and blocks.has(block_index)


func advance_all(step: int = CHARS_PER_FRAME) -> bool:
	## 每个活跃块推进 step 个字符；返回是否有任一块发生变化。
	var changed := false
	for item_id in _entries.keys():
		var blocks: Dictionary = _entries[item_id]
		for block_index in blocks.keys():
			var state: Dictionary = blocks[block_index]
			var total := int(state.get("total", 0))
			var shown := int(state.get("shown", 0))
			if shown >= total:
				continue
			state["shown"] = mini(total, shown + step)
			changed = true
	return changed


func is_active() -> bool:
	for item_id in _entries.keys():
		var blocks: Dictionary = _entries[item_id]
		for block_index in blocks.keys():
			var state: Dictionary = blocks[block_index]
			if int(state.get("shown", 0)) < int(state.get("total", 0)):
				return true
	return false


func drain(item_id: String) -> void:
	if _entries.has(item_id):
		_entries.erase(item_id)


func clear() -> void:
	_entries.clear()


func _shown_state(item_id: String, block_index: int) -> Dictionary:
	var blocks: Variant = _entries.get(item_id)
	if not (blocks is Dictionary):
		return {}
	var state: Variant = blocks.get(block_index)
	return state if state is Dictionary else {}