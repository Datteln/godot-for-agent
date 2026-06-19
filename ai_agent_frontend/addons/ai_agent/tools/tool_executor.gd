@tool
extends Node

const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const ClassDBReader = preload("res://addons/ai_agent/context/classdb_reader.gd")
const FileStateCache = preload("res://addons/ai_agent/context/file_state_cache.gd")
const DiagnosticsCollector = preload("res://addons/ai_agent/context/diagnostics_collector.gd")
const ProgramTools = preload("res://addons/ai_agent/tools/program_tools.gd")
const SceneTools = preload("res://addons/ai_agent/tools/scene_tools.gd")
const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")
const ResourceTools = preload("res://addons/ai_agent/tools/resource_tools.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

var editor_interface: EditorInterface
var undo_manager: Node
var file_state_cache: Node


func _ready() -> void:
	if file_state_cache == null:
		file_state_cache = FileStateCache.new()
		add_child(file_state_cache)


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
		"write_file", "propose_script_edit", "propose_tests", "propose_content_file", "apply_text_edit":
			result = ProgramTools.write_file(input, undo_manager, file_state_cache, editor_interface)
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
		"describe_tilemap_selection":
			result = MapTools.describe_selection(editor_interface)
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
