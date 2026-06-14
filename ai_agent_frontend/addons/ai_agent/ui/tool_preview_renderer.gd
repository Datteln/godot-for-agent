@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")

const MAX_DIFF_LINES_PER_SIDE := 800
const MAX_DIFF_LINES_SHOWN := 400


static func render_call(call: Dictionary) -> Control:
	var box := VBoxContainer.new()
	box.size_flags_horizontal = Control.SIZE_EXPAND_FILL

	var kind := infer_render_kind(call)
	var title := Label.new()
	title.text = "%s  (%s)" % [str(call.get("name", "")), kind]
	box.add_child(title)

	match kind:
		"diff":
			box.add_child(_render_diff(call))
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
		"fill_rect", "paint_from_image_grid":
			return "map"
		"run_tests", "run_headless_self_test":
			return "run"
		"add_node", "set_node_property", "delete_node", "reparent_node", "rename_node", "create_resource", "create_sprite_frames_from_sheet", "batch_rename", "set_project_setting":
			return "list"
		_:
			return "json"


static func _render_diff(call: Dictionary) -> Control:
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

	var diff_lines := _lcs_diff(before_text, after_text)
	var shown := diff_lines
	var truncated := diff_lines.size() > MAX_DIFF_LINES_SHOWN
	if truncated:
		shown = diff_lines.slice(0, MAX_DIFF_LINES_SHOWN)
	for line in shown:
		var text := str(line)
		var escaped := _escape_bbcode(text)
		if text.begins_with("+ "):
			view.append_text("[color=#88ff88]%s[/color]\n" % escaped)
		elif text.begins_with("- "):
			view.append_text("[color=#ff8888]%s[/color]\n" % escaped)
		else:
			view.append_text("%s\n" % escaped)
	if truncated:
		view.append_text("[i]... diff truncated (%d more lines)[/i]\n" % (diff_lines.size() - MAX_DIFF_LINES_SHOWN))
	return view


static func _render_map_op(call: Dictionary) -> Control:
	var input: Dictionary = call.get("input", {})
	var lines: Array[String] = []
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
		var fallback: Array = []
		for line in a:
			fallback.append("- " + line)
		for line in b:
			fallback.append("+ " + line)
		return fallback

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


static func _escape_bbcode(text: String) -> String:
	return text.replace("[", "[lb]").replace("]", "[rb]")
