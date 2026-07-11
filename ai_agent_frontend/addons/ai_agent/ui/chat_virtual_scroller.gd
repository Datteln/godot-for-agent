@tool
extends RefCounted

const ChatMessageStore = preload("res://addons/ai_agent/ui/chat_message_store.gd")
const ChatNodeFactory = preload("res://addons/ai_agent/ui/chat_node_factory.gd")

const BUFFER_MESSAGES := 3
const MIN_VISIBLE_MESSAGES := 50

var _scroll: ScrollContainer
var _message_list: VBoxContainer
var _store: ChatMessageStore
var _factory: ChatNodeFactory
var _top_spacer: Control
var _bottom_spacer: Control
var _node_cache: Dictionary = {}
var _syncing := false
var _scroll_sync_pending := false
var _sync_again := false
var _sync_again_stick_to_bottom := false


func setup(scroll: ScrollContainer, message_list: VBoxContainer, store: ChatMessageStore, factory: ChatNodeFactory) -> void:
	_scroll = scroll
	_message_list = message_list
	_store = store
	_factory = factory
	_store.item_spacing = float(message_list.get_theme_constant("separation"))
	_top_spacer = _make_spacer("TopSpacer")
	_bottom_spacer = _make_spacer("BottomSpacer")


func notify_message_added(index: int, stick_to_bottom: bool) -> void:
	sync(float(_scroll.scroll_vertical) if _scroll != null else 0.0, stick_to_bottom)


func refresh_message(index: int, stick_to_bottom: bool) -> void:
	if _store == null or index < 0 or index >= _store.size():
		return
	if _node_cache.has(index):
		var old_node: Control = _node_cache[index]
		var new_node: Control = _factory.create(_store.get_message(index))
		if new_node != null:
			var child_index := old_node.get_index() if old_node.get_parent() == _message_list else -1
			if child_index >= 0:
				_message_list.remove_child(old_node)
				_message_list.add_child(new_node)
				_message_list.move_child(new_node, child_index)
			else:
				_message_list.add_child(new_node)
			_node_cache[index] = new_node
			if old_node != new_node and is_instance_valid(old_node):
				old_node.queue_free()
	sync(float(_scroll.scroll_vertical) if _scroll != null else 0.0, stick_to_bottom)


func on_scroll_changed(_scroll_y: float) -> void:
	if _scroll_sync_pending:
		return
	_scroll_sync_pending = true
	call_deferred("_deferred_scroll_sync")


func _deferred_scroll_sync() -> void:
	_scroll_sync_pending = false
	if _scroll != null:
		sync(float(_scroll.scroll_vertical), false)


func sync(scroll_y: float, stick_to_bottom: bool) -> void:
	if _message_list == null or _store == null:
		return
	if _syncing:
		_sync_again = true
		_sync_again_stick_to_bottom = _sync_again_stick_to_bottom or stick_to_bottom
		return
	_syncing = true
	_measure_visible_heights()
	var visible_range := _compute_visible_range(scroll_y, stick_to_bottom)
	var visible := _visible_indexes(visible_range)
	var excluded := _pinned_indexes_outside(visible)
	_top_spacer.custom_minimum_size = Vector2(0, _store.total_height(0, visible_range.x, excluded))
	_bottom_spacer.custom_minimum_size = Vector2(0, _store.total_height(visible_range.y, _store.size(), excluded))
	_sync_nodes(visible)
	_syncing = false
	if _sync_again:
		var next_stick_to_bottom := _sync_again_stick_to_bottom
		_sync_again = false
		_sync_again_stick_to_bottom = false
		call_deferred("_deferred_resync", next_stick_to_bottom)


func _deferred_resync(stick_to_bottom: bool) -> void:
	sync(float(_scroll.scroll_vertical) if _scroll != null else 0.0, stick_to_bottom)


func clear() -> void:
	if _store != null:
		for index in range(_store.size()):
			var message := _store.get_message(index)
			if bool(message.get("external", false)):
				var external_node: Control = message.get("node", null)
				if external_node != null and is_instance_valid(external_node):
					if external_node.get_parent() == _message_list:
						_message_list.remove_child(external_node)
					external_node.queue_free()
	for index in _node_cache.keys():
		var cached_message := _store.get_message(int(index)) if _store != null else {}
		if bool(cached_message.get("external", false)):
			continue
		var node = _node_cache[index]
		if node is Control and is_instance_valid(node):
			if node.get_parent() == _message_list:
				_message_list.remove_child(node)
			node.queue_free()
	_node_cache.clear()
	if _message_list != null:
		for child in _message_list.get_children():
			_message_list.remove_child(child)


func remove_external_node(node: Control) -> void:
	for index in _node_cache.keys():
		if _node_cache[index] == node:
			_node_cache.erase(index)
			break
	if node != null and is_instance_valid(node):
		if node.get_parent() == _message_list:
			_message_list.remove_child(node)
		node.queue_free()


func remove_message(index: int) -> void:
	if _store == null or index < 0 or index >= _store.size():
		return
	var removed_message := _store.get_message(index)
	if _node_cache.has(index):
		var node: Control = _node_cache[index]
		if node.get_parent() == _message_list:
			_message_list.remove_child(node)
		node.queue_free()
		_node_cache.erase(index)
	elif bool(removed_message.get("external", false)):
		var external_node: Control = removed_message.get("node", null)
		if external_node != null and is_instance_valid(external_node):
			if external_node.get_parent() == _message_list:
				_message_list.remove_child(external_node)
			external_node.queue_free()
	var shifted := {}
	for cached_index in _node_cache.keys():
		shifted[int(cached_index) - 1 if int(cached_index) > index else int(cached_index)] = _node_cache[cached_index]
	_node_cache = shifted
	_store.remove_message(index)
	sync(float(_scroll.scroll_vertical) if _scroll != null else 0.0, false)


func _compute_visible_range(scroll_y: float, stick_to_bottom: bool) -> Vector2i:
	var total := _store.size()
	if total == 0:
		return Vector2i(0, 0)
	var viewport_height := maxf(1.0, _scroll.size.y if _scroll != null else 600.0)
	if stick_to_bottom:
		var end := total
		var start := _store.find_index_at_scroll(maxf(0.0, _store.total_height() - viewport_height))
		var first := maxi(0, start - BUFFER_MESSAGES)
		# 确保至少渲染 MIN_VISIBLE_MESSAGES 条，向上扩展
		if end - first < MIN_VISIBLE_MESSAGES:
			first = maxi(0, end - MIN_VISIBLE_MESSAGES)
		return Vector2i(first, end)
	var first := _store.find_index_at_scroll(scroll_y)
	var last := _store.find_index_at_scroll(scroll_y + viewport_height)
	var range_start := maxi(0, first - BUFFER_MESSAGES)
	var range_end := mini(total, last + BUFFER_MESSAGES + 1)
	# 确保至少渲染 MIN_VISIBLE_MESSAGES 条，向两侧对称扩展
	if range_end - range_start < MIN_VISIBLE_MESSAGES:
		var deficit := MIN_VISIBLE_MESSAGES - (range_end - range_start)
		var half := deficit / 2
		range_start = maxi(0, range_start - half)
		range_end = mini(total, range_end + (deficit - half))
		# 若一侧到顶，把余量给另一侧
		if range_start == 0:
			range_end = mini(total, MIN_VISIBLE_MESSAGES)
		elif range_end == total:
			range_start = maxi(0, total - MIN_VISIBLE_MESSAGES)
	return Vector2i(range_start, range_end)


func _visible_indexes(visible_range: Vector2i) -> Dictionary:
	var indexes := {}
	for index in range(visible_range.x, visible_range.y):
		indexes[index] = true
	for index in range(_store.size()):
		var message := _store.get_message(index)
		if bool(message.get("keep_visible", false)):
			indexes[index] = true
	return indexes


func _pinned_indexes_outside(visible: Dictionary) -> Dictionary:
	var excluded := {}
	for index in visible.keys():
		if index < 0 or index >= _store.size():
			continue
		var message := _store.get_message(index)
		if bool(message.get("keep_visible", false)):
			excluded[index] = true
	return excluded


func _sync_nodes(visible: Dictionary) -> void:
	# ── 1. 移除不再可见的节点 ──
	for index in _node_cache.keys().duplicate():
		if visible.has(index):
			continue
		var old_node: Control = _node_cache[index]
		if old_node.get_parent() == _message_list:
			_message_list.remove_child(old_node)
		var old_message := _store.get_message(int(index))
		if not bool(old_message.get("external", false)):
			old_node.queue_free()
		_node_cache.erase(index)

	# ── 2. 按 index 升序添加新节点（减少后续 move_child） ──
	var new_indexes: Array = []
	for index in visible.keys():
		if not _node_cache.has(index):
			new_indexes.append(index)
	new_indexes.sort()
	for index in new_indexes:
		var message := _store.get_message(int(index))
		var node: Control = message.get("node", null) if bool(message.get("external", false)) else _factory.create(message)
		if node != null:
			_node_cache[index] = node
			_message_list.add_child(node)

	# ── 3. 确保 spacer 在节点树中 ──
	if _top_spacer.get_parent() != _message_list:
		_message_list.add_child(_top_spacer)
	if _bottom_spacer.get_parent() != _message_list:
		_message_list.add_child(_bottom_spacer)

	# ── 4. 排序：只在位置不对时才 move_child，避免无谓的布局重算 ──
	var sorted_visible: Array = visible.keys()
	sorted_visible.sort()
	if _message_list.get_child(0) != _top_spacer:
		_message_list.move_child(_top_spacer, 0)
	var pos := 1
	for index in sorted_visible:
		var node: Control = _node_cache[index]
		if pos < _message_list.get_child_count() and _message_list.get_child(pos) != node:
			_message_list.move_child(node, pos)
		pos += 1
	if pos < _message_list.get_child_count() and _message_list.get_child(pos) != _bottom_spacer:
		_message_list.move_child(_bottom_spacer, pos)
	elif pos >= _message_list.get_child_count():
		_message_list.move_child(_bottom_spacer, pos)


func _measure_visible_heights() -> void:
	for index in _node_cache.keys():
		var node: Control = _node_cache[index]
		if node != null and is_instance_valid(node) and node.size.y > 1.0:
			_store.update_height(int(index), node.size.y)


func _make_spacer(name: String) -> Control:
	var spacer := Control.new()
	spacer.name = name
	spacer.mouse_filter = Control.MOUSE_FILTER_IGNORE
	spacer.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	return spacer
