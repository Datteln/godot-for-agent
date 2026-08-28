@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")
const ProgramTools = preload("res://addons/ai_agent/tools/program_tools.gd")
const GodotDiagnostics = preload("res://addons/ai_agent/context/godot_diagnostics.gd")

const MAX_RELOAD_TARGETS := 8
const RELOADABLE_EXTENSIONS := ["gd", "tscn", "tres"]
const SUPPORTED_MODES := ["editor_visible", "resource_only", "runtime_only"]
const FIXED_BUILDER_METHOD := "rebuild_from_layout"
const READABLE_LAYOUT_EXTENSIONS := ["json", "cfg", "csv", "txt"]

# 仅在本编辑器会话内记录失败状态。文件内容、布局或场景身份变更会产生新 fingerprint，
# 因此不会把已修复的 builder 永久锁住。
static var _failed_builder_fingerprints: Dictionary = {}


## 重载由已批准代码编辑产生的目标，并始终返回可区分的类型化状态。
static func reload_targets(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return _failure("unavailable", "editor_unavailable", "EditorInterface is not available")
	var mode := str(input.get("reload_mode", "")).strip_edges()
	if mode not in SUPPORTED_MODES:
		return _failure("unavailable", "unsupported_reload_mode", "reload_mode must be editor_visible, resource_only, or runtime_only")
	if _contains_user_data_path(input.get("targets", [])) or _contains_user_data_path(input.get("approved_paths", [])):
		return _failure("blocked", "reload_target_not_project_resource", "Reload targets and approved paths must use res:// project resources")
	var targets := _normalized_paths(input.get("targets", []))
	var approved_paths := _normalized_paths(input.get("approved_paths", []))
	if targets.is_empty() or targets.size() > MAX_RELOAD_TARGETS:
		return _failure("blocked", "invalid_reload_targets", "targets must contain between 1 and %d project-relative paths" % MAX_RELOAD_TARGETS)
	if approved_paths.is_empty() or not _targets_are_approved(targets, approved_paths):
		return _failure("blocked", "unapproved_reload_target", "Every reload target must be present in the approved code-edit batch")
	for target in targets:
		var extension := target.get_extension().to_lower()
		if extension not in RELOADABLE_EXTENSIONS:
			return _failure("unavailable", "unsupported_reload_target", "Only .gd, .tscn, and .tres targets can be reloaded", target)
		if not FileAccess.file_exists(ProjectSettings.globalize_path(target)):
			return _failure("failed", "reload_target_missing", "Reload target does not exist", target)
	if mode == "runtime_only":
		return {
			"ok": false,
			"status": "unavailable",
			"targets": targets,
			"reload_mode": mode,
			"visual_evidence": {"availability": "unavailable", "reason": "runtime_only_generator_not_executed"},
		}

	var dirty_scenes := _normalized_paths(editor_interface.get_unsaved_scenes())
	var dirty_target := _dirty_scene_target(targets, dirty_scenes)
	if dirty_target != "":
		return _failure("blocked", "reload_blocked_dirty_editor_state", "The target scene has unsaved editor changes; save or discard them manually before reloading", dirty_target)

	var filesystem := editor_interface.get_resource_filesystem()
	if filesystem == null:
		return _failure("unavailable", "editor_filesystem_unavailable", "EditorFileSystem is not available")
	var ordered_targets := _ordered_reload_targets(targets)
	for target in ordered_targets:
		filesystem.update_file(target)
	filesystem.scan_sources()
	await _wait_for_scan(filesystem, editor_interface)

	var reloaded: Array = []
	var unavailable: Array = []
	var open_scenes := _normalized_paths(editor_interface.get_open_scenes())
	for target in ordered_targets:
		if target.get_extension().to_lower() == "tscn":
			if target not in open_scenes:
				unavailable.append({"target": target, "reason": "scene_not_open"})
				continue
			editor_interface.reload_scene_from_path(target)
			reloaded.append(target)
		else:
			if target.get_extension().to_lower() == "gd":
				var script_validation := await validate_builder_script(target, editor_interface)
				if not bool(script_validation.get("ok", false)):
					return script_validation
			var resource := ResourceLoader.load(target, "", ResourceLoader.CACHE_MODE_REPLACE)
			if resource == null:
				var execution_id := GodotDiagnostics.operation_id("resource_reload")
				return _with_diagnostics(_failure("failed", "resource_reload_failed", "Godot could not reload the requested resource", target), [GodotDiagnostics.unlocated("resource_reload", execution_id, target, "Godot could not reload the requested resource")])
			reloaded.append(target)
	if reloaded.is_empty():
		return {"ok": false, "status": "unavailable", "error_code": "no_eligible_open_scene", "targets": targets, "reload_mode": mode, "unavailable_targets": unavailable, "visual_evidence": {"availability": "unavailable", "reason": "no_eligible_open_scene"}}
	var visual_availability := "eligible" if mode == "editor_visible" and _contains_scene(reloaded) else "unavailable"
	return {"ok": true, "status": "reloaded", "targets": targets, "reloaded_targets": reloaded, "unavailable_targets": unavailable, "reload_mode": mode, "execution_id": GodotDiagnostics.operation_id("resource_reload"), "diagnostics": [], "visual_evidence": {"availability": visual_availability, "reason": "editor_viewport_can_only_evidence_open_reloaded_scenes" if visual_availability == "eligible" else "resource_reload_has_no_target_scoped_viewport"}}


## 在当前编辑场景中调用已挂载 @tool builder 的固定重建接口。
static func rebuild_map_builder(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return _failure("unavailable", "editor_unavailable", "EditorInterface is not available")
	if _contains_user_data_path(input.get("approved_paths", [])):
		return _failure("blocked", "reload_target_not_project_resource", "Builder approved paths must use res:// project resources")
	var approved_paths := _normalized_paths(input.get("approved_paths", []))
	if approved_paths.is_empty():
		return _failure("blocked", "missing_approved_map_batch", "approved_paths must identify the approved map-authoring batch")
	var builder_path := str(input.get("builder_node_path", "")).strip_edges()
	if builder_path == "" or builder_path.begins_with("/") or builder_path.contains(".."):
		return _failure("blocked", "invalid_builder_node_path", "builder_node_path must be a non-empty scene-relative path")
	var scene_root := editor_interface.get_edited_scene_root()
	if scene_root == null:
		return _failure("unavailable", "no_edited_scene", "Open the selected map scene before rebuilding its builder")
	var builder := scene_root.get_node_or_null(NodePath(builder_path))
	if builder == null:
		return _failure("blocked", "builder_node_not_found", "The requested builder node is not attached to the open edited scene", builder_path)
	var script := builder.get_script()
	if not (script is Script) or str(script.resource_path).is_empty():
		return _failure("blocked", "builder_script_not_attached", "The builder must have an attached project script", builder_path)
	var raw_script_path := str(script.resource_path)
	if not PathUtils.is_res_path(PathUtils.to_godot_path(raw_script_path)):
		return _failure("blocked", "builder_script_not_project_resource", "The attached builder script must use a res:// project resource", raw_script_path)
	var script_observation := _script_path_observation(raw_script_path)
	var script_path := str(script_observation.get("normalized_resource_path", ""))
	if script_path == "" or not bool(script_observation.get("exists", false)):
		return _builder_script_missing(raw_script_path)
	var script_text := FileAccess.get_file_as_string(ProjectSettings.globalize_path(script_path))
	var preliminary_fingerprint := _builder_fingerprint(script_text, "", scene_root.scene_file_path, builder_path)
	if script_text.strip_edges().is_empty():
		return _remember_builder_failure(_failure("failed", "authoring_entry_point_missing", "The attached builder script is empty", script_path), preliminary_fingerprint, script_path)
	var script_validation := await validate_builder_script_after_scan(script_path, editor_interface)
	if not bool(script_validation.get("ok", false)):
		return _remember_builder_failure(script_validation, preliminary_fingerprint, script_path)
	if script_text.find("func %s" % FIXED_BUILDER_METHOD) == -1:
		return _remember_builder_failure(_failure("failed", "builder_script_invalid", "The on-disk builder script lacks the fixed rebuild interface", script_path), preliminary_fingerprint, script_path)
	if not builder.has_method(FIXED_BUILDER_METHOD):
		return _remember_builder_failure(_failure("blocked", "builder_instance_stale", "The on-disk builder defines rebuild_from_layout(), but the attached editor node still exposes an older script instance; reload the approved dependent scene before rebuilding", builder_path), preliminary_fingerprint, script_path)
	var layout_path := PathUtils.to_godot_path(str(builder.get("layout_path")))
	if not PathUtils.is_res_path(layout_path):
		return _failure("blocked", "builder_layout_not_project_resource", "The builder layout must use a res:// project resource", str(builder.get("layout_path")))
	if layout_path.get_extension().to_lower() not in READABLE_LAYOUT_EXTENSIONS:
		return _failure("blocked", "builder_layout_path_invalid", "The builder must expose a readable layout_path", builder_path)
	if layout_path not in approved_paths:
		return _failure("blocked", "layout_not_in_approved_batch", "The builder layout must be part of the approved map-authoring batch", layout_path)
	var layout_absolute := ProjectSettings.globalize_path(layout_path)
	if not FileAccess.file_exists(layout_absolute):
		return _failure("failed", "layout_missing", "The approved builder layout does not exist", layout_path)
	var layout_text := FileAccess.get_file_as_string(layout_absolute)
	var fingerprint := _builder_fingerprint(script_text, layout_text, scene_root.scene_file_path, builder_path)
	var repeated_failure := _prior_builder_failure(fingerprint)
	if not repeated_failure.is_empty():
		return repeated_failure
	var layout_validation := _validate_layout(layout_path, layout_text)
	if not bool(layout_validation.get("ok", false)):
		return _remember_builder_failure(layout_validation, fingerprint, layout_path)
	var generated_path: NodePath = builder.get("generated_target_path")
	if generated_path.is_empty():
		return _failure("blocked", "generated_target_missing", "The builder must expose generated_target_path", builder_path)
	var generated_target := builder.get_node_or_null(generated_path)
	if generated_target == null:
		return _failure("blocked", "generated_target_not_found", "The builder's generated target is not available", str(generated_path))
	if not bool(builder.get("generated_target_is_generated_only")):
		return _failure("blocked", "generated_target_ownership_unverified", "The builder must explicitly declare generated_target_is_generated_only", builder_path)
	var before_cells := _cell_count(generated_target)
	var invocation_result = builder.call(FIXED_BUILDER_METHOD)
	if not (invocation_result is Dictionary) or not bool(invocation_result.get("ok", false)):
		return _remember_builder_failure(_failure("failed", "builder_rebuild_failed", "rebuild_from_layout() did not return a successful typed result", builder_path), fingerprint, builder_path)
	_failed_builder_fingerprints.erase(fingerprint)
	return {
		"ok": true,
		"status": "rebuilt",
		"mutation": {"mutating": true, "kind": "tilemap_cells"},
		"scene": scene_root.scene_file_path,
		"builder_node_path": builder_path,
		"builder_script": script_path,
		"layout_path": layout_path,
		"generated_target_path": str(generated_target.get_path()),
		"before_generated_cells": before_cells,
		"after_generated_cells": _cell_count(generated_target),
		"changed_cell_bounds": {"status": "unavailable", "reason": "builder_does_not_report_exact_cell_delta"},
		"builder_result": invocation_result,
	}


## 将路径数组规范化为受项目边界保护的 res:// 路径。
static func _normalized_paths(value: Variant) -> Array[String]:
	var paths: Array[String] = []
	if not (value is Array):
		return paths
	for raw_path in value:
		var path := PathUtils.to_godot_path(str(raw_path))
		if path == "" or not PathUtils.is_res_path(path) or not PathUtils.is_read_allowed(path) or path in paths:
			return []
		paths.append(path)
	return paths


## 确认每个实际重载目标都来自该批已批准的代码编辑。
static func _targets_are_approved(targets: Array[String], approved_paths: Array[String]) -> bool:
	for target in targets:
		if target not in approved_paths:
			return false
	return true


static func _contains_user_data_path(value: Variant) -> bool:
	if not (value is Array):
		return false
	for raw_path in value:
		if PathUtils.is_user_path(PathUtils.to_godot_path(str(raw_path))):
			return true
	return false


## 将资源置于场景之前，避免场景节点继续引用本批次之前的脚本实例。
static func _ordered_reload_targets(targets: Array[String]) -> Array[String]:
	var resources: Array[String] = []
	var scenes: Array[String] = []
	for target in targets:
		if target.get_extension().to_lower() == "tscn":
			scenes.append(target)
		else:
			resources.append(target)
	resources.append_array(scenes)
	return resources


## 用 Godot 实际解析指定脚本，并保留可修复的路径/诊断证据。
static func validate_builder_script(script_path: String, editor_interface: EditorInterface) -> Dictionary:
	var observation := _script_path_observation(script_path)
	var normalized_path := str(observation.get("normalized_resource_path", ""))
	if normalized_path == "" or not bool(observation.get("exists", false)):
		return _builder_script_missing(script_path)
	var source := FileAccess.get_file_as_string(str(observation.get("globalized_path", "")))
	if source.strip_edges().is_empty():
		return _with_path_observation(_with_diagnostics(_failure("failed", "authoring_entry_point_missing", "The builder script is empty", normalized_path), [_fallback_diagnostic(normalized_path, "The builder script is empty")]), observation)
	var loaded := ResourceLoader.load(normalized_path, "Script", ResourceLoader.CACHE_MODE_REPLACE)
	if not (loaded is Script):
		return _with_path_observation(_with_diagnostics(_failure("failed", "builder_script_compile_failed", "Godot could not load the builder script", normalized_path), await _current_builder_diagnostics(normalized_path, editor_interface, "Godot could not load the builder script")), observation)
	var reload_error := (loaded as Script).reload(true)
	if reload_error != OK:
		var message := "Godot could not compile the builder script: %s" % error_string(reload_error)
		return _with_path_observation(_with_diagnostics(_failure("failed", "builder_script_compile_failed", message, normalized_path), await _current_builder_diagnostics(normalized_path, editor_interface, message)), observation)
	return _with_path_observation({"ok": true, "path": normalized_path, "diagnostics": []}, observation)


## 写入 builder 后必须先让 EditorFileSystem 扫描并重新观察磁盘，再请求 Godot 解析。
## 这样“文件刚写入、导入索引尚未更新”不会被误报为脚本缺失。
static func validate_builder_script_after_scan(script_path: String, editor_interface: EditorInterface) -> Dictionary:
	var observation := _script_path_observation(script_path)
	var normalized_path := str(observation.get("normalized_resource_path", ""))
	if normalized_path == "" or not bool(observation.get("exists", false)):
		return _builder_script_missing(script_path)
	if editor_interface == null:
		return _with_path_observation(_failure("unavailable", "editor_unavailable", "EditorInterface is not available", normalized_path), observation)
	var filesystem := editor_interface.get_resource_filesystem()
	if filesystem == null:
		return _with_path_observation(_failure("unavailable", "editor_filesystem_unavailable", "EditorFileSystem is not available", normalized_path), observation)
	filesystem.update_file(normalized_path)
	filesystem.scan_sources()
	await _wait_for_scan(filesystem, editor_interface)
	return await validate_builder_script(normalized_path, editor_interface)


## 返回原始资源路径、受项目边界保护的标准路径、全局化路径与存在性，绝不读取文件内容。
static func _script_path_observation(raw_resource_path: String) -> Dictionary:
	var candidate_path := PathUtils.to_godot_path(raw_resource_path)
	var normalized_path := candidate_path if PathUtils.is_res_path(candidate_path) else ""
	var globalized_path := ProjectSettings.globalize_path(normalized_path) if normalized_path != "" else ""
	return {
		"raw_resource_path": raw_resource_path,
		"normalized_resource_path": normalized_path,
		"globalized_path": globalized_path,
		"exists": normalized_path != "" and FileAccess.file_exists(globalized_path),
	}


static func _builder_script_missing(raw_resource_path: String) -> Dictionary:
	var observation := _script_path_observation(raw_resource_path)
	var normalized_path := str(observation.get("normalized_resource_path", ""))
	return _with_path_observation(_failure("failed", "builder_script_missing", "The attached builder script is unavailable", normalized_path), observation)


static func _with_path_observation(result: Dictionary, observation: Dictionary) -> Dictionary:
	result["raw_resource_path"] = observation.get("raw_resource_path", "")
	result["normalized_resource_path"] = observation.get("normalized_resource_path", "")
	result["globalized_path"] = observation.get("globalized_path", "")
	result["exists"] = bool(observation.get("exists", false))
	return result


## 仅使用当前控制验证的输出，绝不把历史日志的文件名匹配当作编译原因。
static func _current_builder_diagnostics(script_path: String, editor_interface: EditorInterface, fallback_message: String) -> Array:
	var validation := await ProgramTools.validate_gdscript_resource(script_path, editor_interface)
	var diagnostics: Array = validation.get("diagnostics", [])
	if diagnostics.is_empty() and fallback_message != "":
		var execution_id := str(validation.get("execution_id", GodotDiagnostics.operation_id("builder_validation")))
		diagnostics.append(GodotDiagnostics.unlocated("builder_validation", execution_id, script_path, fallback_message))
	return diagnostics


static func _fallback_diagnostic(script_path: String, message: String) -> Dictionary:
	return GodotDiagnostics.unlocated("builder_validation", GodotDiagnostics.operation_id("builder_validation"), script_path, message)


static func _with_diagnostics(result: Dictionary, diagnostics: Array) -> Dictionary:
	result["diagnostics"] = diagnostics
	return result


static func _builder_fingerprint(script_text: String, layout_text: String, scene_path: String, builder_path: String) -> String:
	return "%s:%s:%s:%s" % [script_text.sha256_text(), layout_text.sha256_text(), scene_path, builder_path]


static func _prior_builder_failure(fingerprint: String) -> Dictionary:
	if not _failed_builder_fingerprints.has(fingerprint):
		return {}
	var previous: Dictionary = _failed_builder_fingerprints[fingerprint]
	return {
		"ok": false,
		"status": "blocked",
		"error_code": "builder_repair_required",
		"message": "The same builder source/layout/scene state already failed; submit an approved repair before retrying execution",
		"target": previous.get("target", ""),
		"prior_error_code": previous.get("error_code", ""),
		"failed_builder_fingerprint": fingerprint,
		"repair_required": true,
	}


static func _remember_builder_failure(result: Dictionary, fingerprint: String, repair_target: String) -> Dictionary:
	_failed_builder_fingerprints[fingerprint] = {"error_code": result.get("error_code", "builder_rebuild_failed"), "target": repair_target}
	result["failed_builder_fingerprint"] = fingerprint
	result["repair_required"] = true
	return result


## 返回会被 reload 覆盖的脏场景；没有冲突时返回空字符串。
static func _dirty_scene_target(targets: Array[String], dirty_scenes: Array[String]) -> String:
	for target in targets:
		if target.get_extension().to_lower() == "tscn" and target in dirty_scenes:
			return target
	return ""


## 等待文件系统扫描结束，避免把未完成扫描误报为可观察的 reload。
static func _wait_for_scan(filesystem: EditorFileSystem, editor_interface: EditorInterface) -> void:
	var tree := editor_interface.get_base_control().get_tree()
	if tree == null:
		return
	var frames := 0
	while filesystem.is_scanning() and frames < 120:
		await tree.process_frame
		frames += 1


## 判断本次重载结果是否包含可进入编辑器视口的场景。
static func _contains_scene(paths: Array) -> bool:
	for path in paths:
		if str(path).get_extension().to_lower() == "tscn":
			return true
	return false


static func _validate_layout(path: String, content: String) -> Dictionary:
	if content.strip_edges().is_empty():
		return _failure("failed", "authoring_entry_point_missing", "The builder layout is empty", path)
	if path.get_extension().to_lower() == "json":
		var json := JSON.new()
		if json.parse(content) != OK:
			return _failure("failed", "layout_parse_failed", "The builder layout is not valid JSON", path)
	if path.get_extension().to_lower() == "cfg":
		var config := ConfigFile.new()
		if config.parse(content) != OK:
			return _failure("failed", "layout_parse_failed", "The builder layout is not valid CFG", path)
	return {"ok": true}


static func _cell_count(node: Node) -> int:
	if node != null and node.has_method("get_used_cells"):
		var cells = node.call("get_used_cells")
		if cells is Array:
			return cells.size()
	return -1


## 构造不会泄露文件内容的类型化失败诊断。
static func _failure(status: String, error_code: String, message: String, target: String = "") -> Dictionary:
	var result := {"ok": false, "status": status, "error_code": error_code, "message": message}
	if target != "":
		result["target"] = target
	return result
