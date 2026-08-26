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


## 将相对路径、res:// 与 user:// 规范化为安全的 Godot URI。
## 相对路径属于项目资源并补为 res://；user:// 保留独立的项目用户数据命名空间。
## 操作系统绝对路径、空路径与任何越界路径返回 ""。
static func to_res_path(path: String) -> String:
	return to_godot_path(path)


static func to_godot_path(path: String) -> String:
	var cleaned := path.strip_edges().replace("\\", "/")
	if cleaned == "":
		return ""
	var scheme := "res://"
	if cleaned.begins_with("res://"):
		scheme = "res://"
	elif cleaned.begins_with("user://"):
		scheme = "user://"
	elif cleaned.is_absolute_path():
		return ""

	var relative := cleaned.trim_prefix(scheme).trim_prefix("/")
	for part in relative.split("/", false):
		if part == "..":
			return ""
	if relative == "":
		return ""
	# 只简化相对部分，避免 String.simplify_path() 改写 Godot URI 的双斜线 scheme。
	var normalized_relative := relative.simplify_path().trim_prefix("./").trim_prefix("/")
	if normalized_relative == "" or normalized_relative == "." or normalized_relative.begins_with("../"):
		return ""
	return scheme + normalized_relative


static func is_res_path(path: String) -> bool:
	return path.begins_with("res://")


static func is_user_path(path: String) -> bool:
	return path.begins_with("user://")


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
