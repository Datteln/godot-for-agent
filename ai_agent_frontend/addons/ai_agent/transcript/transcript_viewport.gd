## 长会话展示稿的窗口化视口。Store 永远保存全部条目；此对象只管理有限 renderer 根节点。
@tool
extends RefCounted

signal older_page_requested(before_ordinal: int)
signal diagnostics_changed(values: Dictionary)
signal follow_mode_changed(enabled: bool)
signal renderer_rejected(diagnostic: Dictionary)

var overscan: int = 8
var max_mounted_roots: int = 64
var estimated_row_height: float = 96.0
var leading_threshold_px: float = 480.0

var _list: VBoxContainer
var _mounts: VBoxContainer
var _top_spacer: Control
var _bottom_spacer: Control
var _renderer: RefCounted
var _store: RefCounted
var _scroll: ScrollContainer
var _window_start := 0
var _window_end := 0
var _follow_mode := true
var _loading_cursors := {}
var _failed_cursors := {}
var _measurements := {}
var _presentation_epoch := 0
var _anchor := {}
var _last_width_bucket := -1
var _eviction_count := 0
var _last_diagnostics: Dictionary = {}


func attach(list: VBoxContainer, renderer: RefCounted, store: RefCounted, scroll: ScrollContainer) -> void:
	_list = list
	_renderer = renderer
	_store = store
	_scroll = scroll
	_top_spacer = Control.new()
	_bottom_spacer = Control.new()
	_mounts = VBoxContainer.new()
	_mounts.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_list.add_child(_top_spacer)
	_list.add_child(_mounts)
	_list.add_child(_bottom_spacer)
	_renderer.attach(_mounts)


## 以尾部为默认窗口重新投影 Store；保持全部数据但只挂载连续范围。
func replace_from_store() -> void:
	_capture_anchor()
	var total: int = _store.ordered_entry_ids().size()
	_window_end = total
	_window_start = maxi(0, total - max_mounted_roots)
	_renderer.clear_all()
	_render_window()
	_restore_anchor()


## 更新一个已接受条目；窗口内原地更新，尾部 follow 时才把窗口推进到底部。
func apply_entry(entry: Dictionary) -> bool:
	_capture_anchor()
	var ids: Array = _store.ordered_entry_ids()
	var index := ids.find(str(entry.get("entry_id", "")))
	if index < 0:
		return false
	if _follow_mode and index >= _window_end - overscan:
		_window_end = ids.size()
		_window_start = maxi(0, _window_end - max_mounted_roots)
	if index >= _window_start and index < _window_end:
		if not _ensure_mounted(entry):
			return false
	_render_window()
	_restore_anchor()
	return true


## 一个投影窗口内批量应用多个已接受条目（任务 3.3）。
##
## 与逐条 `apply_entry` 不同：整批只做一次锚点捕获/恢复、一次窗口推进判定与
## 一次 `_render_window` 重排，避免每个流式包都触发一次同步回流。终态/非流式
## 条目仍由调用方走 `apply_entry` 立即应用，保证顺序不被批处理延后。
func apply_batch(entries: Array) -> int:
	if entries.is_empty():
		return 0
	_capture_anchor()
	var ids: Array = _store.ordered_entry_ids()
	var follow_extend := false
	var rendered := 0
	for entry_value in entries:
		if not (entry_value is Dictionary):
			continue
		var entry: Dictionary = entry_value
		var index := ids.find(str(entry.get("entry_id", "")))
		if index < 0:
			continue
		if _follow_mode and index >= _window_end - overscan:
			follow_extend = true
		if index >= _window_start and index < _window_end:
			if _ensure_mounted(entry):
				rendered += 1
	if follow_extend:
		_window_end = ids.size()
		_window_start = maxi(0, _window_end - max_mounted_roots)
		# 窗口推进后可能有新条目进入范围，需要再挂载一次。
		for index in range(_window_start, _window_end):
			var entry: Dictionary = _store.get_entry(str(ids[index]))
			if not entry.is_empty():
				_ensure_mounted(entry)
	_render_window()
	_restore_anchor()
	return rendered


## 合并旧页后保留当前窗口；若窗口尚未建立则从 Store 初始化。
func merge_older_page() -> void:
	_capture_anchor()
	if _window_end == 0:
		replace_from_store()
		return
	_render_window()
	_restore_anchor()


func notify_scroll(value: float) -> void:
	if _scroll == null:
		return
	var bar := _scroll.get_v_scroll_bar()
	var maximum: float = bar.max_value - bar.page
	_set_follow_mode(maximum <= 0.0 or value >= maximum - 80.0)
	var ids: Array = _store.ordered_entry_ids()
	var visible_index: int = _index_at_offset(ids, value)
	var desired_start: int = clampi(visible_index - overscan, 0, maxi(0, ids.size() - max_mounted_roots))
	var desired_end: int = mini(ids.size(), desired_start + max_mounted_roots)
	if desired_start != _window_start or desired_end != _window_end:
		_capture_anchor()
		_window_start = desired_start
		_window_end = desired_end
		_render_window()
		_restore_anchor()
	if value <= leading_threshold_px and _store.history_has_more and _store.next_before_ordinal >= 0:
		var cursor: int = _store.next_before_ordinal
		if not _loading_cursors.has(cursor) and not _failed_cursors.has(cursor):
			_loading_cursors[cursor] = true
			older_page_requested.emit(cursor)


func complete_older_page(before_ordinal: int, succeeded: bool) -> void:
	_loading_cursors.erase(before_ordinal)
	if not succeeded:
		_failed_cursors[before_ordinal] = true
	else:
		_failed_cursors.erase(before_ordinal)


func retry_older_page() -> void:
	if _store.next_before_ordinal >= 0:
		_failed_cursors.erase(_store.next_before_ordinal)
		notify_scroll(0.0)


func return_to_latest() -> void:
	_set_follow_mode(true)
	var total: int = _store.ordered_entry_ids().size()
	_window_end = total
	_window_start = maxi(0, total - max_mounted_roots)
	_render_window()
	if _scroll != null:
		_scroll.scroll_vertical = 999999


## 用户开始选择、复制或展开详情时显式停止尾部跟随。
func suppress_follow() -> void:
	_set_follow_mode(false)


func is_following() -> bool:
	return _follow_mode


func navigation_diagnostics() -> Dictionary:
	return _last_diagnostics.duplicate(true)


func advance_presentation_epoch() -> void:
	_capture_anchor()
	_presentation_epoch += 1
	_measurements.clear()
	_render_window()
	_restore_anchor()


func _render_window() -> void:
	if _store == null:
		return
	var ids: Array = _store.ordered_entry_ids()
	_window_end = mini(_window_end, ids.size())
	_window_start = clampi(_window_start, 0, _window_end)
	for entry_id in _renderer.mounted_entry_ids():
		var index := ids.find(entry_id)
		if index < _window_start or index >= _window_end:
			_renderer.evict_entry(entry_id)
			_eviction_count += 1
	for index in range(_window_start, _window_end):
		var entry: Dictionary = _store.get_entry(str(ids[index]))
		if not entry.is_empty():
			_ensure_mounted(entry)
	_measure_mounted()
	_update_spacers(ids)
	_last_diagnostics = {
		"mounted_root_count": _renderer.mounted_count(),
		"mounted_rich_text_characters": _renderer.mounted_rich_text_characters(),
		"estimated_range_height": _estimated_range_height(ids, 0, ids.size()),
		"presentation_epoch": _presentation_epoch,
		"follow_mode": _follow_mode,
		"evictions": _eviction_count,
		"content_modes": _renderer.mounted_content_modes(),
	}
	diagnostics_changed.emit(_last_diagnostics)


func _update_spacers(ids: Array) -> void:
	_top_spacer.custom_minimum_size.y = _estimated_range_height(ids, 0, _window_start)
	_bottom_spacer.custom_minimum_size.y = _estimated_range_height(ids, _window_end, ids.size())


func _estimated_range_height(ids: Array, first: int, last: int) -> float:
	var height := 0.0
	for index in range(first, last):
		var entry: Dictionary = _store.get_entry(str(ids[index]))
		var key := _height_key(entry)
		height += float(_measurements.get(key, estimated_row_height))
	return height


func _height_key(entry: Dictionary) -> String:
	return "%s:%d:%d:%d:%s" % [str(entry.get("entry_id", "")), int(entry.get("revision", 1)), _width_bucket(), _presentation_epoch, _renderer.content_mode_for(str(entry.get("entry_id", "")))]


func _width_bucket() -> int:
	if _scroll == null:
		return 0
	return maxi(1, int(_scroll.size.x / 32.0))


func _measure_mounted() -> void:
	var width := _width_bucket()
	if width != _last_width_bucket:
		_last_width_bucket = width
		_presentation_epoch += 1
	for entry_id in _renderer.mounted_entry_ids():
		var entry: Dictionary = _store.get_entry(entry_id)
		var root: Control = _renderer.mounted_root(entry_id)
		if not entry.is_empty() and root != null and is_instance_valid(root) and root.size.y > 0.0:
			_measurements[_height_key(entry)] = root.size.y


func _index_at_offset(ids: Array, offset: float) -> int:
	var remaining := maxf(0.0, offset)
	for index in range(ids.size()):
		var entry: Dictionary = _store.get_entry(str(ids[index]))
		var height := float(_measurements.get(_height_key(entry), estimated_row_height))
		if remaining < height:
			return index
		remaining -= height
	return maxi(0, ids.size() - 1)


func _capture_anchor() -> void:
	if _scroll == null or _store == null:
		return
	var ids: Array = _store.ordered_entry_ids()
	var index := _index_at_offset(ids, float(_scroll.scroll_vertical))
	if index >= 0 and index < ids.size():
		_anchor = {"entry_id": str(ids[index]), "offset": float(_scroll.scroll_vertical) - _estimated_range_height(ids, 0, index), "index": index}


func _restore_anchor() -> void:
	if _scroll == null or _anchor.is_empty() or _follow_mode:
		return
	var ids: Array = _store.ordered_entry_ids()
	var index := ids.find(str(_anchor.get("entry_id", "")))
	if index < 0:
		index = clampi(int(_anchor.get("index", 0)), 0, maxi(0, ids.size() - 1))
	_scroll.scroll_vertical = int(maxf(0.0, _estimated_range_height(ids, 0, index) + float(_anchor.get("offset", 0.0))))


func _set_follow_mode(value: bool) -> void:
	if _follow_mode == value:
		return
	_follow_mode = value
	follow_mode_changed.emit(value)


## 确保窗口内条目拥有有效根节点。renderer 的 False 也可能只是同 revision
## 重放而非失败；只有根节点缺失且重建失败时才诊断为 renderer 拒绝。
func _ensure_mounted(entry: Dictionary) -> bool:
	# ChatPanel/插件热重载可能重建 RefCounted 边界；每次挂载前重申唯一宿主，
	# 防止 renderer 因遗失容器引用拒绝有效的服务端 patch。
	# 旧 viewport 可能在 UI 重建后收到迟到事件；其容器已释放时不可再渲染。
	if _mounts == null or not is_instance_valid(_mounts):
		return false
	_renderer.ensure_mount_container(_mounts)
	var entry_id := str(entry.get("entry_id", ""))
	if entry_id == "":
		renderer_rejected.emit({"entry_id": entry_id, "renderer_reason": "missing_entry_id"})
		return false
	var applied: bool = _renderer.apply_entry(entry, false)
	if applied or _renderer.is_mounted(entry_id):
		return true
	_renderer.forget_entry(entry_id)
	if _renderer.apply_entry(entry, false):
		return true
	renderer_rejected.emit({"entry_id": entry_id, "renderer_reason": _renderer.last_failure_reason()})
	return false
