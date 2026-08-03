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
# 把 revision 计数的持久化与事务协调委托给独立的 MapRevisionTracker 节点，
# 不再由 ToolExecutor 自己读写 revisions.json。
const MapRevisionTracker = preload("res://addons/ai_agent/tools/map_revision_tracker.gd")
const MapTransactionPolicy = preload("res://addons/ai_agent/undo/map_transaction_policy.gd")
const ResourceTools = preload("res://addons/ai_agent/tools/resource_tools.gd")
const ProjectTools = preload("res://addons/ai_agent/tools/project_tools.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

## 地图内容写入工具：直接修改瓦片/对象等地图内容，需要 revision 守卫。
const MAP_CONTENT_WRITE_TOOLS := {
	"edit_map": true,
	"paint_terrain_connect": true,
	"place_map_objects": true,
	"repair_placements": true,
	"repair_layer_coverage": true,
	"repair_map_region": true,
	"apply_map_blueprint": true,
}

## 地图辅助写入工具：不直接修改瓦片/对象内容，但仍会改变地图相关状态。
## 与 MAP_CONTENT_WRITE_TOOLS 分开是因为这些工具不参与 revision 守卫。
const MAP_AUX_WRITE_TOOLS := {
	"compact_spatial_index": true,
	"write_resource_registry": true,
	"save_map_blueprint": true,
	"ensure_standard_map_layers": true,
}

## 需要 revision 守卫的工具集合——当前等同于内容写入工具，
## 辅助写入（索引压缩、蓝图保存等）不会改变地图内容，无需递增 revision。
const MAP_REVISION_GUARDED_TOOLS := MAP_CONTENT_WRITE_TOOLS

const MAP_TARGET_REQUIRED_TOOLS := {
	"edit_map": true,
	"paint_terrain_connect": true,
	"place_map_objects": true,
	"repair_placements": true,
	"repair_layer_coverage": true,
	"repair_map_region": true,
	"save_map_blueprint": true,
	"apply_map_blueprint": true,
}

const MAP_READ_TOOLS := {
	"describe_tilemap_selection": true,
	"describe_map_context": true,
	"plan_map_layout": true,
	"plan_map_algorithms": true,
	"validate_platform_level_plan": true,
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

const PLANNING_CONTRACT_TOOLS := {
	"compute_reachable_frontier": true,
	"plan_reachable_map_growth": true,
	"validate_platform_level_plan": true,
	"validate_map_region": true,
}

const MAP_TRANSACTION_VALIDATORS := {
	"validate_map_region": true,
	"validate_object_placements": true,
	"validate_layer_coverage": true,
	"validate_platform_level_plan": true,
}

var editor_interface: EditorInterface
var undo_manager: Node
var file_state_cache: Node
## 管理地图 revision 计数的独立节点，负责持久化和与 Undo 事务协调。
var map_revision_tracker: Node


func _ready() -> void:
	if file_state_cache == null:
		file_state_cache = FileStateCache.new()
		add_child(file_state_cache)
	if map_revision_tracker == null:
		map_revision_tracker = MapRevisionTracker.new()
		add_child(map_revision_tracker)
	## 初始化 revision tracker 节点，并传入 editor_interface 和 undo_redo，
	## 使 revision 变更能与编辑器 Undo 系统同步。
	var editor_undo_redo = undo_manager.undo_redo if undo_manager != null else null
	map_revision_tracker.configure(editor_interface, editor_undo_redo)


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


func reset_session_state() -> void:
	## 仅清理会话级读前写授权；Godot 权威 revision tracker 属于工程状态，
	## reset 对话时必须保留。
	if file_state_cache != null and file_state_cache.has_method("clear"):
		file_state_cache.clear()


## 执行单个前端工具调用。部分工具（如 run_tests）内部使用 await 轮询子进程，
## 调用方必须 await 本函数，避免阻塞编辑器主线程。
func execute(tool_call: Dictionary) -> Dictionary:
	var name := str(tool_call.get("name", ""))
	var input: Dictionary = tool_call.get("input", {})
	var result = null
	var started_at := Time.get_ticks_msec()
	var map_revision_key := _map_revision_key(name, input)
	## 内容写入和辅助写入都视为地图写入（需要 undo batch），但只有内容写入需要 revision 守卫。
	var is_map_write := MAP_CONTENT_WRITE_TOOLS.has(name) or MAP_AUX_WRITE_TOOLS.has(name)
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
		if undo_manager != null and undo_manager.has_method("ensure_map_recovery_ready"):
			var recovery: Dictionary = undo_manager.ensure_map_recovery_ready()
			if not bool(recovery.get("ok", false)):
				return AgentDTO.tool_result(
					str(tool_call.get("id", "")),
					str(tool_call.get("frame_id", "")),
					"error",
					recovery,
					str(recovery.get("error_code", "map_transaction_recovery_required"))
				)
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
		## ── 内容写入前置校验：先确保 Undo 管理器可用，再解析 canonical 目标节点身份 ──
		if requires_map_revision:
			## revision 守卫依赖 Undo 管理器来回滚冲突写入。
			if undo_manager == null:
				return AgentDTO.error_result(
					tool_call,
					"Map content writes require the unified Undo manager.",
					"map_undo_manager_unavailable"
				)
			## 解析 canonical 节点（TileMap/TileMapLayer），获取真实路径和节点类型，
			## 用于后续生成精确的 revision key（TileMap 按 map_layer 区分）。
			var target_result := MapTools.resolve_map_target_identity(input, editor_interface, true)
			if not bool(target_result.get("ok", false)):
				return AgentDTO.tool_result(
					str(tool_call.get("id", "")),
					str(tool_call.get("frame_id", "")),
					"error",
					target_result,
					str(target_result.get("error_code", "map_target_invalid"))
				)
			## 用解析后的 canonical 路径覆盖 target_path，确保 revision key 一致。
			input["target_path"] = str(target_result.get("path", ""))
			var canonical_node: Node = target_result.get("node")
			if canonical_node != null:
				## 记录 canonical 节点类型（如 TileMapLayer），供 _map_revision_key 区分同一 TileMap 下不同图层。
				input["_canonical_map_type"] = canonical_node.get_class()
			## canonical 信息可能改变 revision key，需重新计算。
			map_revision_key = _map_revision_key(name, input)
		if requires_map_revision:
			if (
				map_revision_tracker != null
				and map_revision_tracker.has_method("synchronize_mutation_boundary")
			):
				var sync_result: Dictionary = map_revision_tracker.synchronize_mutation_boundary()
				if not bool(sync_result.get("ok", false)):
					return AgentDTO.tool_result(
						str(tool_call.get("id", "")),
						str(tool_call.get("frame_id", "")),
						"error",
						sync_result,
						str(sync_result.get("error_code", "map_revision_sync_failed"))
					)
			var revision_error := _validate_map_write_revision(input, map_revision_key)
			if not revision_error.is_empty():
				return AgentDTO.tool_result(
					str(tool_call.get("id", "")),
					str(tool_call.get("frame_id", "")),
					"error",
					revision_error,
					str(revision_error.get("error_code", "map_revision_conflict"))
				)
		var begin_error := _begin_map_write_batch(
			name,
			input,
			tool_call,
			map_revision_key
		)
		if not begin_error.is_empty():
			return AgentDTO.tool_result(
				str(tool_call.get("id", "")),
				str(tool_call.get("frame_id", "")),
				"error",
				begin_error,
				str(begin_error.get("error_code", "map_transaction_start_failed"))
			)
	## planning_contract revision 校验移到 _begin_map_write_batch 之后，
	## 确保 revision 不匹配时 undo batch 已开启但会由上层直接 return 走 abort 路径。
	if PLANNING_CONTRACT_TOOLS.has(name) and input.has("planning_contract"):
		var contract_revision_error := _validate_planning_contract_revision(input, map_revision_key)
		if not contract_revision_error.is_empty():
			_abort_started_map_write(input, "planning_contract_revision_conflict")
			return AgentDTO.tool_result(
				str(tool_call.get("id", "")),
				str(tool_call.get("frame_id", "")),
				"error",
				contract_revision_error,
				str(contract_revision_error.get("error_code", "planning_contract_revision_conflict"))
			)

	if MAP_READ_TOOLS.has(name):
		## 只由执行器注入；MapTools 会用它绑定 canonical collision facts，
		## public tool input 中的原始碰撞格不能成为验证权威。
		input["_canonical_map_revision"] = _current_map_revision(map_revision_key)

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
		"validate_platform_level_plan":
			result = MapTools.validate_platform_level_plan(input, editor_interface)
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
		if MAP_TRANSACTION_VALIDATORS.has(name):
			result = _finish_map_transaction_validation(name, input, result)

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
	var target_path := ""
	if input.get("planning_contract") is Dictionary:
		var contract: Dictionary = input.get("planning_contract", {})
		if contract.get("target") is Dictionary:
			var contract_target: Dictionary = contract.get("target")
			target_path = str(contract_target.get("path", "")).strip_edges()
			## 从 planning_contract.target 中提取 canonical 类型和 map_layer，
			## 让 revision key 能区分同一 TileMap 的不同图层。
			if not input.has("_canonical_map_type"):
				input["_canonical_map_type"] = str(contract_target.get("type", ""))
			if not input.has("map_layer") and contract_target.has("map_layer"):
				input["map_layer"] = contract_target.get("map_layer")
	if target_path == "":
		target_path = str(input.get("target_path", "")).strip_edges()
	if target_path != "":
		## 对 TileMap 类型的地图，revision key 按 map_layer 区分，
		## 避免不同图层共享同一个 revision 计数导致误判冲突。
		var map_type := str(input.get("_canonical_map_type", "")).strip_edges()
		var key := target_path
		if map_type == "TileMap" and input.has("map_layer"):
			key += "::map_layer=%d" % int(input.get("map_layer", 0))
		return key
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


func _validate_planning_contract_revision(input: Dictionary, key: String) -> Dictionary:
	var contract_value = input.get("planning_contract")
	if not (contract_value is Dictionary):
		return {
			"ok": false,
			"error_code": "invalid_planning_contract",
			"message": "planning_contract must be an object returned by a planning/read tool.",
		}
	var contract: Dictionary = contract_value
	if not contract.has("map_revision") or not (contract.get("map_revision") is int):
		return {
			"ok": false,
			"error_code": "planning_contract_revision_required",
			"message": "planning_contract is missing its integer map_revision; recompute the frontier/plan.",
			"actual_revision": _current_map_revision(key),
			"revision_key": key,
		}
	var expected := int(contract.get("map_revision"))
	var actual := _current_map_revision(key)
	if expected == actual:
		return {}
	return {
		"ok": false,
		"error_code": "planning_contract_revision_conflict",
		"message": "The map changed after this planning contract was created; recompute the frontier/plan.",
		"expected_revision": expected,
		"actual_revision": actual,
		"revision_key": key,
	}


## 委托给 map_revision_tracker 查询当前 revision，不再自行管理 revisions.json。
func _current_map_revision(key: String) -> int:
	if map_revision_tracker == null:
		return 0
	return int(map_revision_tracker.current_revision(key))


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
	if input.has("approval_id"):
		for field_name in [
			"approval_batch_fingerprint",
			"approval_snapshot_id",
			"approval_snapshot_digest",
			"approval_target_path",
			"approval_map_layer",
			"approval_expected_revision",
		]:
			if not input.has(field_name) or str(input.get(field_name, "")).strip_edges() == "":
				return {
					"ok": false,
					"error_code": "approval_snapshot_contract_incomplete",
					"message": "Approved platform writes require snapshot-bound approval metadata.",
					"missing_field": field_name,
				}
		if str(input.get("approval_target_path", "")) != str(input.get("target_path", "")) \
				or int(input.get("approval_map_layer", -1)) != int(input.get("map_layer", 0)) \
				or int(input.get("approval_expected_revision", -1)) != actual_revision:
			return {
				"ok": false,
				"error_code": "approval_snapshot_scope_mismatch",
				"message": "Approved batch target/layer/revision no longer matches the write request.",
				"actual_revision": actual_revision,
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


func _begin_map_write_batch(
	tool_name: String,
	input: Dictionary,
	tool_call: Dictionary,
	key: String
) -> Dictionary:
	## 通知 revision tracker 开始受控写入，阻止并发的 revision 变更。
	if map_revision_tracker != null:
		map_revision_tracker.begin_controlled_write()
	if undo_manager == null:
		return {}
	var description := _map_write_undo_description(tool_name, input, tool_call, key)
	if MapTransactionPolicy.mode(input) == MapTransactionPolicy.MODE_APPROVED_WRITE_GROUP:
		var prepared: Dictionary = undo_manager.prepare_map_write_group(
			str(input.get("map_transaction_id", "")),
			description,
			key,
			int(input.get("map_transaction_base_revision", input.get("expected_revision", 0))),
			str(input.get("approval_id", "")),
			str(input.get("approval_batch_fingerprint", "")),
			int(input.get("approval_expected_revision", -1))
		)
		if not bool(prepared.get("ok", false)):
			if map_revision_tracker != null:
				map_revision_tracker.end_controlled_write()
			return prepared
		return {}
	if undo_manager.has_method("has_active_batch") and bool(undo_manager.has_active_batch()):
		undo_manager.commit_batch()
	undo_manager.begin_batch(description)
	return {}


func _finish_map_write_batch(tool_name: String, input: Dictionary, result: Dictionary, key: String) -> Dictionary:
	var ok := bool(result.get("ok", true))
	var transaction_id := str(input.get("map_transaction_id", "")).strip_edges()
	var grouped := (
		MapTransactionPolicy.mode(input)
		== MapTransactionPolicy.MODE_APPROVED_WRITE_GROUP
	)
	if grouped and undo_manager != null and undo_manager.has_method("map_transaction_error"):
		var transaction_error: Dictionary = undo_manager.map_transaction_error()
		if not transaction_error.is_empty():
			result = transaction_error
			ok = false
	## changed=false 表示写入成功但未实际修改内容（如空操作），此时不递增 revision。
	var changed := bool(result.get("changed", true))
	if ok and changed:
		## 在同一个事务内递增 revision 并持久化，与 Undo 操作绑定，保证撤销时 revision 也回退。
		var advance: Dictionary = map_revision_tracker.advance_controlled_write(key, undo_manager)
		var previous_revision := int(advance.get("previous_revision", 0))
		var next_revision := int(advance.get("next_revision", previous_revision))
		var revision_error := int(advance.get("error", FAILED))
		## 持久化失败时回滚：撤销 Undo batch 并结束受控写入，返回错误让 agent 重试。
		if revision_error != OK:
			result = {
				"ok": false,
				"error_code": "map_revision_persist_failed",
				"message": "Map content was reverted because its revision could not be persisted.",
				"target_path": str(input.get("target_path", "")),
				"map_revision": previous_revision,
				"revision_key": key,
				"error": revision_error,
			}
			if undo_manager != null:
				undo_manager.abort_batch()
			map_revision_tracker.end_controlled_write()
			return result
		result["expected_revision"] = int(input.get("expected_revision", previous_revision))
		result["previous_map_revision"] = previous_revision
		result["map_revision"] = next_revision
		result["revision_key"] = key
		result["write_batch_id"] = str(input.get("write_batch_id", ""))
		if input.has("approval_id"):
			result["approval_id"] = str(input.get("approval_id", ""))
			result["approval_batch_fingerprint"] = str(
				input.get("approval_batch_fingerprint", "")
			)
			result["approval_expected_revision"] = int(
				input.get("approval_expected_revision", previous_revision)
			)
			result["approval_snapshot_id"] = str(input.get("approval_snapshot_id", ""))
			result["approval_snapshot_digest"] = str(
				input.get("approval_snapshot_digest", "")
			)
			result["approval_target_path"] = str(input.get("approval_target_path", ""))
			result["approval_map_layer"] = int(input.get("approval_map_layer", 0))
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
			if grouped:
				undo_manager.update_map_transaction_revision(
					transaction_id,
					next_revision
				)
				result["map_transaction_id"] = transaction_id
				result["map_transaction_status"] = "prepared"
			else:
				undo_manager.commit_batch()
	## 写入失败：保持当前 revision 不变，abort undo batch 撤销已记录的 undo 操作。
	elif not ok:
		result["map_revision"] = _current_map_revision(key)
		if undo_manager != null:
			if grouped:
				var aborted: Dictionary = undo_manager.abort_map_write_group(
					transaction_id,
					str(result.get("error_code", "map_write_failed"))
				)
				result["map_transaction_id"] = transaction_id
				result["map_transaction_status"] = str(
					aborted.get("map_transaction_status", "rolled_back")
				)
			else:
				undo_manager.abort_batch()
	## 写入成功但 changed=false（无实际变更）：abort undo batch，不递增 revision。
	else:
		result["map_revision"] = _current_map_revision(key)
		result["revision_key"] = key
		if undo_manager != null:
			undo_manager.abort_batch()
	## 无论成功或失败，都要结束受控写入状态。
	if map_revision_tracker != null:
		map_revision_tracker.end_controlled_write()
	return result


func _finish_aux_write_batch(_tool_name: String, input: Dictionary, result: Dictionary) -> Dictionary:
	var transaction_id := str(input.get("map_transaction_id", "")).strip_edges()
	var grouped := (
		MapTransactionPolicy.mode(input)
		== MapTransactionPolicy.MODE_APPROVED_WRITE_GROUP
	)
	if bool(result.get("ok", true)):
		result["write_batch_id"] = str(input.get("write_batch_id", ""))
		result["worker"] = str(input.get("worker", ""))
		result["mode"] = str(input.get("mode", ""))
		result["frame_id"] = str(input.get("frame_id", ""))
		if undo_manager != null:
			if grouped:
				result["map_transaction_id"] = transaction_id
				result["map_transaction_status"] = "prepared"
			else:
				undo_manager.commit_batch()
	else:
		if undo_manager != null:
			if grouped:
				undo_manager.abort_map_write_group(
					transaction_id,
					str(result.get("error_code", "map_aux_write_failed"))
				)
				result["map_transaction_id"] = transaction_id
				result["map_transaction_status"] = "rolled_back"
			else:
				undo_manager.abort_batch()
	return result


func _abort_started_map_write(input: Dictionary, reason: String) -> void:
	var transaction_id := str(input.get("map_transaction_id", "")).strip_edges()
	if undo_manager != null:
		if (
			MapTransactionPolicy.mode(input)
			== MapTransactionPolicy.MODE_APPROVED_WRITE_GROUP
		):
			undo_manager.abort_map_write_group(transaction_id, reason)
		else:
			undo_manager.abort_batch()
	if map_revision_tracker != null:
		map_revision_tracker.end_controlled_write()


func _finish_map_transaction_validation(
	tool_name: String,
	input: Dictionary,
	result: Dictionary
) -> Dictionary:
	var transaction_id := str(input.get("map_transaction_id", "")).strip_edges()
	if transaction_id == "":
		return result
	result["map_transaction_id"] = transaction_id
	result["validation_tool"] = tool_name
	if undo_manager == null:
		result["ok"] = false
		result["error_code"] = "map_undo_manager_unavailable"
		result["message"] = "Cannot resolve an approved map write group without the Undo manager."
		return result
	var target := str(
		result.get(
			"target_path",
			result.get(
				"target",
				input.get("map_transaction_target", input.get("target_path", ""))
			)
		)
	).strip_edges()
	var revision_value = result.get("map_revision")
	if not (revision_value is int):
		var invalid_revision: Dictionary = undo_manager.abort_map_write_group(
			transaction_id,
			"validation_revision_missing"
		)
		result["ok"] = false
		result["error_code"] = "map_transaction_validation_revision_missing"
		result["message"] = "Validation did not return an integer map_revision."
		result["map_transaction_status"] = str(
			invalid_revision.get("map_transaction_status", "rolled_back")
		)
		return result
	var passed: bool = _validation_result_passed(result)
	if not passed:
		var aborted: Dictionary = undo_manager.abort_map_write_group(
			transaction_id,
			"validation_failed"
		)
		result["map_transaction_status"] = str(
			aborted.get("map_transaction_status", "rolled_back")
		)
		result["transaction_rollback_reason"] = "validation_failed"
		return result
	var committed: Dictionary = undo_manager.commit_map_write_group(
		transaction_id,
		target,
		int(revision_value)
	)
	if not bool(committed.get("ok", false)):
		var validation_copy: Dictionary = result.duplicate(true)
		result = committed
		result["validation"] = validation_copy
		result["validation_tool"] = tool_name
		result["map_transaction_id"] = transaction_id
		return result
	result["map_transaction_status"] = "committed"
	result["committed_revision"] = int(
		committed.get("committed_revision", revision_value)
	)
	result["approval_records"] = committed.get("approval_records", [])
	return result


func _validation_result_passed(result: Dictionary) -> bool:
	if result.get("passed") is bool:
		return bool(result.get("passed"))
	var validation = result.get("validation")
	if validation is Dictionary and validation.get("passed") is bool:
		return bool(validation.get("passed"))
	return false


func _attach_map_revision(result: Dictionary, key: String) -> void:
	if not bool(result.get("ok", true)):
		return
	var resolved_key := str(result.get("target_path", "")).strip_edges()
	if resolved_key == "":
		resolved_key = str(result.get("target", "")).strip_edges()
	if resolved_key == "":
		resolved_key = key
	var identity_input := {
		"target_path": resolved_key,
		"_canonical_map_type": str(result.get("type", "")),
	}
	if result.has("map_layer") and result.get("map_layer") != null:
		identity_input["map_layer"] = result.get("map_layer")
	var canonical_key := _map_revision_key("", identity_input)
	if (
		str(identity_input.get("_canonical_map_type", "")).strip_edges() == ""
		and (key == resolved_key or key.begins_with(resolved_key + "::"))
	):
		canonical_key = key
	result["revision_key"] = canonical_key
	var revision := _current_map_revision(canonical_key)
	result["map_revision"] = revision
	if result.get("planning_contract") is Dictionary:
		var contract: Dictionary = (result.get("planning_contract") as Dictionary).duplicate(true)
		contract["map_revision"] = revision
		contract.erase("facts_hash")
		contract["facts_hash"] = _dictionary_sha256(contract)
		result["planning_contract"] = contract


func _dictionary_sha256(value: Dictionary) -> String:
	var context := HashingContext.new()
	context.start(HashingContext.HASH_SHA256)
	context.update(JSON.stringify(value).to_utf8_buffer())
	return context.finish().hex_encode()


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
