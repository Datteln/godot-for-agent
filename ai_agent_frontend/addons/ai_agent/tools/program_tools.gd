@tool
extends RefCounted

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")

const MAX_STALE_CONTENT_CHARS := 40000
const MAX_SYSTEM_COMMAND_CHARS := 100000
const MAX_SYSTEM_COMMAND_OUTPUT_CHARS := 200000
const GIT_COMMAND_TIMEOUT_MS := 15000


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


static func run_system_command(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var command := str(input.get("command", ""))
	if command.strip_edges() == "":
		return {"ok": false, "message": "command is required", "error_code": "command_required"}
	if command.length() > MAX_SYSTEM_COMMAND_CHARS:
		return {"ok": false, "message": "command is too long", "error_code": "command_too_long"}
	if editor_interface == null:
		return {"ok": false, "message": "editor_interface is not available"}

	var shell_name := str(input.get("shell", "auto")).to_lower().strip_edges()
	var shell := _resolve_system_shell(shell_name)
	if shell.is_empty():
		return {
			"ok": false,
			"message": "shell is not supported on this platform: " + shell_name,
			"error_code": "unsupported_shell"
		}
	var working_directory := _resolve_working_directory(str(input.get("working_directory", "res://")))
	if working_directory == "" or not DirAccess.dir_exists_absolute(working_directory):
		return {
			"ok": false,
			"message": "working directory does not exist: " + str(input.get("working_directory", "res://")),
			"error_code": "working_directory_missing"
		}

	var configured_timeout := int(ConfigMigrations.get_value(editor_interface, "ai_agent/system_command_timeout_ms"))
	var requested_timeout := int(input.get("timeout_ms", configured_timeout))
	var timeout_ms = min(max(requested_timeout, 1000), max(configured_timeout, 1000))
	var launch := _build_system_command_launch(command, shell, working_directory)
	if launch.is_empty():
		return {"ok": false, "message": "failed to prepare system command", "error_code": "failed_to_prepare"}

	FrontendLogger.info(editor_interface, "ProgramTools", "Launching confirmed system command.", {
		"shell": shell.get("name", shell_name),
		"working_directory": working_directory,
		"timeout_ms": timeout_ms,
	})
	var run_result: Dictionary = await _run_system_command_launch(launch, timeout_ms)
	var output := _read_bounded_file(str(launch.get("output_path", "")), MAX_SYSTEM_COMMAND_OUTPUT_CHARS)
	_cleanup_runner_files(launch)
	var ok := bool(run_result.get("ok", false))
	var status := str(run_result.get("status", "failed"))
	var exit_code = run_result.get("exit_code", null)
	var result := {
		"ok": ok,
		"status": status,
		"pid": run_result.get("pid", -1),
		"exit_code": exit_code,
		"shell": shell.get("name", shell_name),
		"working_directory": working_directory,
		"timeout_ms": timeout_ms,
		"output": output,
		"output_truncated": output.length() >= MAX_SYSTEM_COMMAND_OUTPUT_CHARS,
	}
	if not ok:
		result["error_code"] = status
		result["message"] = _describe_system_command_failure(status, exit_code, output)
	return result


## 用本编辑器自身的 Godot 可执行文件以 `--headless --script` 方式直接运行一个项目内
## 的 .gd 文件，并捕获其 stdout/stderr 与退出码。与 run_system_command 不同，这里
## 不经过用户输入的自由文本 shell 命令，可执行文件与参数都是受控构造的，只有目标
## 脚本路径和透传给脚本的参数来自工具调用入参。
static func execute_gd_script(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "editor_interface is not available", "error_code": "editor_unavailable"}
	var res_path := PathUtils.to_res_path(str(input.get("path", "")))
	if res_path == "" or res_path.get_extension().to_lower() != "gd":
		return {
			"ok": false,
			"message": "path must be a project-relative .gd file",
			"error_code": "invalid_path"
		}
	if not FileAccess.file_exists(res_path):
		return {
			"ok": false,
			"message": "script file not found: " + res_path,
			"error_code": "script_not_found"
		}

	var script_args := PackedStringArray()
	var raw_args = input.get("args", [])
	if raw_args is Array:
		for arg in raw_args:
			script_args.append(str(arg))

	var configured_timeout := int(ConfigMigrations.get_value(editor_interface, "ai_agent/gd_script_timeout_ms"))
	var requested_timeout := int(input.get("timeout_ms", configured_timeout))
	var timeout_ms = min(max(requested_timeout, 1000), max(configured_timeout, 1000))

	var project_path := ProjectSettings.globalize_path("res://")
	var launch_args := PackedStringArray(["--headless", "--path", project_path, "--script", res_path])
	for arg in script_args:
		launch_args.append(arg)

	FrontendLogger.info(editor_interface, "ProgramTools", "Launching gd script.", {
		"path": res_path,
		"timeout_ms": timeout_ms,
	})
	var launch := _build_direct_process_launch(OS.get_executable_path(), launch_args, project_path, "ai_agent_gdscript")
	if launch.is_empty():
		return {"ok": false, "message": "failed to prepare gd script launch", "error_code": "failed_to_prepare"}
	var run_result: Dictionary = await _run_system_command_launch(launch, timeout_ms)
	var output := _read_bounded_file(str(launch.get("output_path", "")), MAX_SYSTEM_COMMAND_OUTPUT_CHARS)
	_cleanup_runner_files(launch)
	var ok := bool(run_result.get("ok", false))
	var status := str(run_result.get("status", "failed"))
	var exit_code = run_result.get("exit_code", null)
	var result := {
		"ok": ok,
		"status": status,
		"pid": run_result.get("pid", -1),
		"exit_code": exit_code,
		"path": res_path,
		"timeout_ms": timeout_ms,
		"output": output,
		"output_truncated": output.length() >= MAX_SYSTEM_COMMAND_OUTPUT_CHARS,
	}
	if not ok:
		result["error_code"] = status
		result["message"] = _describe_system_command_failure(status, exit_code, output)
	return result


## git_status/git_diff 用固定的可执行文件名与参数数组直接拼起子进程，不经过用户输入
## 的自由文本，所以不需要每次确认；风险等同于其他只读诊断工具。
static func git_status(editor_interface: EditorInterface) -> Dictionary:
	return await _run_git_command(PackedStringArray(["status", "--porcelain=v1", "-b"]), editor_interface)


static func git_diff(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	var args := PackedStringArray(["diff"])
	if bool(input.get("staged", false)):
		args.append("--staged")
	var target := str(input.get("path", "")).strip_edges()
	if target != "":
		var res_path := PathUtils.to_res_path(target)
		if res_path == "":
			return {"ok": false, "message": "path must be a project-relative path", "error_code": "invalid_path"}
		args.append("--")
		args.append(res_path.trim_prefix("res://"))
	return await _run_git_command(args, editor_interface)


static func _run_git_command(args: PackedStringArray, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "editor_interface is not available", "error_code": "editor_unavailable"}
	var project_path := ProjectSettings.globalize_path("res://")
	var launch := _build_direct_process_launch("git", args, project_path, "ai_agent_git")
	if launch.is_empty():
		return {"ok": false, "message": "failed to prepare git command", "error_code": "failed_to_prepare"}
	var run_result: Dictionary = await _run_system_command_launch(launch, GIT_COMMAND_TIMEOUT_MS)
	var output := _read_bounded_file(str(launch.get("output_path", "")), MAX_SYSTEM_COMMAND_OUTPUT_CHARS)
	_cleanup_runner_files(launch)
	var ok := bool(run_result.get("ok", false))
	var status := str(run_result.get("status", "failed"))
	var exit_code = run_result.get("exit_code", null)
	var result := {
		"ok": ok,
		"status": status,
		"exit_code": exit_code,
		"output": output,
		"output_truncated": output.length() >= MAX_SYSTEM_COMMAND_OUTPUT_CHARS,
	}
	if not ok:
		result["error_code"] = status
		result["message"] = _describe_system_command_failure(status, exit_code, output)
	return result


## 用编辑器自身的 Godot 可执行文件以 `--export-release`/`--export-debug` 方式触发导出，
## 复用 run_system_command 的子进程基础设施，但可执行文件与参数都是受控构造的
## （只有 preset 名称和输出路径来自调用入参），跟 execute_gd_script 同一思路。
static func export_project(input: Dictionary, editor_interface: EditorInterface) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "editor_interface is not available", "error_code": "editor_unavailable"}
	var preset := str(input.get("preset", "")).strip_edges()
	if preset == "":
		return {"ok": false, "message": "preset is required", "error_code": "preset_required"}
	var output_path := PathUtils.to_res_path(str(input.get("output_path", "")))
	if output_path == "":
		return {"ok": false, "message": "output_path must be a project-relative path", "error_code": "invalid_output_path"}
	if not PathUtils.is_write_allowed(output_path):
		return {"ok": false, "message": "output_path is not writable: " + output_path, "error_code": "path_denied"}
	var debug := bool(input.get("debug", false))

	var output_absolute := ProjectSettings.globalize_path(output_path)
	DirAccess.make_dir_recursive_absolute(output_absolute.get_base_dir())

	var configured_timeout := int(ConfigMigrations.get_value(editor_interface, "ai_agent/export_timeout_ms"))
	var requested_timeout := int(input.get("timeout_ms", configured_timeout))
	var timeout_ms = min(max(requested_timeout, 1000), max(configured_timeout, 1000))

	var project_path := ProjectSettings.globalize_path("res://")
	var export_flag := "--export-debug" if debug else "--export-release"
	var launch_args := PackedStringArray(["--headless", "--path", project_path, export_flag, preset, output_absolute])
	var launch := _build_direct_process_launch(OS.get_executable_path(), launch_args, project_path, "ai_agent_export")
	if launch.is_empty():
		return {"ok": false, "message": "failed to prepare export launch", "error_code": "failed_to_prepare"}

	FrontendLogger.info(editor_interface, "ProgramTools", "Launching project export.", {
		"preset": preset,
		"output_path": output_path,
		"timeout_ms": timeout_ms,
	})
	var run_result: Dictionary = await _run_system_command_launch(launch, timeout_ms)
	var output := _read_bounded_file(str(launch.get("output_path", "")), MAX_SYSTEM_COMMAND_OUTPUT_CHARS)
	_cleanup_runner_files(launch)
	var ok := bool(run_result.get("ok", false))
	var status := str(run_result.get("status", "failed"))
	var exit_code = run_result.get("exit_code", null)
	var result := {
		"ok": ok,
		"status": status,
		"exit_code": exit_code,
		"preset": preset,
		"output_path": output_path,
		"output": output,
		"output_truncated": output.length() >= MAX_SYSTEM_COMMAND_OUTPUT_CHARS,
	}
	if not ok:
		result["error_code"] = status
		result["message"] = _describe_system_command_failure(status, exit_code, output)
	return result


static func _describe_system_command_failure(status: String, exit_code, output: String) -> String:
	var tail := output.strip_edges()
	if tail.length() > 400:
		tail = tail.right(400)
	match status:
		"failed_to_start":
			return "failed to start the shell process"
		"timed_out":
			return "command timed out"
		"exit_code_missing":
			return "command did not report an exit code (process may have been killed)"
		_:
			var base := "command exited with code %s" % str(exit_code)
			return base if tail == "" else "%s: %s" % [base, tail]


static func _resolve_system_shell(requested: String) -> Dictionary:
	var name := requested if requested != "" else "auto"
	var windows := OS.get_name() == "Windows"
	if name == "auto":
		name = "powershell" if windows else "sh"
	match name:
		"powershell":
			return {
				"name": "powershell",
				"executable": "powershell.exe" if windows else "pwsh",
				"args": PackedStringArray(["-NoProfile", "-Command"]),
			}
		"pwsh":
			return {
				"name": "pwsh",
				"executable": "pwsh.exe" if windows else "pwsh",
				"args": PackedStringArray(["-NoProfile", "-Command"]),
			}
		"cmd":
			if not windows:
				return {}
			return {"name": "cmd", "executable": "cmd.exe", "args": PackedStringArray(["/D", "/S", "/C"])}
		"sh", "bash", "zsh":
			var executable := name + (".exe" if windows else "")
			if name == "sh" and not windows:
				executable = "/bin/sh"
			return {"name": name, "executable": executable, "args": PackedStringArray(["-c"])}
		_:
			return {}


static func _resolve_working_directory(value: String) -> String:
	var path := value.strip_edges()
	if path == "":
		path = "res://"
	if path.begins_with("res://") or path.begins_with("user://"):
		return ProjectSettings.globalize_path(path).simplify_path()
	if path.is_absolute_path():
		return path.simplify_path()
	return ProjectSettings.globalize_path("res://" + path).simplify_path()


static func _build_system_command_launch(command: String, shell: Dictionary, working_directory: String) -> Dictionary:
	var shell_args: PackedStringArray = shell.get("args", PackedStringArray()).duplicate()
	shell_args.append(command)
	return _build_direct_process_launch(str(shell.get("executable", "")), shell_args, working_directory, "ai_agent_command")


## 直接以 executable+args 数组启动子进程并捕获 stdout/stderr 与退出码到临时文件，
## 不经过"把参数拼成一段文本再交给 shell 解析"这一步，因此调用方传入的每个
## 参数都是字面值，不会被目标 shell 的引号/转义规则二次解释。
static func _build_direct_process_launch(executable: String, args: PackedStringArray, working_directory: String, token_prefix: String = "ai_agent_command") -> Dictionary:
	var token := "%d_%d" % [Time.get_ticks_usec(), randi()]
	var windows := OS.get_name() == "Windows"
	var script_path := "user://%s_%s%s" % [token_prefix, token, ".ps1" if windows else ".sh"]
	var exit_path := "user://%s_%s.exit" % [token_prefix, token]
	var output_path := "user://%s_%s.log" % [token_prefix, token]
	var script_abs := ProjectSettings.globalize_path(script_path)
	var exit_abs := ProjectSettings.globalize_path(exit_path)
	var output_abs := ProjectSettings.globalize_path(output_path)
	var shell_args: PackedStringArray = args
	var script := ""
	var launch_executable := ""
	var launch_args := PackedStringArray()
	if windows:
		# Windows PowerShell 5.1 的 `>`/`*>` 默认生成 UTF-16 LE 文件，Godot 会按
		# UTF-8 读取并报 Invalid UTF-8。通过显式 UTF-8 StreamWriter 流式合并
		# stdout/stderr，既避免整段输出驻留内存，也兼容 PowerShell 5.1 与 pwsh。
		script = "$exe = '%s'\n$argList = @(%s)\nSet-Location -LiteralPath '%s'\n$utf8 = New-Object System.Text.UTF8Encoding($false)\n$writer = New-Object System.IO.StreamWriter('%s', $false, $utf8)\ntry {\n  & $exe @argList 2>&1 | ForEach-Object { $writer.WriteLine($_.ToString()) }\n  $code = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }\n} finally {\n  $writer.Dispose()\n}\nSet-Content -LiteralPath '%s' -Value $code -Encoding ASCII\n" % [
			_powershell_quote(executable),
			_powershell_array(shell_args),
			_powershell_quote(working_directory),
			_powershell_quote(output_abs),
			_powershell_quote(exit_abs),
		]
		launch_executable = "powershell.exe"
		launch_args = PackedStringArray(["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_abs])
	else:
		script = "cd %s || exit 125\n%s %s > %s 2>&1\ncode=$?\nprintf '%%s' \"$code\" > %s\n" % [
			_shell_quote(working_directory),
			_shell_quote(executable),
			_shell_args(shell_args),
			_shell_quote(output_abs),
			_shell_quote(exit_abs),
		]
		launch_executable = "/bin/sh"
		launch_args = PackedStringArray([script_abs])
	var file := FileAccess.open(script_abs, FileAccess.WRITE)
	if file == null:
		return {}
	if windows:
		# Windows PowerShell 5.1 only recognizes non-ASCII script text as UTF-8 when
		# the script has a BOM. This preserves Unicode commands and working paths.
		file.store_buffer(PackedByteArray([0xEF, 0xBB, 0xBF]))
	file.store_string(script)
	file.close()
	return {
		"executable": launch_executable,
		"args": launch_args,
		"script_path": script_abs,
		"exit_path": exit_abs,
		"output_path": output_abs,
	}


static func _run_system_command_launch(launch: Dictionary, timeout_ms: int) -> Dictionary:
	var launch_args: PackedStringArray = launch.get("args", PackedStringArray())
	var pid := OS.create_process(str(launch.get("executable", "")), launch_args, false)
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
	var exit_code = _read_runner_exit_code(str(launch.get("exit_path", "")))
	if exit_code == null:
		return {"ok": false, "status": "exit_code_missing", "pid": pid, "exit_code": null}
	var code := int(exit_code)
	return {"ok": code == 0, "status": "completed" if code == 0 else "failed", "pid": pid, "exit_code": code}


static func _read_bounded_file(path: String, max_chars: int) -> String:
	if path == "" or not FileAccess.file_exists(path):
		return ""
	var text := FileAccess.get_file_as_string(path)
	if text.length() > max_chars:
		return text.left(max_chars)
	return text


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
	for key in ["script_path", "exit_path", "output_path"]:
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
