@tool
extends Node

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const ClassDBReader = preload("res://addons/ai_agent/context/classdb_reader.gd")
const FileStateCache = preload("res://addons/ai_agent/context/file_state_cache.gd")
const DiagnosticsCollector = preload("res://addons/ai_agent/context/diagnostics_collector.gd")
const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")
const ProgramTools = preload("res://addons/ai_agent/tools/program_tools.gd")
const SceneTools = preload("res://addons/ai_agent/tools/scene_tools.gd")
const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")
const ResourceTools = preload("res://addons/ai_agent/tools/resource_tools.gd")
const ProjectTools = preload("res://addons/ai_agent/tools/project_tools.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

const MAP_WRITE_TOOLS := {
	"edit_map": true,
	"paint_terrain_connect": true,
	"place_map_objects": true,
	"repair_placements": true,
	"repair_layer_coverage": true,
	"repair_map_region": true,
	"compact_spatial_index": true,
	"write_resource_registry": true,
	"save_map_blueprint": true,
	"apply_map_blueprint": true,
	"ensure_standard_map_layers": true,
	"fill_rect": true,
	"paint_from_image_grid": true,
}

const MAP_REVISION_GUARDED_TOOLS := {
	"edit_map": true,
	"paint_terrain_connect": true,
	"place_map_objects": true,
	"repair_placements": true,
	"repair_layer_coverage": true,
	"repair_map_region": true,
	"compact_spatial_index": true,
	"save_map_blueprint": true,
	"apply_map_blueprint": true,
	"ensure_standard_map_layers": true,
	"fill_rect": true,
	"paint_from_image_grid": true,
}

const MAP_TARGET_REQUIRED_TOOLS := {
	"edit_map": true,
	"paint_terrain_connect": true,
	"place_map_objects": true,
	"repair_placements": true,
	"repair_layer_coverage": true,
	"repair_map_region": true,
	"save_map_blueprint": true,
	"apply_map_blueprint": true,
	"fill_rect": true,
	"paint_from_image_grid": true,
}

const MAP_READ_TOOLS := {
	"describe_tilemap_selection": true,
	"describe_map_context": true,
	"plan_map_layout": true,
	"plan_map_algorithms": true,
	"plan_platform_level": true,
	"plan_reachable_map_growth": true,
	"compute_reachable_frontier": true,
	"sample_poisson_points": true,
	"compose_map_blueprint_grammar": true,
	"describe_map_region": true,
	"convert_map_coords": true,
	"find_placement_anchors": true,
	"validate_object_placements": true,
	"validate_layer_coverage": true,
	"query_spatial_index": true,
	"validate_map_region": true,
	"sample_noise_grid": true,
}

const MAP_REVISIONS_PATH := "res://.ai_agent_service/map_agent/revisions.json"

var editor_interface: EditorInterface
var undo_manager: Node
var file_state_cache: Node
var _map_revisions := {}
var _map_revisions_loaded := false


func _ready() -> void:
	if file_state_cache == null:
		file_state_cache = FileStateCache.new()
		add_child(file_state_cache)
	_ensure_map_revisions_loaded()


## 记录服务端 read_file/read_script 已成功读取的文件，让后续前端编辑共享同一份读取状态。
func remember_server_file_read(path: String) -> bool:
	var normalized_path := PathUtils.to_res_path(path)
	if normalized_path == "" or not PathUtils.is_read_allowed(normalized_path):
		return false
	var absolute := ProjectSettings.globalize_path(normalized_path)
	if not FileAccess.file_exists(absolute):
		return false
	if file_state_cache == null:
		file_state_cache = FileStateCache.new()
		add_child(file_state_cache)
	file_state_cache.snapshot(normalized_path, true)
	FrontendLogger.debug(editor_interface, "ToolExecutor", "Remembered server file read.", {
		"path": normalized_path,
	})
	return true


## 执行单个前端工具调用。部分工具（如 run_tests）内部使用 await 轮询子进程，
## 调用方必须 await 本函数，避免阻塞编辑器主线程。
func execute(tool_call: Dictionary) -> Dictionary:
	var name := str(tool_call.get("name", ""))
	var input: Dictionary = tool_call.get("input", {})
	var result = null
	var started_at := Time.get_ticks_msec()
	var map_revision_key := _map_revision_key(name, input)
	var is_map_write := MAP_WRITE_TOOLS.has(name)
	var requires_map_revision := MAP_REVISION_GUARDED_TOOLS.has(name)

	# ── 排查日志：记录原始 tool_call 结构 ──
	var _raw_id := tool_call.get("id", "")
	var _raw_frame_id := tool_call.get("frame_id", "")
	FrontendLogger.info(editor_interface, "ToolExecutor", "execute() entry — raw tool_call inspection.", {
		"tool": name,
		"call_keys": tool_call.keys(),
		"id_present": tool_call.has("id"),
		"id_value": str(_raw_id),
		"id_type": type_string(typeof(_raw_id)),
		"frame_id_present": tool_call.has("frame_id"),
		"frame_id_value": str(_raw_frame_id),
		"frame_id_type": type_string(typeof(_raw_frame_id)),
	})

	FrontendLogger.debug(editor_interface, "ToolExecutor", "Executing front tool.", {
		"tool": name,
		"id": str(tool_call.get("id", "")),
	})

	if is_map_write:
		_inject_map_write_metadata(input, tool_call)
		if MAP_TARGET_REQUIRED_TOOLS.has(name):
			var target_path := str(input.get("target_path", "")).strip_edges()
			if target_path == "":
				var target_error := {
					"ok": false,
					"error_code": "missing_target_path",
					"message": "%s requires a non-empty target_path" % name,
					"revision_key": "",
				}
				return AgentDTO.tool_result(
					str(tool_call.get("id", "")),
					str(tool_call.get("frame_id", "")),
					"error",
					target_error,
					"missing_target_path"
				)
		if requires_map_revision:
			var revision_error := _validate_map_write_revision(input, map_revision_key)
			if not revision_error.is_empty():
				return AgentDTO.tool_result(
					str(tool_call.get("id", "")),
					str(tool_call.get("frame_id", "")),
					"error",
					revision_error,
					str(revision_error.get("error_code", "map_revision_conflict"))
				)
		_begin_map_write_batch(name, input, tool_call, map_revision_key)

	match name:
		"read_class_docs", "read_class_info", "get_class_info":
			result = ClassDBReader.get_class_info(str(input.get("class_name", input.get("name", ""))))
		"read_file", "read_script":
			result = ProgramTools.read_file(input, file_state_cache)
		"write_file", "propose_script_edit", "propose_tests", "propose_content_file":
			result = ProgramTools.write_file(input, undo_manager, file_state_cache, editor_interface)
		"apply_text_edit":
			result = ProgramTools.apply_text_edit(input, undo_manager, file_state_cache, editor_interface)
		"read_debugger_errors":
			result = _read_debugger_errors(input)
		"read_profiler_snapshot":
			result = ProgramTools.read_profiler_snapshot(input)
		"run_tests":
			result = await ProgramTools.run_tests(input, editor_interface)
		"run_headless_self_test":
			var headless_input := input.duplicate(true)
			headless_input["kind"] = "headless_scene"
			result = await ProgramTools.run_tests(headless_input, editor_interface)
		"run_system_command":
			result = await ProgramTools.run_system_command(input, editor_interface)
		"execute_gd_script":
			result = await ProgramTools.execute_gd_script(input, editor_interface)
		"git_status":
			result = await ProgramTools.git_status(editor_interface)
		"git_diff":
			result = await ProgramTools.git_diff(input, editor_interface)
		"export_project":
			result = await ProgramTools.export_project(input, editor_interface)
		"read_scene_tree":
			if editor_interface == null:
				return AgentDTO.error_result(tool_call, "Godot editor interface is unavailable.", "editor_interface_unavailable")
			result = SceneTools.read_scene_tree(editor_interface)
			if result.is_empty():
				return AgentDTO.error_result(tool_call, "No edited scene is open in the Godot editor.", "no_edited_scene")
		"read_runtime_state":
			result = SceneTools.read_runtime_state(input, editor_interface)
		"validate_scene_state":
			result = SceneTools.validate_scene_state(input, editor_interface)
		"add_node":
			result = SceneTools.add_node(input, editor_interface, undo_manager)
		"set_node_property":
			result = SceneTools.set_node_property(input, editor_interface, undo_manager)
		"delete_node":
			result = SceneTools.delete_node(input, editor_interface, undo_manager)
		"reparent_node":
			result = SceneTools.reparent_node(input, editor_interface, undo_manager)
		"rename_node":
			result = SceneTools.rename_node(input, editor_interface, undo_manager)
		"instance_scene":
			result = SceneTools.instance_scene(input, editor_interface, undo_manager)
		"duplicate_node":
			result = SceneTools.duplicate_node(input, editor_interface, undo_manager)
		"connect_signal":
			result = SceneTools.connect_signal(input, editor_interface, undo_manager)
		"disconnect_signal":
			result = SceneTools.disconnect_signal(input, editor_interface, undo_manager)
		"add_to_group":
			result = SceneTools.add_to_group(input, editor_interface, undo_manager)
		"remove_from_group":
			result = SceneTools.remove_from_group(input, editor_interface, undo_manager)
		"list_node_groups":
			result = SceneTools.list_node_groups(input, editor_interface)
		"list_node_signals":
			result = SceneTools.list_node_signals(input, editor_interface)
		"list_node_methods":
			result = SceneTools.list_node_methods(input, editor_interface)
		"save_scene":
			result = SceneTools.save_scene(editor_interface)
		"list_open_scenes":
			result = SceneTools.list_open_scenes(editor_interface)
		"capture_viewport_screenshot":
			result = await SceneTools.capture_viewport_screenshot(input, editor_interface)
		"open_scene":
			result = await SceneTools.open_scene(input, editor_interface)
		"list_groups":
			result = SceneTools.list_groups(editor_interface)
		"get_current_scene_path":
			result = SceneTools.get_current_scene_path(editor_interface)
		"bake_navigation_mesh":
			result = SceneTools.bake_navigation_mesh(input, editor_interface, undo_manager)
		"set_project_setting":
			result = ProjectTools.set_project_setting(input, undo_manager)
		"read_project_setting":
			result = ProjectTools.read_project_setting(input)
		"list_autoloads":
			result = ProjectTools.list_autoloads()
		"add_autoload":
			result = ProjectTools.add_autoload(input, undo_manager)
		"remove_autoload":
			result = ProjectTools.remove_autoload(input, undo_manager)
		"list_input_actions":
			result = ProjectTools.list_input_actions()
		"add_input_action":
			result = ProjectTools.add_input_action(input, undo_manager)
		"remove_input_action":
			result = ProjectTools.remove_input_action(input, undo_manager)
		"list_export_presets":
			result = ProjectTools.list_export_presets()
		"describe_tilemap_selection":
			result = MapTools.describe_selection(editor_interface)
		"describe_map_context":
			result = MapTools.describe_map_context(input, editor_interface)
		"plan_map_layout":
			result = MapTools.plan_map_layout(input, editor_interface)
		"plan_map_algorithms":
			result = MapTools.plan_map_algorithms(input, editor_interface)
		"plan_platform_level":
			result = MapTools.plan_platform_level(input, editor_interface)
		"plan_reachable_map_growth":
			result = MapTools.plan_reachable_map_growth(input, editor_interface)
		"compute_reachable_frontier":
			result = MapTools.compute_reachable_frontier(input, editor_interface)
		"sample_poisson_points":
			result = MapTools.sample_poisson_points(input, editor_interface)
		"compose_map_blueprint_grammar":
			result = MapTools.compose_map_blueprint_grammar(input, editor_interface)
		"describe_map_region":
			result = _call_map_tool("describe_map_region", [input, editor_interface])
		"convert_map_coords":
			result = MapTools.convert_map_coords(input, editor_interface)
		"edit_map":
			result = MapTools.edit_map(input, editor_interface, undo_manager)
		"paint_terrain_connect":
			result = MapTools.paint_terrain_connect(input, editor_interface, undo_manager)
		"place_map_objects":
			result = MapTools.place_map_objects(input, editor_interface, undo_manager)
		"find_placement_anchors":
			result = MapTools.find_placement_anchors(input, editor_interface)
		"validate_object_placements":
			result = MapTools.validate_object_placements(input, editor_interface)
		"repair_placements":
			result = MapTools.repair_placements(input, editor_interface, undo_manager)
		"validate_layer_coverage":
			result = MapTools.validate_layer_coverage(input, editor_interface)
		"repair_layer_coverage":
			result = MapTools.repair_layer_coverage(input, editor_interface, undo_manager)
		"query_spatial_index":
			result = MapTools.query_spatial_index(input, editor_interface)
		"compact_spatial_index":
			result = MapTools.compact_spatial_index(input, editor_interface, undo_manager)
		"validate_map_region":
			result = MapTools.validate_map_region(input, editor_interface)
		"repair_map_region":
			result = MapTools.repair_map_region(input, editor_interface, undo_manager)
		"sample_noise_grid":
			result = MapTools.sample_noise_grid(input, editor_interface)
		"write_resource_registry":
			result = MapTools.write_resource_registry(input, editor_interface, undo_manager)
		"save_map_blueprint":
			result = MapTools.save_map_blueprint(input, editor_interface, undo_manager)
		"apply_map_blueprint":
			result = MapTools.apply_map_blueprint(input, editor_interface, undo_manager)
		"ensure_standard_map_layers":
			result = MapTools.ensure_standard_map_layers(input, editor_interface, undo_manager)
		"fill_rect":
			result = MapTools.fill_rect(input, editor_interface, undo_manager)
		"paint_from_image_grid":
			result = MapTools.paint_from_image_grid(input, editor_interface, undo_manager)
		"create_resource":
			result = ResourceTools.create_resource(input, undo_manager)
		"read_image_metadata":
			result = ResourceTools.read_image_metadata(input)
		"create_sprite_frames_from_sheet":
			result = ResourceTools.create_sprite_frames_from_sheet(input, undo_manager)
		"read_resource":
			result = ResourceTools.read_resource(input)
		"set_resource_property":
			result = ResourceTools.set_resource_property(input, undo_manager)
		"create_animation_track":
			result = ResourceTools.create_animation_track(input, editor_interface, undo_manager)
		"create_shader_material":
			result = ResourceTools.create_shader_material(input, undo_manager)
		_:
			FrontendLogger.warn(editor_interface, "ToolExecutor", "Unknown front tool requested.", {"tool": name})
			return AgentDTO.error_result(tool_call, "Unknown front tool: " + name, "unknown_front_tool")

	if not (result is Dictionary):
		FrontendLogger.warn(editor_interface, "ToolExecutor", "Front tool returned invalid payload.", {
			"tool": name,
			"result_type": typeof(result),
		})
		result = {
			"ok": false,
			"message": "Front tool returned an invalid non-dictionary result.",
			"error_code": "invalid_front_tool_result",
			"result_type": typeof(result),
		}

	if requires_map_revision:
		result = _finish_map_write_batch(name, input, result, map_revision_key)
	elif is_map_write:
		result = _finish_aux_write_batch(name, input, result)
	elif MAP_READ_TOOLS.has(name):
		_attach_map_revision(result, map_revision_key)

	var elapsed_ms := Time.get_ticks_msec() - started_at
	# ── 排查日志：记录即将传给 AgentDTO 的元数据 ──
	var _dto_tool_use_id := str(tool_call.get("id", ""))
	var _dto_frame_id := str(tool_call.get("frame_id", ""))
	FrontendLogger.info(editor_interface, "ToolExecutor", "execute() exit — DTO metadata check.", {
		"tool": name,
		"ok": result.get("ok", true),
		"dto_tool_use_id": _dto_tool_use_id,
		"dto_tool_use_id_empty": _dto_tool_use_id.strip_edges() == "",
		"dto_frame_id": _dto_frame_id,
		"dto_frame_id_empty": _dto_frame_id.strip_edges() == "",
		"result_keys": result.keys() if result is Dictionary else "NOT_A_DICT",
		"elapsed_ms": elapsed_ms,
	})
	if bool(result.get("ok", true)):
		FrontendLogger.info(editor_interface, "ToolExecutor", "Front tool applied.", {
			"tool": name,
			"elapsed_ms": elapsed_ms,
		})
		return AgentDTO.tool_result(
			_dto_tool_use_id,
			_dto_frame_id,
			"applied",
			result,
			"",
			_result_artifacts(result)
		)
	# 把工具函数返回的完整 result 字典原样带回去（而不是只取 message 拼一个新字典），
	# 这样像 write_file 的 file_stale 场景里附带的 current_content/path 等字段才能
	# 传到 LLM 那一侧，不用再让它额外猜一次该不该重新 read_file。
	FrontendLogger.warn(editor_interface, "ToolExecutor", "Front tool failed.", {
		"tool": name,
		"error_code": str(result.get("error_code", "front_tool_failed")),
		"message": str(result.get("message", result.get("error", ""))),
		"elapsed_ms": elapsed_ms,
	})
	return AgentDTO.tool_result(
		str(tool_call.get("id", "")),
		str(tool_call.get("frame_id", "")),
		"error",
		result,
		str(result.get("error_code", "front_tool_failed"))
	)


func _map_revision_key(tool_name: String, input: Dictionary) -> String:
	var target_path := str(input.get("target_path", "")).strip_edges()
	if target_path != "":
		return target_path
	var parent_path := str(input.get("parent_path", "")).strip_edges()
	if parent_path != "":
		return parent_path
	match tool_name:
		"write_resource_registry":
			return "res://.ai_agent_service/map_agent/resource_registry.json"
		"save_map_blueprint", "apply_map_blueprint":
			return "res://.ai_agent_service/map_agent/blueprints"
		"compact_spatial_index":
			return "res://.ai_agent_service/map_agent/spatial_index.json"
		_:
			return "__selected_map__"


func _current_map_revision(key: String) -> int:
	_ensure_map_revisions_loaded()
	return int(_map_revisions.get(key, 0))


func _ensure_map_revisions_loaded() -> void:
	if _map_revisions_loaded:
		return
	_map_revisions_loaded = true
	_map_revisions.clear()
	var absolute := ProjectSettings.globalize_path(MAP_REVISIONS_PATH)
	if not FileAccess.file_exists(absolute):
		return
	var text := FileAccess.get_file_as_string(absolute)
	var parsed = JSON.parse_string(text)
	if not (parsed is Dictionary):
		return
	for key in parsed.keys():
		var value = parsed.get(key, 0)
		if value is int or value is float:
			_map_revisions[str(key)] = int(value)


func _save_map_revisions() -> void:
	_ensure_map_revisions_loaded()
	var absolute := ProjectSettings.globalize_path(MAP_REVISIONS_PATH)
	var base_dir := absolute.get_base_dir()
	DirAccess.make_dir_recursive_absolute(base_dir)
	var file := FileAccess.open(absolute, FileAccess.WRITE)
	if file == null:
		FrontendLogger.warn(editor_interface, "ToolExecutor", "Failed to persist map revisions.", {
			"path": MAP_REVISIONS_PATH,
			"error": FileAccess.get_open_error(),
		})
		return
	file.store_string(JSON.stringify(_map_revisions, "\t"))


func _validate_map_write_revision(input: Dictionary, key: String) -> Dictionary:
	if not input.has("expected_revision"):
		return {
			"ok": false,
			"error_code": "expected_revision_required",
			"message": "map write requires expected_revision",
			"actual_revision": _current_map_revision(key),
			"revision_key": key,
		}
	var expected_revision = input.get("expected_revision")
	if expected_revision is float and float(int(expected_revision)) == expected_revision:
		expected_revision = int(expected_revision)
		input["expected_revision"] = expected_revision
	if not (expected_revision is int):
		return {
			"ok": false,
			"error_code": "expected_revision_required",
			"message": "expected_revision must be an integer",
			"actual_revision": _current_map_revision(key),
			"revision_key": key,
		}
	var actual_revision := _current_map_revision(key)
	if int(expected_revision) != actual_revision:
		return {
			"ok": false,
			"error_code": "map_revision_conflict",
			"target_path": key,
			"revision_key": key,
			"expected_revision": int(expected_revision),
			"actual_revision": actual_revision,
			"next_expected_revision": actual_revision,
			"message": "map changed since this plan was made; re-read the affected region before writing",
			"hint": "Call describe_map_region on the affected region, then retry with expected_revision=actual_revision.",
		}
	return {}


func _inject_map_write_metadata(input: Dictionary, tool_call: Dictionary) -> void:
	if not input.has("frame_id"):
		input["frame_id"] = str(tool_call.get("frame_id", ""))
	if not input.has("worker"):
		input["worker"] = str(tool_call.get("agent", "map-agent"))
	if not input.has("mode"):
		input["mode"] = "write_one_batch"
	if not input.has("task_summary"):
		input["task_summary"] = str(input.get("summary", input.get("objective", ""))).strip_edges()


func _begin_map_write_batch(tool_name: String, input: Dictionary, tool_call: Dictionary, key: String) -> void:
	if undo_manager == null:
		return
	if undo_manager.has_method("has_active_batch") and bool(undo_manager.has_active_batch()):
		undo_manager.commit_batch()
	var description := _map_write_undo_description(tool_name, input, tool_call, key)
	undo_manager.begin_batch(description)


func _finish_map_write_batch(tool_name: String, input: Dictionary, result: Dictionary, key: String) -> Dictionary:
	var ok := bool(result.get("ok", true))
	if ok:
		var previous_revision := _current_map_revision(key)
		var next_revision := previous_revision + 1
		_map_revisions[key] = next_revision
		_save_map_revisions()
		result["expected_revision"] = int(input.get("expected_revision", previous_revision))
		result["previous_map_revision"] = previous_revision
		result["map_revision"] = next_revision
		result["write_batch_id"] = str(input.get("write_batch_id", ""))
		result["plan_version"] = int(input.get("plan_version", 0))
		result["batch_index"] = int(input.get("batch_index", 0))
		result["worker"] = str(input.get("worker", ""))
		result["mode"] = str(input.get("mode", ""))
		if input.has("workflow_operations"):
			result["workflow_operations"] = input.get("workflow_operations", [])
		if input.has("workflow_constraints"):
			result["workflow_constraints"] = input.get("workflow_constraints", [])
		result["frame_id"] = str(input.get("frame_id", ""))
		result["delegate_group_id"] = str(input.get("delegate_group_id", ""))
		if undo_manager != null:
			undo_manager.commit_batch()
	else:
		result["map_revision"] = _current_map_revision(key)
		if undo_manager != null:
			undo_manager.abort_batch()
	return result


func _finish_aux_write_batch(_tool_name: String, input: Dictionary, result: Dictionary) -> Dictionary:
	if bool(result.get("ok", true)):
		result["write_batch_id"] = str(input.get("write_batch_id", ""))
		result["worker"] = str(input.get("worker", ""))
		result["mode"] = str(input.get("mode", ""))
		result["frame_id"] = str(input.get("frame_id", ""))
		if undo_manager != null:
			undo_manager.commit_batch()
	else:
		if undo_manager != null:
			undo_manager.abort_batch()
	return result


func _attach_map_revision(result: Dictionary, key: String) -> void:
	if not bool(result.get("ok", true)):
		return
	result["map_revision"] = _current_map_revision(key)


func _map_write_undo_description(tool_name: String, input: Dictionary, tool_call: Dictionary, key: String) -> String:
	var worker := str(input.get("worker", tool_call.get("agent", "map-agent"))).strip_edges()
	var frame_id := str(input.get("frame_id", tool_call.get("frame_id", ""))).strip_edges()
	var batch_id := str(input.get("write_batch_id", "")).strip_edges()
	var mode := str(input.get("mode", "write_one_batch")).strip_edges()
	var summary := str(input.get("task_summary", tool_name)).strip_edges()
	if summary == "":
		summary = tool_name + " " + key
	return "AI map edit [worker=%s frame=%s batch=%s mode=%s]: %s" % [worker, frame_id, batch_id, mode, summary.left(80)]


func _result_artifacts(result: Dictionary) -> Array:
	var artifacts: Array = []
	if result.has("path") and result["path"] is String:
		artifacts.append(result["path"])
	return artifacts


func _call_map_tool(method_name: String, args: Array) -> Dictionary:
	var map_tools_instance := MapTools.new()
	if not map_tools_instance.has_method(method_name):
		return {
			"ok": false,
			"message": "MapTools is missing method: " + method_name + ". Restart the Godot editor or reinstall the AI Agent addon so the latest scripts are loaded.",
			"error_code": "map_tool_method_missing",
			"method": method_name,
		}
	var value = map_tools_instance.callv(method_name, args)
	if value is Dictionary:
		return value
	return {
		"ok": false,
		"message": "MapTools method returned a non-dictionary result: " + method_name,
		"error_code": "invalid_map_tool_result",
		"method": method_name,
	}


func _read_debugger_errors(input: Dictionary) -> Dictionary:
	var max_items := int(input.get("max_items", 20))
	var items: Array = DiagnosticsCollector.collect(editor_interface)
	if max_items > 0 and items.size() > max_items:
		items = items.slice(0, max_items)
	return {"ok": true, "items": items}
