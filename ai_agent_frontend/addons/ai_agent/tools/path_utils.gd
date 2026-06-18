@tool
extends RefCounted

## 写操作禁止访问的路径前缀：插件自身、Godot 内部数据与版本控制目录。
const DENY_READ_PREFIXES: PackedStringArray = [
	"res://addons/ai_agent/",
	"res://ai_agent_frontend/",
	"res://ai_agent_service/",
	"res://.ai_agent_service/",
	"res://.godot/",
	"res://.git/",
]

const DENY_WRITE_PREFIXES: PackedStringArray = [
	"res://addons/",
	"res://ai_agent_frontend/",
	"res://ai_agent_service/",
	"res://.ai_agent_service/",
	"res://.godot/",
	"res://.git/",
]


## 将任意输入路径归一化为 res:// 路径；绝对路径、越界路径或空字符串返回 ""。
static func to_res_path(path: String) -> String:
	var cleaned := path.strip_edges().replace("\\", "/")
	if cleaned == "":
		return ""
	if cleaned.is_absolute_path():
		return ""
	if cleaned.begins_with("user://"):
		return ""

	var relative := cleaned.trim_prefix("res://").trim_prefix("/")
	for part in relative.split("/", false):
		if part == "..":
			return ""

	var res_path := cleaned if cleaned.begins_with("res://") else "res://" + relative
	res_path = res_path.simplify_path()
	if not res_path.begins_with("res://"):
		return ""
	return res_path


## 判断给定 res:// 路径是否允许写入（不在 DENY_WRITE_PREFIXES 之内）。
static func is_write_allowed(res_path: String) -> bool:
	if res_path == "":
		return false
	for prefix in DENY_WRITE_PREFIXES:
		if res_path.begins_with(prefix):
			return false
	return true


static func is_read_allowed(res_path: String) -> bool:
	if res_path == "":
		return false
	for prefix in DENY_READ_PREFIXES:
		if res_path.begins_with(prefix):
			return false
	return true
