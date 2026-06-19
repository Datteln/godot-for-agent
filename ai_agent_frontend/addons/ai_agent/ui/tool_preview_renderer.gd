@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")

const MAX_DIFF_LINES_PER_SIDE := 260
const MAX_DIFF_LINES_SHOWN := 240


static func render_call(call: Dictionary, theme_colors: Dictionary = {}) -> Control:
	var box := VBoxContainer.new()
	box.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var kind := infer_render_kind(call)
	var title := Label.new()
	title.text = "%s  (%s)" % [str(call.get("name", "")), kind]
	box.add_child(title)

	match kind:
		"diff":
			box.add_child(_render_diff(call, theme_colors))
		"map":
			box.add_child(_render_map_op(call))
		"run":
			box.add_child(_render_execution_confirm(call))
		"list":
			box.add_child(_render_op_list(call))
		_:
			box.add_child(_render_json(call))
	return box


static func infer_render_kind(call: Dictionary) -> String:
	var explicit := str(call.get("render_kind", ""))
	if explicit != "":
		return explicit
	match str(call.get("name", "")):
		"propose_script_edit", "propose_tests", "apply_text_edit":
			return "diff"
		"edit_map", "fill_rect", "paint_from_image_grid":
			return "map"
		"run_tests", "run_headless_self_test":
			return "run"
		"add_node", "set_node_property", "delete_node", "reparent_node", "rename_node", "create_resource", "create_sprite_frames_from_sheet", "batch_rename", "set_project_setting":
			return "list"
		_:
			return "json"


static func _render_diff(call: Dictionary, theme_colors: Dictionary) -> Control:
	var input: Dictionary = call.get("input", {})
	var path := PathUtils.to_res_path(str(input.get("path", input.get("target_path", ""))))
	var after_text := str(input.get("content", input.get("after_text", input.get("after", ""))))
	var before_text := str(input.get("before_text", input.get("before", "")))
	if before_text == "" and path != "":
		var absolute := ProjectSettings.globalize_path(path)
		if FileAccess.file_exists(absolute):
			before_text = FileAccess.get_file_as_string(absolute)

	var view := RichTextLabel.new()
	view.bbcode_enabled = true
	view.selection_enabled = true
	view.context_menu_enabled = true
	view.scroll_active = true
	view.fit_content = false
	view.custom_minimum_size = Vector2(640, 260)
	view.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	view.append_text("[b]%s[/b]\n" % _escape_bbcode(path if path != "" else "(no path)"))
	var added_color := _color_tag(_theme_color(theme_colors, "success_text", Color(0.12, 0.56, 0.26)))
	var removed_color := _color_tag(_theme_color(theme_colors, "error_text", Color(0.72, 0.20, 0.20)))

	var diff_lines := _lcs_diff(before_text, after_text)
	var shown := diff_lines
	var truncated := diff_lines.size() > MAX_DIFF_LINES_SHOWN
	if truncated:
		shown = diff_lines.slice(0, MAX_DIFF_LINES_SHOWN)
	for line in shown:
		var text := str(line)
		var escaped := _escape_bbcode(text)
		if text.begins_with("+ "):
			view.append_text("[color=%s]%s[/color]\n" % [added_color, escaped])
		elif text.begins_with("- "):
			view.append_text("[color=%s]%s[/color]\n" % [removed_color, escaped])
		else:
			view.append_text("%s\n" % escaped)
	if truncated:
		view.append_text("[i]... diff truncated (%d more lines)[/i]\n" % (diff_lines.size() - MAX_DIFF_LINES_SHOWN))
	return view


## 计算 diff 新增/删除行数，与 `_render_diff` 用同一份 before/after 文本来源，
## 保证统计数字与实际展示的 diff 内容一致（旧实现是单独按 after_text 行数估算，
## 在 before_text 缺失时会把整份文件算成"全部新增"）。必须在工具执行前调用——
## 执行后磁盘上的文件已经变成 after_text，再读就读不到真正的 before 了。
static func diff_stats(call: Dictionary) -> Dictionary:
	var input: Dictionary = call.get("input", {})
	var path := PathUtils.to_res_path(str(input.get("path", input.get("target_path", ""))))
	var after_text := str(input.get("content", input.get("after_text", input.get("after", ""))))
	var before_text := str(input.get("before_text", input.get("before", "")))
	if before_text == "" and path != "":
		var absolute := ProjectSettings.globalize_path(path)
		if FileAccess.file_exists(absolute):
			before_text = FileAccess.get_file_as_string(absolute)
	var added := 0
	var removed := 0
	for line in _lcs_diff(before_text, after_text):
		var text := str(line)
		if text.begins_with("+ "):
			added += 1
		elif text.begins_with("- "):
			removed += 1
	return {"added": added, "removed": removed}


static func _render_map_op(call: Dictionary) -> Control:
	var input: Dictionary = call.get("input", {})
	var lines: Array[String] = []
	if input.has("operations"):
		lines.append("Target: %s" % str(input.get("target_path", "selected/auto-detected map")))
		var operations: Array = input.get("operations", [])
		lines.append("Map operations: %d" % operations.size())
		for index in range(mini(operations.size(), 12)):
			var operation = operations[index]
			if operation is Dictionary:
				lines.append("%d. %s at (%s, %s, %s), size %sx%sx%s" % [
					index + 1,
					str(operation.get("action", "")),
					str(operation.get("x", operation.get("to_x", 0))),
					str(operation.get("y", operation.get("to_y", 0))),
					str(operation.get("z", operation.get("to_z", 0))),
					str(operation.get("width", 1)),
					str(operation.get("height", 1)),
					str(operation.get("depth", 1))
				])
		if operations.size() > 12:
			lines.append("... %d more operation(s)" % (operations.size() - 12))
	if input.has("x"):
		lines.append("Area: (%s, %s) %sx%s" % [
			str(input.get("x", 0)),
			str(input.get("y", 0)),
			str(input.get("width", 1)),
			str(input.get("height", 1))
		])
	if input.has("image_path"):
		lines.append("Image: %s" % str(input.get("image_path", "")))
		var palette: Array = input.get("palette", [])
		lines.append("Palette: %d item(s)" % palette.size())
	if input.has("source_id"):
		lines.append("Tile: source %s atlas(%s, %s) alt=%s" % [
			str(input.get("source_id", -1)),
			str(input.get("atlas_x", 0)),
			str(input.get("atlas_y", 0)),
			str(input.get("alternative_tile", 0))
		])
	if lines.is_empty():
		return _render_json(call)
	var label := Label.new()
	label.text = "\n".join(lines)
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	return label


static func _render_execution_confirm(call: Dictionary) -> Control:
	var input: Dictionary = call.get("input", {})
	var kind := str(input.get("kind", "project"))
	var timeout_ms := int(input.get("timeout_ms", 0))
	var label := Label.new()
	label.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	label.text = "\n".join([
		"Run type: %s" % kind,
		"Timeout: %s" % ("%d ms" % timeout_ms if timeout_ms > 0 else "configured default"),
		"This will start the configured external process and return its output log."
	])
	return label


static func _render_op_list(call: Dictionary) -> Control:
	return _render_json(call)


static func _render_json(call: Dictionary) -> Control:
	var input: Dictionary = call.get("input", {})
	var text := TextEdit.new()
	text.editable = false
	text.context_menu_enabled = true
	text.custom_minimum_size = Vector2(600, 150)
	text.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	text.text = JSON.stringify(input, "\t")
	return text


static func _lcs_diff(before: String, after: String) -> Array:
	var a := before.split("\n")
	var b := after.split("\n")
	var n := a.size()
	var m := b.size()

	if n > MAX_DIFF_LINES_PER_SIDE or m > MAX_DIFF_LINES_PER_SIDE:
		return _bounded_fallback_diff(a, b)

	var lcs: Array = []
	for i in range(n + 1):
		var row: Array = []
		row.resize(m + 1)
		row.fill(0)
		lcs.append(row)
	for i in range(n - 1, -1, -1):
		for j in range(m - 1, -1, -1):
			if a[i] == b[j]:
				lcs[i][j] = lcs[i + 1][j + 1] + 1
			else:
				lcs[i][j] = max(lcs[i + 1][j], lcs[i][j + 1])

	var out: Array = []
	var i := 0
	var j := 0
	while i < n and j < m:
		if a[i] == b[j]:
			out.append("  " + a[i])
			i += 1
			j += 1
		elif lcs[i + 1][j] >= lcs[i][j + 1]:
			out.append("- " + a[i])
			i += 1
		else:
			out.append("+ " + b[j])
			j += 1
	while i < n:
		out.append("- " + a[i])
		i += 1
	while j < m:
		out.append("+ " + b[j])
		j += 1
	return out


static func _bounded_fallback_diff(before_lines: PackedStringArray, after_lines: PackedStringArray) -> Array:
	var fallback: Array = []
	var per_side := maxi(1, int(MAX_DIFF_LINES_SHOWN / 2))
	var before_count = mini(before_lines.size(), per_side)
	var after_count = mini(after_lines.size(), per_side)
	for index in range(before_count):
		fallback.append("- " + str(before_lines[index]))
	if before_lines.size() > before_count:
		fallback.append("- ... (%d more line(s))" % (before_lines.size() - before_count))
	for index in range(after_count):
		fallback.append("+ " + str(after_lines[index]))
	if after_lines.size() > after_count:
		fallback.append("+ ... (%d more line(s))" % (after_lines.size() - after_count))
	return fallback


static func _escape_bbcode(text: String) -> String:
	return text.replace("[", "[lb]").replace("]", "[rb]")


static func _theme_color(theme_colors: Dictionary, key: String, fallback: Color) -> Color:
	var value = theme_colors.get(key, fallback)
	return value if value is Color else fallback


static func _color_tag(color: Color) -> String:
	return "#" + color.to_html(color.a < 1.0)
