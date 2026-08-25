## 编辑器 reload 边界与诚实结果的无 UI 回归测试。
extends SceneTree

const EditorReloadTools = preload("res://addons/ai_agent/tools/editor_reload_tools.gd")
const ProgramTools = preload("res://addons/ai_agent/tools/program_tools.gd")

var _failures := 0
var _checks := 0


func _init() -> void:
	_check(EditorReloadTools._normalized_paths(["res://maps/level.tscn"]) == ["res://maps/level.tscn"], "project-relative reload path accepted")
	_check(EditorReloadTools._normalized_paths(["../outside.tscn"]).is_empty(), "outside reload path rejected")
	_check(EditorReloadTools._targets_are_approved(["res://maps/level.tscn"], ["res://maps/level.tscn"]), "approved reload target accepted")
	_check(not EditorReloadTools._targets_are_approved(["res://maps/level.tscn"], ["res://maps/other.tscn"]), "unapproved reload target rejected")
	_check(EditorReloadTools._dirty_scene_target(["res://maps/level.tscn"], ["res://maps/level.tscn"]) == "res://maps/level.tscn", "dirty target blocks reload")
	_check(EditorReloadTools._dirty_scene_target(["res://maps/generator.gd"], ["res://maps/level.tscn"]) == "", "unrelated dirty scene is preserved")
	_check("runtime_only" in EditorReloadTools.SUPPORTED_MODES, "runtime-only mode is explicitly unavailable rather than executed")
	var bootstrap_scene := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://maps/level.tscn", "tile_map_data = PackedByteArray()", "tile_map_data = PackedByteArray()")
	_check(bool(bootstrap_scene.get("ok", false)), "scene file remains eligible for an approved @tool bootstrap")
	var blocked_extension := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://maps/level.png", "", "")
	_check(str(blocked_extension.get("error_code", "")) == "unsupported_map_authoring_target", "opaque map asset still yields typed rejection")
	print("editor reload tool checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)
