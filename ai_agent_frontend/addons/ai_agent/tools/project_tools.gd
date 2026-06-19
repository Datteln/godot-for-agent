@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")


static func set_project_setting(input: Dictionary, undo_manager: Node) -> Dictionary:
	var key := str(input.get("key", "")).strip_edges()
	if key == "":
		return {"ok": false, "message": "key is required", "error_code": "key_required"}
	var before: Variant = ProjectSettings.get_setting(key) if ProjectSettings.has_setting(key) else null
	var after: Variant = input.get("value")
	if undo_manager != null:
		undo_manager.record_project_setting(key, before, after)
	else:
		ProjectSettings.set_setting(key, after)
		ProjectSettings.save()
	return {"ok": true, "key": key, "before": before, "after": after}


static func read_project_setting(input: Dictionary) -> Dictionary:
	var key := str(input.get("key", "")).strip_edges()
	if key == "":
		return {"ok": false, "message": "key is required", "error_code": "key_required"}
	if not ProjectSettings.has_setting(key):
		return {"ok": true, "key": key, "value": null, "exists": false}
	return {"ok": true, "key": key, "value": ProjectSettings.get_setting(key), "exists": true}


static func list_autoloads() -> Dictionary:
	var autoloads: Array = []
	for prop in ProjectSettings.get_property_list():
		var setting_name := str(prop.get("name", ""))
		if not setting_name.begins_with("autoload/"):
			continue
		var autoload_name := setting_name.trim_prefix("autoload/")
		var raw := str(ProjectSettings.get_setting(setting_name, ""))
		autoloads.append({
			"name": autoload_name,
			"path": raw.trim_prefix("*"),
			"enabled": raw.begins_with("*")
		})
	return {"ok": true, "autoloads": autoloads}


static func add_autoload(input: Dictionary, undo_manager: Node) -> Dictionary:
	var name := str(input.get("name", "")).strip_edges()
	if name == "" or name.contains("/") or name.contains("."):
		return {"ok": false, "message": "name must be a simple identifier", "error_code": "invalid_name"}
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	if path == "" or not (path.get_extension().to_lower() in ["gd", "tscn", "scn", "cs"]):
		return {
			"ok": false,
			"message": "path must be a project-relative script or scene file",
			"error_code": "invalid_path"
		}
	if not FileAccess.file_exists(path):
		return {"ok": false, "message": "file not found: " + path, "error_code": "path_not_found"}
	var enabled := bool(input.get("enabled", true))
	var key := "autoload/" + name
	var before: Variant = ProjectSettings.get_setting(key) if ProjectSettings.has_setting(key) else null
	var after := ("*" if enabled else "") + path
	if undo_manager != null:
		undo_manager.record_project_setting(key, before, after)
	else:
		ProjectSettings.set_setting(key, after)
		ProjectSettings.save()
	return {"ok": true, "name": name, "path": path, "enabled": enabled}


static func remove_autoload(input: Dictionary, undo_manager: Node) -> Dictionary:
	var name := str(input.get("name", "")).strip_edges()
	if name == "":
		return {"ok": false, "message": "name is required", "error_code": "name_required"}
	var key := "autoload/" + name
	if not ProjectSettings.has_setting(key):
		return {"ok": false, "message": "autoload not found: " + name, "error_code": "autoload_not_found"}
	var before: Variant = ProjectSettings.get_setting(key)
	if undo_manager != null:
		undo_manager.record_project_setting(key, before, null)
	else:
		ProjectSettings.set_setting(key, null)
		ProjectSettings.save()
	return {"ok": true, "name": name}


static func list_input_actions() -> Dictionary:
	var actions: Array = []
	for prop in ProjectSettings.get_property_list():
		var setting_name := str(prop.get("name", ""))
		if not setting_name.begins_with("input/"):
			continue
		var action_name := setting_name.trim_prefix("input/")
		var raw: Dictionary = ProjectSettings.get_setting(setting_name, {})
		var events: Array = raw.get("events", [])
		var event_texts: Array = []
		for event in events:
			if event is InputEvent:
				event_texts.append(event.as_text())
		actions.append({
			"action": action_name,
			"deadzone": float(raw.get("deadzone", 0.5)),
			"events": event_texts
		})
	return {"ok": true, "actions": actions}


## 用 `keys`（按键名，如 "A"/"Space"/"Enter"，经 `OS.find_keycode_from_string` 解析）
## 和/或 `mouse_buttons`（"left"/"right"/"middle"/"wheel_up"/"wheel_down" 等）整体替换
## 该 action 的绑定；要追加而不是替换，先用 `list_input_actions` 读出现有绑定再一起传入。
static func add_input_action(input: Dictionary, undo_manager: Node) -> Dictionary:
	var action := str(input.get("action", "")).strip_edges()
	if action == "":
		return {"ok": false, "message": "action is required", "error_code": "action_required"}
	var deadzone := float(input.get("deadzone", 0.5))
	var events: Array = []
	var keys: Array = input.get("keys", [])
	for key_name in keys:
		var keycode := OS.find_keycode_from_string(str(key_name))
		if keycode == KEY_NONE:
			return {"ok": false, "message": "Unknown key name: " + str(key_name), "error_code": "invalid_key"}
		var event := InputEventKey.new()
		event.keycode = keycode
		event.physical_keycode = keycode
		events.append(event)
	var mouse_buttons: Array = input.get("mouse_buttons", [])
	for button_name in mouse_buttons:
		var button_index := _mouse_button_from_string(str(button_name))
		if button_index == 0:
			return {"ok": false, "message": "Unknown mouse button name: " + str(button_name), "error_code": "invalid_mouse_button"}
		var mb := InputEventMouseButton.new()
		mb.button_index = button_index
		events.append(mb)
	if events.is_empty():
		return {"ok": false, "message": "keys or mouse_buttons is required", "error_code": "events_required"}

	var key := "input/" + action
	var before: Variant = ProjectSettings.get_setting(key) if ProjectSettings.has_setting(key) else null
	var after := {"deadzone": deadzone, "events": events}
	if undo_manager != null:
		undo_manager.record_project_setting(key, before, after)
	else:
		ProjectSettings.set_setting(key, after)
		ProjectSettings.save()
	var event_texts: Array = []
	for event in events:
		event_texts.append(event.as_text())
	return {"ok": true, "action": action, "deadzone": deadzone, "events": event_texts}


static func remove_input_action(input: Dictionary, undo_manager: Node) -> Dictionary:
	var action := str(input.get("action", "")).strip_edges()
	if action == "":
		return {"ok": false, "message": "action is required", "error_code": "action_required"}
	var key := "input/" + action
	if not ProjectSettings.has_setting(key):
		return {"ok": false, "message": "input action not found: " + action, "error_code": "action_not_found"}
	var before: Variant = ProjectSettings.get_setting(key)
	if undo_manager != null:
		undo_manager.record_project_setting(key, before, null)
	else:
		ProjectSettings.set_setting(key, null)
		ProjectSettings.save()
	return {"ok": true, "action": action}


static func _mouse_button_from_string(name: String) -> int:
	match name.to_lower().strip_edges():
		"left":
			return MOUSE_BUTTON_LEFT
		"right":
			return MOUSE_BUTTON_RIGHT
		"middle":
			return MOUSE_BUTTON_MIDDLE
		"wheel_up":
			return MOUSE_BUTTON_WHEEL_UP
		"wheel_down":
			return MOUSE_BUTTON_WHEEL_DOWN
		"wheel_left":
			return MOUSE_BUTTON_WHEEL_LEFT
		"wheel_right":
			return MOUSE_BUTTON_WHEEL_RIGHT
		"xbutton1":
			return MOUSE_BUTTON_XBUTTON1
		"xbutton2":
			return MOUSE_BUTTON_XBUTTON2
		_:
			return 0


static func list_export_presets() -> Dictionary:
	var cfg := ConfigFile.new()
	var err := cfg.load("res://export_presets.cfg")
	if err != OK:
		return {
			"ok": false,
			"message": "export_presets.cfg not found or unreadable (error %d)" % err,
			"error_code": "presets_not_found"
		}
	var presets: Array = []
	for section in cfg.get_sections():
		var parts := section.split(".")
		if parts.size() != 2 or parts[0] != "preset":
			continue
		presets.append({
			"name": str(cfg.get_value(section, "name", "")),
			"platform": str(cfg.get_value(section, "platform", "")),
			"export_path": str(cfg.get_value(section, "export_path", "")),
			"runnable": bool(cfg.get_value(section, "runnable", false))
		})
	return {"ok": true, "presets": presets}
