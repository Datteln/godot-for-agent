@tool
extends RefCounted

const ChatTimelineStore = preload("res://addons/ai_agent/timeline/chat_timeline_store.gd")
const ChatItemRendererRegistry = preload("res://addons/ai_agent/timeline/chat_item_renderer_registry.gd")

const BUFFER_ITEMS := 3
const MIN_VISIBLE_ITEMS := 12
const MAX_RESYNC_FRAMES := 4

var _scroll: ScrollContainer
var _item_list: VBoxContainer
var _store: ChatTimelineStore
var _registry: ChatItemRendererRegistry
var _top_spacer: Control
var _bottom_spacer: Control
var _node_cache: Dictionary = {}
var _syncing := false
var _scroll_sync_pending := false
var _sync_again := false
var _sync_again_stick_to_bottom := false
var _resync_requested := false
var _resync_stick_to_bottom := false
var _consecutive_resyncs := 0
var _pending_anchor_offset := 0.0


func setup(scroll: ScrollContainer, item_list: VBoxContainer, store: ChatTimelineStore, registry: ChatItemRendererRegistry) -> void:
	_scroll = scroll
	_item_list = item_list
	_store = store
	_registry = registry
	_store.item_spacing = float(item_list.get_theme_constant("separation"))
	_store.mutation_applied.connect(_on_store_mutation)
	_top_spacer = _make_spacer("TopSpacer")
	_bottom_spacer = _make_spacer("BottomSpacer")


func _on_store_mutation(mutation: Dictionary, result: Dictionary) -> void:
	var kind := str(mutation.get("kind", ""))
	match kind:
		"reset_epoch":
			_clear_nodes()
		"insert":
			if not bool(result.get("duplicate", false)):
				_shift_cache_for_insert(int(result.get("index", _store.size() - 1)), 1)
			sync(_scroll_position(), false)
		"prepend_page":
			var inserted := int(result.get("inserted", 0))
			var first_old_index := int(result.get("first_old_index", inserted))
			if inserted > 0:
				_shift_cache_for_insert(0, inserted)
				_pending_anchor_offset += _store.total_height(0, maxi(first_old_index, inserted))
				call_deferred("_restore_anchor")
		"patch", "finalize":
			_refresh_index(int(result.get("index", -1)))
		"discard", "remove":
			_remove_cached_index(int(result.get("index", -1)))
			sync(_scroll_position(), false)


func _restore_anchor() -> void:
	if _scroll == null or not is_instance_valid(_scroll):
		_pending_anchor_offset = 0.0
		return
	var offset := _pending_anchor_offset
	_pending_anchor_offset = 0.0
	if offset > 0.0:
		_scroll.scroll_vertical += int(round(offset))
	on_scroll_changed(float(_scroll.scroll_vertical))


func debug_state() -> Dictionary:
	return {
		"store_size": _store.size() if _store != null else -1,
		"cache_size": _node_cache.size(),
		"child_count": _item_list.get_child_count() if _item_list != null else -1,
		"syncing": _syncing,
		"resync_requested": _resync_requested,
		"pending_anchor_offset": _pending_anchor_offset,
	}


func consume_resync_request() -> Dictionary:
	var request := {"requested": _resync_requested, "stick_to_bottom": _resync_stick_to_bottom}
	_resync_requested = false
	_resync_stick_to_bottom = false
	return request


func on_scroll_changed(_scroll_y: float) -> void:
	if _scroll_sync_pending:
		return
	_scroll_sync_pending = true
	call_deferred("_deferred_scroll_sync")


func _deferred_scroll_sync() -> void:
	_scroll_sync_pending = false
	sync(_scroll_position(), false)


func sync(scroll_y: float, stick_to_bottom: bool) -> void:
	if _item_list == null or _store == null:
		return
	if _syncing:
		_sync_again = true
		_sync_again_stick_to_bottom = _sync_again_stick_to_bottom or stick_to_bottom
		return
	_syncing = true
	var heights_changed := _measure_visible_heights()
	var visible_range := _compute_visible_range(scroll_y, stick_to_bottom)
	var visible := _visible_indexes(visible_range)
	_top_spacer.custom_minimum_size = Vector2(0, _store.total_height(0, visible_range.x))
	_bottom_spacer.custom_minimum_size = Vector2(0, _store.total_height(visible_range.y, _store.size()))
	_sync_nodes(visible)
	_syncing = false
	var needs_resync := heights_changed or _sync_again
	var next_stick := stick_to_bottom or _sync_again_stick_to_bottom
	_sync_again = false
	_sync_again_stick_to_bottom = false
	if needs_resync:
		_request_next_frame_resync(next_stick)
	else:
		_consecutive_resyncs = 0


func _request_next_frame_resync(stick_to_bottom: bool) -> void:
	if _consecutive_resyncs >= MAX_RESYNC_FRAMES:
		return
	_consecutive_resyncs += 1
	_resync_requested = true
	_resync_stick_to_bottom = _resync_stick_to_bottom or stick_to_bottom


func _compute_visible_range(scroll_y: float, stick_to_bottom: bool) -> Vector2i:
	var total := _store.size()
	if total == 0:
		return Vector2i(0, 0)
	var viewport_height := maxf(1.0, _scroll.size.y if _scroll != null else 600.0)
	var first: int
	var last: int
	if stick_to_bottom:
		last = total
		first = maxi(0, _store.find_index_at_scroll(maxf(0.0, _store.total_height() - viewport_height)) - BUFFER_ITEMS)
	else:
		first = maxi(0, _store.find_index_at_scroll(scroll_y) - BUFFER_ITEMS)
		last = mini(total, _store.find_index_at_scroll(scroll_y + viewport_height) + BUFFER_ITEMS + 1)
	if last - first < MIN_VISIBLE_ITEMS:
		first = maxi(0, mini(first, total - MIN_VISIBLE_ITEMS))
		last = mini(total, maxi(last, first + MIN_VISIBLE_ITEMS))
	return Vector2i(first, last)


func _visible_indexes(visible_range: Vector2i) -> Dictionary:
	var result := {}
	for index in range(visible_range.x, visible_range.y):
		result[index] = true
	return result


func _sync_nodes(visible: Dictionary) -> void:
	for cached_index in _node_cache.keys().duplicate():
		if visible.has(cached_index):
			continue
		_free_cached_node(int(cached_index))
	var indexes: Array = visible.keys()
	indexes.sort()
	for raw_index in indexes:
		var index := int(raw_index)
		if _node_cache.has(index):
			continue
		var node := _registry.create_item_node(_store.get_item(index))
		if node != null:
			_node_cache[index] = node
			_item_list.add_child(node)
	if _top_spacer.get_parent() != _item_list:
		_item_list.add_child(_top_spacer)
	if _bottom_spacer.get_parent() != _item_list:
		_item_list.add_child(_bottom_spacer)
	if _item_list.get_child_count() > 0 and _item_list.get_child(0) != _top_spacer:
		_item_list.move_child(_top_spacer, 0)
	var position := 1
	for raw_index in indexes:
		var index := int(raw_index)
		if not _node_cache.has(index):
			continue
		var node: Control = _node_cache[index]
		if position >= _item_list.get_child_count() or _item_list.get_child(position) != node:
			_item_list.move_child(node, position)
		position += 1
	_item_list.move_child(_bottom_spacer, position)


func _refresh_index(index: int) -> void:
	if index < 0:
		return
	if _node_cache.has(index):
		var old_node: Control = _node_cache[index]
		var new_node := _registry.create_item_node(_store.get_item(index))
		if new_node != null:
			var child_index := old_node.get_index() if old_node.get_parent() == _item_list else -1
			if child_index >= 0:
				_item_list.remove_child(old_node)
				_item_list.add_child(new_node)
				_item_list.move_child(new_node, child_index)
			_node_cache[index] = new_node
			old_node.queue_free()
	sync(_scroll_position(), false)


func _shift_cache_for_insert(index: int, count: int) -> void:
	var shifted := {}
	for raw_index in _node_cache.keys():
		var old_index := int(raw_index)
		shifted[old_index + count if old_index >= index else old_index] = _node_cache[raw_index]
	_node_cache = shifted


func _remove_cached_index(index: int) -> void:
	if index < 0:
		return
	_free_cached_node(index)
	var shifted := {}
	for raw_index in _node_cache.keys():
		var old_index := int(raw_index)
		shifted[old_index - 1 if old_index > index else old_index] = _node_cache[raw_index]
	_node_cache = shifted


func _free_cached_node(index: int) -> void:
	if not _node_cache.has(index):
		return
	var node: Control = _node_cache[index]
	_node_cache.erase(index)
	if is_instance_valid(node):
		if node.get_parent() == _item_list:
			_item_list.remove_child(node)
		node.queue_free()


func _measure_visible_heights() -> bool:
	var changed := false
	for raw_index in _node_cache.keys():
		var index := int(raw_index)
		var node: Control = _node_cache[raw_index]
		if node != null and is_instance_valid(node) and node.size.y > 1.0:
			var old_height := _store.height_at(index) - _store.item_spacing
			if absf(old_height - node.size.y) > 1.0:
				_store.update_height(index, node.size.y)
				changed = true
	return changed


func _clear_nodes() -> void:
	_resync_requested = false
	_resync_stick_to_bottom = false
	_consecutive_resyncs = 0
	for raw_index in _node_cache.keys().duplicate():
		_free_cached_node(int(raw_index))
	if _item_list != null:
		for child in _item_list.get_children():
			if child == _top_spacer or child == _bottom_spacer:
				continue
			_item_list.remove_child(child)
			child.queue_free()


func _scroll_position() -> float:
	return float(_scroll.scroll_vertical) if _scroll != null else 0.0


func _make_spacer(node_name: String) -> Control:
	var spacer := Control.new()
	spacer.name = node_name
	spacer.mouse_filter = Control.MOUSE_FILTER_IGNORE
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	return spacer
