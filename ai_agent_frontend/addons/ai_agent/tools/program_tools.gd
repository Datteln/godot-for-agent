@tool
extends RefCounted

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")


static func read_file(input: Dictionary, file_state_cache: Node = null) -> Dictionary:
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	if path == "":
		return {"ok": false, "message": "path is required"}
	if not PathUtils.is_read_allowed(path):
		return {"ok": false, "message": "reading this path is not allowed: " + path, "error_code": "read_denied"}
	if not FileAccess.file_exists(ProjectSettings.globalize_path(path)):
		return {"ok": false, "message": "file does not exist", "path": path}
	var content := FileAccess.get_file_as_string(ProjectSettings.globalize_path(path))
	if file_state_cache != null:
		file_state_cache.snapshot(path, true)
	return {
		"ok": true,
		"path": path,
		"content": content
	}


static func write_file(input: Dictionary, undo_manager: Node, file_state_cache: Node) -> Dictionary:
	var path := PathUtils.to_res_path(str(input.get("path", input.get("target_path", ""))))
	var after_text := str(input.get("content", input.get("after_text", "")))
	if path == "":
		return {"ok": false, "message": "path is required"}
	if not PathUtils.is_write_allowed(path):
		return {"ok": false, "message": "writing to this path is not allowed: " + path, "error_code": "write_denied"}

	if file_state_cache != null and file_state_cache.is_stale(path):
		return {
			"ok": false,
			"message": "file changed on disk since it was last read: " + path,
			"error_code": "file_stale",
			"path": path
		}

	var before_text := ""
	if FileAccess.file_exists(ProjectSettings.globalize_path(path)):
		before_text = FileAccess.get_file_as_string(ProjectSettings.globalize_path(path))

	var before_state := {}
	if file_state_cache != null:
		before_state = file_state_cache.snapshot(path, true)

	if undo_manager == null:
		return {"ok": false, "message": "undo manager is not available"}

	undo_manager.record_file_write(path, before_text, after_text)

	var after_state := {}
	if file_state_cache != null:
		after_state = file_state_cache.snapshot(path, true)

	return {
		"ok": true,
		"path": path,
		"before_hash": before_state.get("hash", before_text.sha256_text()),
		"after_hash": after_state.get("hash", after_text.sha256_text()),
		"mtime_ns": after_state.get("mtime_ns", 0),
		"known_full_read": true
	}


static func run_tests(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var kind := str(input.get("kind", "project"))
	if kind != "project" and kind != "headless_scene":
		return {"ok": false, "message": "unknown runner kind: " + kind}
	if editor_interface == null:
		return {"ok": false, "message": "editor_interface is not available"}

	var executable_key := "ai_agent/test_executable"
	var args_key := "ai_agent/test_args"
	var log_key := "ai_agent/test_output_log"
	if kind == "headless_scene":
		executable_key = "ai_agent/headless_executable"
		args_key = "ai_agent/headless_args"
		log_key = "ai_agent/headless_output_log"

	var executable := str(ConfigMigrations.get_value(editor_interface, executable_key)).strip_edges()
	if executable == "":
		return {
			"ok": false,
			"message": "No runner executable configured in EditorSettings.",
			"setting": executable_key
		}

	var args := _split_args(str(ConfigMigrations.get_value(editor_interface, args_key)))
	var configured_timeout := int(ConfigMigrations.get_value(editor_interface, "ai_agent/runner_timeout_ms"))
	var requested_timeout := int(input.get("timeout_ms", configured_timeout))
	var timeout_ms = min(max(requested_timeout, 1000), max(configured_timeout, 1000))
	var log_path := str(ConfigMigrations.get_value(editor_interface, log_key)).strip_edges()
	var run_result: Dictionary = await _run_process_with_timeout(executable, args, timeout_ms)
	var output_text := _read_optional_log(log_path)
	return {
		"ok": bool(run_result.get("ok", false)),
		"kind": kind,
		"pid": run_result.get("pid", -1),
		"exit_code": run_result.get("exit_code", null),
		"status": run_result.get("status", "failed"),
		"executable_setting": executable_key,
		"args_setting": args_key,
		"log_setting": log_key,
		"timeout_ms": timeout_ms,
		"output": output_text
	}


static func read_profiler_snapshot(_input: Dictionary = {}) -> Dictionary:
	var monitors := {
		"time_fps": Performance.TIME_FPS,
		"time_process": Performance.TIME_PROCESS,
		"time_physics_process": Performance.TIME_PHYSICS_PROCESS,
		"memory_static": Performance.MEMORY_STATIC,
		"memory_static_max": Performance.MEMORY_STATIC_MAX,
		"object_count": Performance.OBJECT_COUNT,
		"object_resource_count": Performance.OBJECT_RESOURCE_COUNT,
		"object_node_count": Performance.OBJECT_NODE_COUNT,
		"object_orphan_node_count": Performance.OBJECT_ORPHAN_NODE_COUNT,
		"render_objects_in_frame": Performance.RENDER_TOTAL_OBJECTS_IN_FRAME,
		"render_primitives_in_frame": Performance.RENDER_TOTAL_PRIMITIVES_IN_FRAME,
		"render_draw_calls_in_frame": Performance.RENDER_TOTAL_DRAW_CALLS_IN_FRAME
	}
	var values := {}
	for key in monitors.keys():
		values[key] = Performance.get_monitor(monitors[key])
	return {
		"ok": true,
		"values": values,
		"captured_at_msec": Time.get_ticks_msec()
	}


static func _split_args(args_text: String) -> PackedStringArray:
	var result := PackedStringArray()
	for part in args_text.strip_edges().split(" ", false):
		if str(part).strip_edges() != "":
			result.append(str(part).strip_edges())
	return result


static func _run_process_with_timeout(executable: String, args: PackedStringArray, timeout_ms: int) -> Dictionary:
	var pid := OS.create_process(executable, args, false)
	if pid <= 0:
		return {"ok": false, "status": "failed_to_start", "pid": pid, "exit_code": null}
	var tree := Engine.get_main_loop() as SceneTree
	var start_ms := Time.get_ticks_msec()
	while OS.is_process_running(pid):
		if Time.get_ticks_msec() - start_ms > timeout_ms:
			OS.kill(pid)
			return {"ok": false, "status": "timed_out", "pid": pid, "exit_code": null}
		if tree != null:
			await tree.create_timer(0.1).timeout
		else:
			OS.delay_msec(100)
	return {"ok": true, "status": "completed", "pid": pid, "exit_code": null}


static func _read_optional_log(path: String) -> String:
	if path == "":
		return ""
	var actual := path
	if path.begins_with("res://") or path.begins_with("user://"):
		actual = ProjectSettings.globalize_path(path)
	if not FileAccess.file_exists(actual):
		return ""
	var file := FileAccess.open(actual, FileAccess.READ)
	if file == null:
		return ""
	var text := file.get_as_text()
	if text.length() > 20000:
		return text.substr(text.length() - 20000)
	return text
