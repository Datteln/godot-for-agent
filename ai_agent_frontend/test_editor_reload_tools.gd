## 编辑器 reload 边界与诚实结果的无 UI 回归测试。
extends SceneTree

const EditorReloadTools = preload("res://addons/ai_agent/tools/editor_reload_tools.gd")
const ProgramTools = preload("res://addons/ai_agent/tools/program_tools.gd")
const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")
const GodotDiagnostics = preload("res://addons/ai_agent/context/godot_diagnostics.gd")
const EventFormatter = preload("res://addons/ai_agent/ui/event_formatter.gd")

var _failures := 0
var _checks := 0


func _init() -> void:
	_check(PathUtils.to_godot_path("maps/level.tscn") == "res://maps/level.tscn", "relative path canonicalizes to project resource URI")
	_check(PathUtils.to_godot_path("res://maps/level.tscn") == "res://maps/level.tscn", "resource URI remains canonical")
	_check(PathUtils.to_godot_path("user://ai_agent_outputs/preview.json") == "user://ai_agent_outputs/preview.json", "user-data URI remains in user namespace")
	_check(PathUtils.is_res_path(PathUtils.to_godot_path("res://maps/level.tscn")), "project resource identity is recognized")
	_check(PathUtils.is_user_path(PathUtils.to_godot_path("user://ai_agent_outputs/preview.json")), "user-data identity is recognized")
	_check(PathUtils.to_godot_path("C:/outside/level.tscn") == "", "system absolute path rejected")
	_check(PathUtils.to_godot_path("res://maps/../outside.tscn") == "", "resource traversal path rejected")
	_check(EditorReloadTools._normalized_paths(["res://maps/level.tscn"]) == ["res://maps/level.tscn"], "project-relative reload path accepted")
	_check(EditorReloadTools._normalized_paths(["user://ai_agent_outputs/preview.tscn"]).is_empty(), "user-data reload path rejected")
	_check(EditorReloadTools._contains_user_data_path(["user://ai_agent_outputs/preview.tscn"]), "user-data reload request is identified for typed rejection")
	_check(EditorReloadTools._normalized_paths(["../outside.tscn"]).is_empty(), "outside reload path rejected")
	_check(EditorReloadTools._targets_are_approved(["res://maps/level.tscn"], ["res://maps/level.tscn"]), "approved reload target accepted")
	_check(not EditorReloadTools._targets_are_approved(["res://maps/level.tscn"], ["res://maps/other.tscn"]), "unapproved reload target rejected")
	_check(EditorReloadTools._dirty_scene_target(["res://maps/level.tscn"], ["res://maps/level.tscn"]) == "res://maps/level.tscn", "dirty target blocks reload")
	_check(EditorReloadTools._dirty_scene_target(["res://maps/generator.gd"], ["res://maps/level.tscn"]) == "", "unrelated dirty scene is preserved")
	_check(EditorReloadTools._closed_scene_targets(["res://maps/builder.gd", "res://maps/level.tscn"], ["res://maps/other.tscn"]) == ["res://maps/level.tscn"], "closed target scene blocks reload before resources are reloaded")
	_check(EditorReloadTools._ordered_reload_targets(["res://maps/level.tscn", "res://maps/map_builder.gd", "res://maps/tiles.tres"]) == ["res://maps/map_builder.gd", "res://maps/tiles.tres", "res://maps/level.tscn"], "scripts and resources reload before scenes")
	var invalid_path_observation := EditorReloadTools._script_path_observation("../outside.gd")
	_check(str(invalid_path_observation.get("raw_resource_path", "")) == "../outside.gd", "script observation preserves raw resource path")
	_check(str(invalid_path_observation.get("normalized_resource_path", "")) == "", "script observation rejects unsafe resource path")
	_check(not bool(invalid_path_observation.get("exists", true)), "unsafe resource path is never reported as existing")
	var missing_script := EditorReloadTools._builder_script_missing("res://missing_builder.gd")
	_check(str(missing_script.get("error_code", "")) == "builder_script_missing", "missing script remains typed")
	_check(missing_script.has("globalized_path") and missing_script.has("exists"), "missing script includes path observability facts")
	_check("runtime_only" in EditorReloadTools.SUPPORTED_MODES, "runtime-only mode is explicitly unavailable rather than executed")
	var bootstrap_scene := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://maps/level.tscn", "tile_map_data = PackedByteArray()", "tile_map_data = PackedByteArray()")
	_check(bool(bootstrap_scene.get("ok", false)), "scene file remains eligible for an approved @tool bootstrap")
	var user_map_source := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "user://map_layouts/ground_extension.json", "", "{}")
	_check(str(user_map_source.get("error_code", "")) == "map_authoring_requires_project_resource", "map authoring source rejects user-data namespace")
	var blocked_extension := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://maps/level.png", "", "")
	_check(str(blocked_extension.get("error_code", "")) == "unsupported_map_authoring_target", "opaque map asset still yields typed rejection")
	var outside_layout_json := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://maps/floor_extension.json", "", "{}")
	_check(str(outside_layout_json.get("error_code", "")) == "map_authoring_requires_layout_dir", "layout outside map_layouts is rejected")
	var layout_dir_json := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://map_layouts/floor_extension.json", "", "{}")
	_check(bool(layout_dir_json.get("ok", false)), "layout under map_layouts is accepted")
	var outside_layout_builder := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://maps/floor_extension_builder.gd", "", "")
	_check(str(outside_layout_builder.get("error_code", "")) == "map_authoring_requires_layout_dir", "builder outside map_layouts is rejected")
	var layout_dir_builder := ProgramTools._validate_map_authoring_target({"workflow": "code_driven_map"}, "res://map_layouts/floor_extension_builder.gd", "", "")
	_check(bool(layout_dir_builder.get("ok", false)), "builder under map_layouts is accepted")
	var parser_output := "SCRIPT ERROR: Parse Error: Expected expression after '='\n          at: GDScript::reload (res://scripts/map_builder.gd:17)"
	var parser_diagnostics := GodotDiagnostics.from_output(parser_output, "gdscript_validation", "test-operation", ["res://scripts/map_builder.gd"])
	_check(parser_diagnostics.size() == 1, "parser output creates one structured diagnostic")
	var parser_diagnostic: Dictionary = parser_diagnostics[0] if not parser_diagnostics.is_empty() else {}
	_check(str(parser_diagnostic.get("resource_path", "")) == "res://scripts/map_builder.gd", "parser diagnostic carries source resource")
	_check(int(parser_diagnostic.get("line", 0)) == 17, "parser diagnostic carries source line")
	_check(int(parser_diagnostic.get("column", 0)) == 0, "unreported source column remains unlocated")
	_check(str(parser_diagnostic.get("execution_id", "")) == "test-operation", "parser diagnostic retains operation correlation")
	var localized_output := "错误 (82, 35)： Too many arguments for add_layer() call. Expected at most 1 but received 2."
	var localized_diagnostics := GodotDiagnostics.from_output(localized_output, "gdscript_validation", "localized-operation", ["res://map_layouts/floor_extension_builder.gd"])
	_check(localized_diagnostics.size() == 1, "localized Godot error creates one structured diagnostic")
	var localized_diagnostic: Dictionary = localized_diagnostics[0] if not localized_diagnostics.is_empty() else {}
	_check(str(localized_diagnostic.get("resource_path", "")) == "res://map_layouts/floor_extension_builder.gd", "localized diagnostic uses affected script path")
	_check(int(localized_diagnostic.get("line", 0)) == 82 and int(localized_diagnostic.get("column", 0)) == 35, "localized diagnostic carries source line and column")
	var compile_failure := EditorReloadTools._builder_compile_failure_result("Godot could not compile the builder script", "res://map_layouts/floor_extension_builder.gd", {"raw_resource_path": "res://map_layouts/floor_extension_builder.gd", "normalized_resource_path": "res://map_layouts/floor_extension_builder.gd", "globalized_path": "C:/project/map_layouts/floor_extension_builder.gd", "exists": true}, localized_diagnostics)
	_check(not bool(compile_failure.get("ok", true)), "builder compile failure remains unsuccessful")
	_check(str(compile_failure.get("next_action", "")) == "fix_builder_script", "builder compile failure directs repair before lifecycle actions")
	_check("Too many arguments" in str(compile_failure.get("raw_diagnostic", "")), "builder compile failure preserves bounded raw diagnostic text")
	var debugger_summary := EventFormatter.format_tool_result_detail("read_debugger_errors", {}, "applied", {"items": [localized_diagnostic], "error_count": 1, "warning_count": 0}, {})
	_check("1 error" in debugger_summary, "debugger read result with errors is rendered as errors rather than a clean result")
	var historical := GodotDiagnostics.unlocated("godot_log_historical", "old-operation", "", "ERROR: old command failure")
	_check(int(historical.get("line", 0)) == 0 and str(historical.get("resource_path", "")) == "", "historical log diagnostics never fabricate source locations")
	print("editor reload tool checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)
