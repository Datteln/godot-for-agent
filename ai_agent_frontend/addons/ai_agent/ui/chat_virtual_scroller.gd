@tool
extends RefCounted

const ChatMessageStore = preload("res://addons/ai_agent/ui/chat_message_store.gd")
const ChatNodeFactory = preload("res://addons/ai_agent/ui/chat_node_factory.gd")

const BUFFER_MESSAGES := 3

var _scroll: ScrollContainer
var _message_list: VBoxContainer
var _store: ChatMessageStore
var _factory: ChatNodeFactory
var _top_spacer: Control
var _bottom_spacer: Control
var _node_cache: Dictionary = {}
var _syncing := false


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


func on_scroll_changed(scroll_y: float) -> void:
	sync(scroll_y, false)


func sync(scroll_y: float, stick_to_bottom: bool) -> void:
	if _syncing or _message_list == null or _store == null:
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
		return Vector2i(maxi(0, start - BUFFER_MESSAGES), end)
	var first := _store.find_index_at_scroll(scroll_y)
	var last := _store.find_index_at_scroll(scroll_y + viewport_height)
	return Vector2i(maxi(0, first - BUFFER_MESSAGES), mini(total, last + BUFFER_MESSAGES + 1))


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

	for index in visible.keys():
		if _node_cache.has(index):
			continue
		var message := _store.get_message(int(index))
		var node: Control = message.get("node", null) if bool(message.get("external", false)) else _factory.create(message)
		if node != null:
			_node_cache[index] = node

	for child in _message_list.get_children():
		_message_list.remove_child(child)
	_message_list.add_child(_top_spacer)
	for index in range(_store.size()):
		if _node_cache.has(index):
			_message_list.add_child(_node_cache[index])
	_message_list.add_child(_bottom_spacer)


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
