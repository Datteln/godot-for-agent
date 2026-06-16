## 把 Markdown 文本转换为 RichTextLabel 可用的 BBCode 字符串。
## 所有方法均为静态，调用时通过 theme_colors 字典传入当前主题色。
@tool
extends Object

const _CODE_LANG_ALIASES := {
	"gd": "gdscript", "py": "python", "js": "javascript", "ts": "typescript",
	"cs": "csharp", "yml": "yaml", "sh": "bash", "shell": "bash",
}

const _CODE_LINE_COMMENT := {
	"gdscript": "#", "python": "#", "bash": "#", "yaml": "#", "toml": "#", "ini": "#", "cfg": "#",
	"c": "//", "cpp": "//", "csharp": "//", "java": "//",
	"javascript": "//", "typescript": "//", "go": "//", "rust": "//",
}

const _CODE_KEYWORDS := {
	"gdscript": ["func", "var", "const", "if", "elif", "else", "for", "while", "return", "class",
		"extends", "class_name", "enum", "match", "pass", "break", "continue", "signal", "static",
		"await", "preload", "load", "true", "false", "null", "self", "and", "or", "not", "in", "is",
		"as", "super", "tool"],
	"python": ["def", "class", "if", "elif", "else", "for", "while", "return", "import", "from",
		"as", "with", "try", "except", "finally", "raise", "pass", "break", "continue", "lambda",
		"yield", "async", "await", "global", "nonlocal", "not", "and", "or", "in", "is", "None",
		"True", "False", "self"],
	"json": ["true", "false", "null"],
}

const _TREE_LINE_CHARS := ["├", "└", "│", "─", "┌", "┐", "┘", "┬", "┴", "┤", "┼", "╭", "╮", "╯", "╰"]


static func color_tag(color: Color) -> String:
	return "#" + color.to_html(color.a < 1.0)


static func theme_color_tag(key: String, theme_colors: Dictionary) -> String:
	var value = theme_colors.get(key)
	if value is Color:
		return color_tag(value)
	return "#888888"


static func markdown_to_bbcode(text: String, theme_colors: Dictionary) -> String:
	var result: Array[String] = []
	var in_code := false
	var code_lang := ""
	var lines := text.split("\n")
	var tree_ranges := find_tree_block_ranges(lines)
	var tree_range_index := 0
	var index := 0
	while index < lines.size():
		var line := str(lines[index])
		if line.begins_with("```"):
			if in_code:
				in_code = false
				code_lang = ""
				result.append("[/code][/bgcolor]")
			else:
				in_code = true
				code_lang = normalize_code_lang(line.substr(3))
				result.append("[bgcolor=%s][code]" % theme_color_tag("code_bg", theme_colors))
			index += 1
			continue
		if in_code:
			result.append(highlight_code_line(line, code_lang, theme_colors))
			index += 1
			continue
		while tree_range_index < tree_ranges.size() and int(tree_ranges[tree_range_index].y) <= index:
			tree_range_index += 1
		if tree_range_index < tree_ranges.size() and int(tree_ranges[tree_range_index].x) == index:
			var tree_range: Vector2i = tree_ranges[tree_range_index]
			result.append("[bgcolor=%s][code]" % theme_color_tag("code_bg", theme_colors))
			for tree_index in range(tree_range.x, tree_range.y):
				result.append(escape_bbcode(str(lines[tree_index])))
			result.append("[/code][/bgcolor]")
			index = tree_range.y
			tree_range_index += 1
			continue
		if looks_like_table_start(lines, index):
			var table_lines: Array[String] = []
			while index < lines.size() and str(lines[index]).contains("|"):
				table_lines.append(str(lines[index]))
				index += 1
			result.append(render_markdown_table(table_lines, theme_colors))
			continue
		result.append(markdown_line_to_bbcode(line, theme_colors))
		index += 1
	if in_code:
		result.append("[/code][/bgcolor]")
	return "\n".join(result)


static func normalize_code_lang(raw: String) -> String:
	var token := raw.strip_edges().split(" ")[0].to_lower()
	if _CODE_LANG_ALIASES.has(token):
		return str(_CODE_LANG_ALIASES[token])
	return token


static func render_markdown_table(table_lines: Array[String], theme_colors: Dictionary) -> String:
	if table_lines.size() < 2:
		return escape_bbcode("\n".join(table_lines))
	var header_cells := split_table_row(table_lines[0])
	if header_cells.is_empty():
		return escape_bbcode("\n".join(table_lines))
	var column_count := header_cells.size()
	var bbcode := "[table=%d]" % column_count
	for cell in header_cells:
		bbcode += "[cell][b]%s[/b][/cell]" % format_table_cell(str(cell))
	for row_index in range(2, table_lines.size()):
		var cells := split_table_row(table_lines[row_index])
		for col_index in range(column_count):
			var cell_text := str(cells[col_index]) if col_index < cells.size() else ""
			bbcode += "[cell]%s[/cell]" % format_table_cell(cell_text)
	bbcode += "[/table]"
	return bbcode


static func format_table_cell(cell: String) -> String:
	var escaped := escape_bbcode(cell.strip_edges())
	escaped = replace_inline_code(escaped)
	escaped = replace_bold(escaped)
	return escaped


static func split_table_row(line: String) -> PackedStringArray:
	var trimmed := line.strip_edges()
	if trimmed.begins_with("|"):
		trimmed = trimmed.substr(1)
	if trimmed.ends_with("|"):
		trimmed = trimmed.substr(0, trimmed.length() - 1)
	return trimmed.split("|")


static func markdown_line_to_bbcode(line: String, theme_colors: Dictionary) -> String:
	var escaped := escape_bbcode(line)
	var stripped := line.strip_edges()
	if stripped == "---" or stripped == "***" or stripped == "___":
		return "[color=%s]────────────────────────[/color]" % theme_color_tag("subtle_text", theme_colors)
	if line.begins_with("### "):
		return "[b]" + escape_bbcode(line.substr(4)) + "[/b]"
	if line.begins_with("## "):
		return "[font_size=18][b]" + escape_bbcode(line.substr(3)) + "[/b][/font_size]"
	if line.begins_with("# "):
		return "[font_size=20][b]" + escape_bbcode(line.substr(2)) + "[/b][/font_size]"
	if line.begins_with("- "):
		escaped = "• " + escape_bbcode(line.substr(2))
	elif begins_with_ordered_list(line):
		escaped = escape_bbcode(line)
	escaped = replace_inline_code(escaped)
	escaped = replace_bold(escaped)
	return escaped


static func looks_like_table_start(lines: PackedStringArray, index: int) -> bool:
	if index + 1 >= lines.size():
		return false
	var line := str(lines[index])
	var next := str(lines[index + 1])
	return line.contains("|") and is_markdown_table_separator(next)


static func is_markdown_table_separator(line: String) -> bool:
	var stripped := line.strip_edges()
	if not stripped.contains("|") or not stripped.contains("-"):
		return false
	var allowed := "|-: "
	for index in range(stripped.length()):
		var character := stripped.substr(index, 1)
		if not allowed.contains(character):
			return false
	return true


static func looks_like_tree_line(line: String) -> bool:
	for character in _TREE_LINE_CHARS:
		if line.contains(character):
			return true
	return line.contains("+-- ") or line.contains("|-- ") or line.contains("`-- ")


static func find_tree_block_ranges(lines: PackedStringArray) -> Array:
	var ranges: Array = []
	var in_code := false
	var paragraph_start := -1
	var paragraph_has_tree := false
	for index in range(lines.size()):
		var line := str(lines[index])
		if line.begins_with("```"):
			if paragraph_start >= 0:
				if paragraph_has_tree:
					ranges.append(Vector2i(paragraph_start, index))
				paragraph_start = -1
				paragraph_has_tree = false
			in_code = not in_code
			continue
		if in_code:
			continue
		if line.strip_edges() == "":
			if paragraph_start >= 0:
				if paragraph_has_tree:
					ranges.append(Vector2i(paragraph_start, index))
				paragraph_start = -1
				paragraph_has_tree = false
			continue
		if paragraph_start < 0:
			paragraph_start = index
		if looks_like_tree_line(line):
			paragraph_has_tree = true
	if paragraph_start >= 0 and paragraph_has_tree:
		ranges.append(Vector2i(paragraph_start, lines.size()))
	return ranges


static func begins_with_ordered_list(line: String) -> bool:
	var dot_index := line.find(". ")
	if dot_index <= 0 or dot_index > 4:
		return false
	for index in range(dot_index):
		var code := line.unicode_at(index)
		if code < 48 or code > 57:
			return false
	return true


static func replace_inline_code(text: String) -> String:
	var parts := text.split("`")
	if parts.size() < 3:
		return text
	var result := ""
	for index in range(parts.size()):
		result += str(parts[index])
		if index < parts.size() - 1:
			result += "[code]" if index % 2 == 0 else "[/code]"
	return result


static func replace_bold(text: String) -> String:
	var parts := text.split("**")
	if parts.size() < 3:
		return text
	var result := ""
	for index in range(parts.size()):
		result += str(parts[index])
		if index < parts.size() - 1:
			result += "[b]" if index % 2 == 0 else "[/b]"
	return result


static func escape_bbcode(text: String) -> String:
	return text.replace("[", "[lb]").replace("]", "[rb]")


static func highlight_code_line(line: String, lang: String, theme_colors: Dictionary) -> String:
	var comment_prefix: String = _CODE_LINE_COMMENT.get(lang, "")
	if comment_prefix != "":
		var comment_index := find_comment_index(line, comment_prefix)
		if comment_index >= 0:
			var code_part := line.substr(0, comment_index)
			var comment_part := line.substr(comment_index)
			return highlight_code_segment(code_part, lang, theme_colors) \
				+ "[color=%s]%s[/color]" % [theme_color_tag("syntax_comment", theme_colors), escape_bbcode(comment_part)]
	return highlight_code_segment(line, lang, theme_colors)


static func find_comment_index(line: String, prefix: String) -> int:
	var in_string := ""
	var index := 0
	while index < line.length():
		var character := line.substr(index, 1)
		if in_string != "":
			if character == "\\":
				index += 2
				continue
			if character == in_string:
				in_string = ""
			index += 1
			continue
		if character == "\"" or character == "'":
			in_string = character
			index += 1
			continue
		if line.substr(index, prefix.length()) == prefix:
			return index
		index += 1
	return -1


static func highlight_code_segment(text: String, lang: String, theme_colors: Dictionary) -> String:
	var keywords: Array = _CODE_KEYWORDS.get(lang, [])
	var result := ""
	var index := 0
	var length := text.length()
	while index < length:
		var code := text.unicode_at(index)
		var character := text.substr(index, 1)
		if character == "\"" or character == "'":
			var quote := character
			var end := index + 1
			while end < length:
				var next_char := text.substr(end, 1)
				if next_char == "\\":
					end += 2
					continue
				end += 1
				if next_char == quote:
					break
			var literal := text.substr(index, end - index)
			result += "[color=%s]%s[/color]" % [theme_color_tag("syntax_string", theme_colors), escape_bbcode(literal)]
			index = end
			continue
		if (code >= 65 and code <= 90) or (code >= 97 and code <= 122) or code == 95:
			var end := index
			while end < length:
				var next_code := text.unicode_at(end)
				var is_word := (next_code >= 65 and next_code <= 90) \
					or (next_code >= 97 and next_code <= 122) \
					or (next_code >= 48 and next_code <= 57) \
					or next_code == 95
				if not is_word:
					break
				end += 1
			var word := text.substr(index, end - index)
			if keywords.has(word):
				result += "[color=%s]%s[/color]" % [theme_color_tag("syntax_keyword", theme_colors), escape_bbcode(word)]
			else:
				result += escape_bbcode(word)
			index = end
			continue
		if code >= 48 and code <= 57:
			var end := index
			while end < length:
				var next_char := text.substr(end, 1)
				if (next_char.unicode_at(0) >= 48 and next_char.unicode_at(0) <= 57) or next_char == ".":
					end += 1
				else:
					break
			result += "[color=%s]%s[/color]" % [theme_color_tag("syntax_number", theme_colors), escape_bbcode(text.substr(index, end - index))]
			index = end
			continue
		result += escape_bbcode(character)
		index += 1
	return result
