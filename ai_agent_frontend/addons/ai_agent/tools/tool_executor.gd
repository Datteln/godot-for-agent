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

var editor_interface: EditorInterface
var undo_manager: Node
var file_state_cache: Node


func _ready() -> void:
	if file_state_cache == null:
		file_state_cache = FileStateCache.new()
		add_child(file_state_cache)


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
	var result: Dictionary
	var started_at := Time.get_ticks_msec()

	FrontendLogger.debug(editor_interface, "ToolExecutor", "Executing front tool.", {
		"tool": name,
		"id": str(tool_call.get("id", "")),
	})

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
			result = SceneTools.read_scene_tree(editor_interface)
		"read_runtime_state":
			result = SceneTools.read_runtime_state(input, editor_interface)
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
			result = SceneTools.open_scene(input, editor_interface)
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
		"edit_map":
			result = MapTools.edit_map(input, editor_interface, undo_manager)
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

	var elapsed_ms := Time.get_ticks_msec() - started_at
	if bool(result.get("ok", true)):
		FrontendLogger.info(editor_interface, "ToolExecutor", "Front tool applied.", {
			"tool": name,
			"elapsed_ms": elapsed_ms,
		})
		return AgentDTO.tool_result(
			str(tool_call.get("id", "")),
			str(tool_call.get("frame_id", "")),
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


func _result_artifacts(result: Dictionary) -> Array:
	var artifacts: Array = []
	if result.has("path"):
		artifacts.append(result["path"])
	return artifacts


func _read_debugger_errors(input: Dictionary) -> Dictionary:
	var max_items := int(input.get("max_items", 20))
	var items: Array = DiagnosticsCollector.collect(editor_interface)
	if max_items > 0 and items.size() > max_items:
		items = items.slice(0, max_items)
	return {"ok": true, "items": items}
