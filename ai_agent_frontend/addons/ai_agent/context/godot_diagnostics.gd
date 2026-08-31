@tool
extends RefCounted

## Godot 输出诊断的最大返回数量与单条原文长度，防止工具结果膨胀。
const MAX_ITEMS := 20
const MAX_RAW_CHARS := 2000


## 创建一次工具执行专用的诊断关联标识。
static func operation_id(source: String) -> String:
	return "%s:%d:%d" % [source, Time.get_ticks_usec(), randi()]


## 将本次操作捕获的 Godot 输出转换为可供模型修复的结构化诊断。
## 未能可靠识别源码位置时保持 0，而不是把日志文件行号伪装为源码位置。
static func from_output(output: String, source: String, execution_id: String, affected_paths: Array = []) -> Array:
	var diagnostics: Array = []
	var lines := output.split("\n")
	for index in range(lines.size()):
		var raw := str(lines[index]).strip_edges()
		if not is_diagnostic_line(raw):
			continue
		var location := _location_from_text(raw)
		if str(location.get("resource_path", "")) == "" and index + 1 < lines.size():
			location = _location_from_text(raw + "\n" + str(lines[index + 1]))
		var resource_path := str(location.get("resource_path", ""))
		if resource_path == "" and affected_paths.size() == 1:
			resource_path = str(affected_paths[0])
		diagnostics.append({
			"source": source,
			"severity": _severity(raw),
			"resource_path": resource_path,
			"line": int(location.get("line", 0)),
			"column": int(location.get("column", 0)),
			"message": _message(raw),
			"raw_text": raw.left(MAX_RAW_CHARS),
			"execution_id": execution_id,
		})
		if diagnostics.size() >= MAX_ITEMS:
			break
	return diagnostics


## 生成没有编造位置的当前操作失败诊断。
static func unlocated(source: String, execution_id: String, resource_path: String, message: String) -> Dictionary:
	return {
		"source": source,
		"severity": "error",
		"resource_path": resource_path,
		"line": 0,
		"column": 0,
		"message": message.left(MAX_RAW_CHARS),
		"raw_text": message.left(MAX_RAW_CHARS),
		"execution_id": execution_id,
	}


static func is_diagnostic_line(line: String) -> bool:
	var lower := line.to_lower()
	return line.begins_with("ERROR:") or line.begins_with("SCRIPT ERROR:") or line.begins_with("错误") or line.begins_with("脚本错误") or lower.find("parse error") >= 0 or lower.find("parser error") >= 0 or lower.find("compile error") >= 0


static func _severity(line: String) -> String:
	var lower := line.to_lower()
	return "warning" if (lower.find("warning") >= 0 or line.find("警告") >= 0) and lower.find("error") < 0 and line.find("错误") < 0 else "error"


static func _message(line: String) -> String:
	var message := line.trim_prefix("SCRIPT ERROR:").trim_prefix("ERROR:").strip_edges()
	return message if message != "" else line


static func _location_from_text(text: String) -> Dictionary:
	var regex := RegEx.new()
	if regex.compile("(res://[^\\s:)]+)(?::(\\d+))?(?::(\\d+))?") != OK:
		return {}
	var match := regex.search(text)
	if match != null:
		return {
			"resource_path": str(match.get_string(1)),
			"line": int(match.get_string(2)) if match.get_string(2).is_valid_int() else 0,
			"column": int(match.get_string(3)) if match.get_string(3).is_valid_int() else 0,
		}
	var localized := RegEx.new()
	if localized.compile("[\\(（]\\s*(\\d+)\\s*[,，]\\s*(\\d+)\\s*[\\)）]") != OK:
		return {}
	var localized_match := localized.search(text)
	if localized_match == null:
		return {}
	return {"resource_path": "", "line": int(localized_match.get_string(1)), "column": int(localized_match.get_string(2))}
