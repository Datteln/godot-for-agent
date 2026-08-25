@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")

const MAX_RELOAD_TARGETS := 8
const RELOADABLE_EXTENSIONS := ["gd", "tscn", "tres"]
const SUPPORTED_MODES := ["editor_visible", "resource_only", "runtime_only"]


## 重载由已批准代码编辑产生的目标，并始终返回可区分的类型化状态。
static func reload_targets(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return _failure("unavailable", "editor_unavailable", "EditorInterface is not available")
	var mode := str(input.get("reload_mode", "")).strip_edges()
	if mode not in SUPPORTED_MODES:
		return _failure("unavailable", "unsupported_reload_mode", "reload_mode must be editor_visible, resource_only, or runtime_only")
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
			"ok": true,
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
	for target in targets:
		filesystem.update_file(target)
	filesystem.scan_sources()
	await _wait_for_scan(filesystem, editor_interface)

	var reloaded: Array = []
	var unavailable: Array = []
	var open_scenes := _normalized_paths(editor_interface.get_open_scenes())
	for target in targets:
		if target.get_extension().to_lower() == "tscn":
			if target not in open_scenes:
				unavailable.append({"target": target, "reason": "scene_not_open"})
				continue
			editor_interface.reload_scene_from_path(target)
			reloaded.append(target)
		else:
			var resource := ResourceLoader.load(target, "", ResourceLoader.CACHE_MODE_REPLACE)
			if resource == null:
				return _failure("failed", "resource_reload_failed", "Godot could not reload the requested resource", target)
			reloaded.append(target)
	if reloaded.is_empty():
		return {"ok": true, "status": "unavailable", "targets": targets, "reload_mode": mode, "unavailable_targets": unavailable, "visual_evidence": {"availability": "unavailable", "reason": "no_eligible_open_scene"}}
	var visual_availability := "eligible" if mode == "editor_visible" and _contains_scene(reloaded) else "unavailable"
	return {"ok": true, "status": "reloaded", "targets": targets, "reloaded_targets": reloaded, "unavailable_targets": unavailable, "reload_mode": mode, "visual_evidence": {"availability": visual_availability, "reason": "editor_viewport_can_only_evidence_open_reloaded_scenes" if visual_availability == "eligible" else "resource_reload_has_no_target_scoped_viewport"}}


## 将路径数组规范化为受项目边界保护的 res:// 路径。
static func _normalized_paths(value: Variant) -> Array[String]:
	var paths: Array[String] = []
	if not (value is Array):
		return paths
	for raw_path in value:
		var path := PathUtils.to_res_path(str(raw_path))
		if path == "" or not PathUtils.is_read_allowed(path) or path in paths:
			return []
		paths.append(path)
	return paths


## 确认每个实际重载目标都来自该批已批准的代码编辑。
static func _targets_are_approved(targets: Array[String], approved_paths: Array[String]) -> bool:
	for target in targets:
		if target not in approved_paths:
			return false
	return true


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


## 构造不会泄露文件内容的类型化失败诊断。
static func _failure(status: String, error_code: String, message: String, target: String = "") -> Dictionary:
	var result := {"ok": true, "status": status, "error_code": error_code, "message": message}
	if target != "":
		result["target"] = target
	return result
