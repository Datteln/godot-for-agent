@tool
extends RefCounted

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

const MAX_STALE_CONTENT_CHARS := 40000


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


static func write_file(input: Dictionary, undo_manager: Node, file_state_cache: Node, editor_interface: EditorInterface = null) -> Dictionary:
	var path := PathUtils.to_res_path(str(input.get("path", input.get("target_path", ""))))
	var after_text := str(input.get("content", input.get("after_text", "")))
	if path == "":
		return {"ok": false, "message": "path is required"}
	if not PathUtils.is_write_allowed(path):
		FrontendLogger.warn(editor_interface, "ProgramTools", "Blocked write outside allowed paths.", {"path": path})
		return {"ok": false, "message": "writing to this path is not allowed: " + path, "error_code": "write_denied"}

	if file_state_cache != null and file_state_cache.is_stale(path):
		# 把磁盘上的最新内容直接带回去，让 LLM 不用再额外调一次 read_file 才能拿到
		# 最新内容；同时刷新快照，避免基于这份最新内容重新提交的编辑又被误判一次 stale。
		var current_content := ""
		if FileAccess.file_exists(ProjectSettings.globalize_path(path)):
			current_content = FileAccess.get_file_as_string(ProjectSettings.globalize_path(path))
			if current_content.length() > MAX_STALE_CONTENT_CHARS:
				current_content = current_content.left(MAX_STALE_CONTENT_CHARS) + "\n\n... (content truncated)"
		file_state_cache.snapshot(path, true)
		FrontendLogger.warn(editor_interface, "ProgramTools", "Rejected write of stale file.", {"path": path})
		return {
			"ok": false,
			"message": (
				"file changed on disk since it was last read: " + path
				+ ". The up-to-date content is included as `current_content` below — "
				+ "use it directly to construct your next edit instead of calling read_file again."
			),
			"error_code": "file_stale",
			"path": path,
			"current_content": current_content
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

	FrontendLogger.info(editor_interface, "ProgramTools", "Wrote file.", {
		"path": path,
		"before_bytes": before_text.length(),
		"after_bytes": after_text.length(),
	})
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
	FrontendLogger.info(editor_interface, "ProgramTools", "Launching runner process.", {
		"kind": kind,
		"executable": executable,
		"timeout_ms": timeout_ms,
	})
	var run_result: Dictionary = await _run_process_with_timeout(executable, args, timeout_ms)
	var output_text := _read_optional_log(log_path)
	var status := str(run_result.get("status", "failed"))
	if bool(run_result.get("ok", false)):
		FrontendLogger.info(editor_interface, "ProgramTools", "Runner process completed.", {
			"kind": kind,
			"pid": run_result.get("pid", -1),
			"exit_code": run_result.get("exit_code", null),
		})
	else:
		FrontendLogger.warn(editor_interface, "ProgramTools", "Runner process did not complete successfully.", {
			"kind": kind,
			"status": status,
			"pid": run_result.get("pid", -1),
			"exit_code": run_result.get("exit_code", null),
		})
	return {
		"ok": bool(run_result.get("ok", false)),
		"kind": kind,
		"pid": run_result.get("pid", -1),
		"exit_code": run_result.get("exit_code", null),
		"status": status,
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
	var launch := _build_runner_launch(executable, args)
	if launch.is_empty():
		return {"ok": false, "status": "failed_to_prepare", "pid": -1, "exit_code": null}
	var launch_args: PackedStringArray = launch.get("args", PackedStringArray())
	var pid := OS.create_process(str(launch.get("executable", "")), launch_args, false)
	if pid <= 0:
		_cleanup_runner_files(launch)
		return {"ok": false, "status": "failed_to_start", "pid": pid, "exit_code": null}
	var tree := Engine.get_main_loop() as SceneTree
	var start_ms := Time.get_ticks_msec()
	while OS.is_process_running(pid):
		if Time.get_ticks_msec() - start_ms > timeout_ms:
			OS.kill(pid)
			_cleanup_runner_files(launch)
			return {"ok": false, "status": "timed_out", "pid": pid, "exit_code": null}
		if tree != null:
			await tree.create_timer(0.1).timeout
		else:
			OS.delay_msec(100)
	var exit_code = _read_runner_exit_code(str(launch.get("exit_path", "")))
	_cleanup_runner_files(launch)
	if exit_code == null:
		return {"ok": false, "status": "exit_code_missing", "pid": pid, "exit_code": null}
	var code := int(exit_code)
	return {"ok": code == 0, "status": "completed" if code == 0 else "failed", "pid": pid, "exit_code": code}


static func _build_runner_launch(executable: String, args: PackedStringArray) -> Dictionary:
	var token := "%d_%d" % [Time.get_ticks_usec(), randi()]
	var script_path := "user://ai_agent_runner_%s%s" % [token, ".ps1" if OS.get_name() == "Windows" else ".sh"]
	var exit_path := "user://ai_agent_runner_%s.exit" % token
	var script_abs := ProjectSettings.globalize_path(script_path)
	var exit_abs := ProjectSettings.globalize_path(exit_path)
	var script := ""
	var launch_executable := ""
	var launch_args := PackedStringArray()
	if OS.get_name() == "Windows":
		script = "$exe = '%s'\n$argList = @(%s)\n& $exe @argList\n$code = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } else { 0 }\nSet-Content -LiteralPath '%s' -Value $code -Encoding ASCII\n" % [
			_powershell_quote(executable),
			_powershell_array(args),
			_powershell_quote(exit_abs)
		]
		launch_executable = "powershell.exe"
		launch_args = PackedStringArray(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_abs])
	else:
		script = "#!/bin/sh\n%s %s\ncode=$?\nprintf '%%s' \"$code\" > %s\n" % [
			_shell_quote(executable),
			_shell_args(args),
			_shell_quote(exit_abs)
		]
		launch_executable = "/bin/sh"
		launch_args = PackedStringArray([script_abs])
	var file := FileAccess.open(script_abs, FileAccess.WRITE)
	if file == null:
		return {}
	file.store_string(script)
	file.close()
	return {
		"executable": launch_executable,
		"args": launch_args,
		"script_path": script_abs,
		"exit_path": exit_abs
	}


static func _powershell_quote(value: String) -> String:
	return value.replace("'", "''")


static func _powershell_array(args: PackedStringArray) -> String:
	var parts: Array[String] = []
	for arg in args:
		parts.append("'%s'" % _powershell_quote(str(arg)))
	return ", ".join(parts)


static func _shell_quote(value: String) -> String:
	return "'" + value.replace("'", "'\"'\"'") + "'"


static func _shell_args(args: PackedStringArray) -> String:
	var parts: Array[String] = []
	for arg in args:
		parts.append(_shell_quote(str(arg)))
	return " ".join(parts)


static func _read_runner_exit_code(exit_path: String) -> Variant:
	if exit_path == "" or not FileAccess.file_exists(exit_path):
		return null
	var text := FileAccess.get_file_as_string(exit_path).strip_edges()
	if not text.is_valid_int():
		return null
	return int(text)


static func _cleanup_runner_files(launch: Dictionary) -> void:
	for key in ["script_path", "exit_path"]:
		var path := str(launch.get(key, ""))
		if path != "" and FileAccess.file_exists(path):
			DirAccess.remove_absolute(path)


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
