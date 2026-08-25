## 聊天展示稿渲染层契约测试（change: chat-transcript-rendering 任务 1.3 / 4.3）。
##
## 覆盖：相同正文双条目、流式 revision 原地更新、单条展示预算的预览/显示完整/
## 驱逐/重挂载（所有长内容 kind）、Thought 流式/完成/展开/复制/预算边界/终态
## 回退拒绝、选区保留、畸形 Markdown、解决态审批一行文本 + 独立补充用户条目、
## 紧凑工具结果、部分失败修改警示、错误上下文与条件重试、瞬时提示丢弃、
## 缺少 kind 的输入拒绝。
extends SceneTree

const TranscriptStore = preload("res://addons/ai_agent/transcript/transcript_store.gd")
const TranscriptProjector = preload("res://addons/ai_agent/transcript/transcript_projector.gd")
const TranscriptRenderer = preload("res://addons/ai_agent/transcript/transcript_renderer.gd")
const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")
const LogEntryRenderer = preload("res://addons/ai_agent/ui/log_entry_renderer.gd")
const TransientNoticeHost = preload("res://addons/ai_agent/ui/transient_notice_host.gd")
const TranscriptViewport = preload("res://addons/ai_agent/transcript/transcript_viewport.gd")

var _failures := 0
var _checks := 0


class StableMinimumRoot extends Control:
	func _get_minimum_size() -> Vector2:
		return Vector2(0, 28)


class ViewportStore:
	var entry := {
		"entry_id": "thought-layout", "ordinal": 0, "kind": "thought", "state": "thinking",
		"revision": 1, "payload": {"content": "brief"},
	}

	func ordered_entry_ids() -> Array:
		return ["thought-layout"]

	func get_entry(entry_id: String) -> Dictionary:
		return entry if entry_id == "thought-layout" else {}


class ViewportRenderer:
	var root: Control

	func attach(_mounts: VBoxContainer) -> void:
		pass

	func mounted_entry_ids() -> Array:
		return ["thought-layout"]

	func mounted_root(entry_id: String) -> Control:
		return root if entry_id == "thought-layout" else null


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _init() -> void:
	_run_store_regression_fixture()
	_run_equal_text_and_streaming_tests()
	_run_selection_preservation_test()
	_run_oversized_budget_tests()
	_run_thought_tests()
	_run_malformed_markdown_test()
	_run_markdown_single_pass_test()
	_run_completion_no_rebuild_test()
	_run_thought_streaming_no_rebuild_test()
	_run_approval_tests()
	_run_tool_tests()
	_run_error_and_status_tests()
	_run_transient_host_tests()
	_run_viewport_layout_tests()
	_run_kind_guard_tests()
	print("transcript rendering checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


# ─── 环境 ────────────────────────────────────────────────────────────────────


func _make_renderer(budget := 0) -> Dictionary:
	var list := VBoxContainer.new()
	var log_renderer := LogEntryRenderer.new()
	log_renderer.theme_colors = {}
	var renderer := TranscriptRenderer.new()
	renderer.log_renderer = log_renderer
	renderer.theme_colors = {}
	if budget > 0:
		renderer.display_budget_chars = budget
	renderer.attach(list)
	return {"renderer": renderer, "list": list}


func _entry(id: String, ordinal: int, kind: String, state: String, revision: int, payload: Dictionary) -> Dictionary:
	return {
		"entry_id": id, "ordinal": ordinal, "kind": kind, "state": state,
		"revision": revision, "turn_id": "t1", "tool_call_id": null, "payload": payload,
	}


func _find_descendant_rich(node: Node) -> RichTextLabel:
	for child in node.get_children():
		if child is RichTextLabel:
			return child
		var found := _find_descendant_rich(child)
		if found != null:
			return found
	return null


func _find_buttons(node: Node) -> Array:
	var result: Array = []
	for child in node.get_children():
		if child is Button:
			result.append(child)
		result.append_array(_find_buttons(child))
	return result


func _find_first_button(node: Node) -> Button:
	var buttons := _find_buttons(node)
	return buttons[0] if not buttons.is_empty() else null


func _find_button_by_text(node: Node, text: String) -> Button:
	for button_value in _find_buttons(node):
		var button: Button = button_value
		if button.text.contains(text):
			return button
	return null


func _find_label_containing(node: Node, text: String) -> Label:
	for child in node.get_children():
		if child is Label and str(child.text).contains(text):
			return child
		var found := _find_label_containing(child, text)
		if found != null:
			return found
	return null


func _has_panel(node: Node) -> bool:
	for child in node.get_children():
		if child is PanelContainer:
			return true
		if _has_panel(child):
			return true
	return false


# ─── Store 级：终态回退拒绝（高 revision 也不行）─────────────────────────────


func _run_store_regression_fixture() -> void:
	var path := _fixture_path("thinking_regression_after_complete.json")
	_check(FileAccess.file_exists(path), "fixture exists: thinking_regression_after_complete")
	if not FileAccess.file_exists(path):
		return
	var parsed = JSON.parse_string(FileAccess.get_file_as_string(path))
	_check(parsed is Dictionary, "fixture parses")
	if not (parsed is Dictionary):
		return
	var fixture: Dictionary = parsed
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := projector.begin_hydration("s1")
	projector.apply_snapshot({
		"transcript": {"version": 1, "session_id": "s1", "upto_event_seq": 0, "legacy": false, "entries": []}
	}, generation)
	var steps: Array = fixture.get("steps", []) if fixture.get("steps", []) is Array else []
	for step_value in steps:
		if not (step_value is Dictionary):
			continue
		var step: Dictionary = step_value
		var entry: Dictionary = step.get("entry", {}) if step.get("entry", {}) is Dictionary else {}
		var envelope := {
			"event_id": str(step.get("event_id", "")),
			"session_id": "s1",
			"seq": int(step.get("seq", 0)),
			"type": "transcript_patch",
			"payload": {"entry": entry, "stream_key": str(entry.get("entry_id", ""))},
		}
		var applied := projector.apply_event(envelope, generation)
		var expected := bool(step.get("expect_applied", true))
		_check(applied == expected, "store regression fixture: %s applied=%s expected=%s" % [str(step.get("event_id", "")), str(applied), str(expected)])
	var thought: Dictionary = store.get_entry("e2")
	_check(str(thought.get("state", "")) == "complete", "store: thought stays complete after thinking regression attempt")
	_check(int(thought.get("revision", 0)) == 2, "store: thought revision unchanged")
	var payload: Dictionary = thought.get("payload", {}) if thought.get("payload", {}) is Dictionary else {}
	_check(absf(float(payload.get("duration_seconds", 0.0)) - 1.75) < 0.001, "store: thought duration preserved")


func _fixture_path(file_name: String) -> String:
	var project_root := ProjectSettings.globalize_path("res://")
	var candidate := project_root.path_join("../ai_agent_service/tests/fixtures/transcript/" + file_name)
	if FileAccess.file_exists(candidate):
		return candidate
	var fallback := "D:/godot-master/ai_agent_service/tests/fixtures/transcript/" + file_name
	if FileAccess.file_exists(fallback):
		return fallback
	return candidate


# ─── 相同正文双条目 + 流式原地更新 ──────────────────────────────────────────


func _run_equal_text_and_streaming_tests() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]

	# 两条相同正文的助手条目必须各自成为独立可寻址控件，按 ordinal 排列。
	renderer.apply_entry(_entry("a1", 0, "assistant", "complete", 1, {"text": "DONE"}))
	renderer.apply_entry(_entry("a2", 1, "assistant", "complete", 1, {"text": "DONE"}))
	_check(list.get_child_count() == 2, "equal text: two independent controls")
	_check(renderer.entry_id_for_node(list.get_child(0)) == "a1", "equal text: ordinal order [0]")
	_check(renderer.entry_id_for_node(list.get_child(1)) == "a2", "equal text: ordinal order [1]")

	# 流式更新：同一 entry 只更新同一个根控件，不追加第二块。
	var env2 := _make_renderer()
	var renderer2: RefCounted = env2["renderer"]
	var list2: VBoxContainer = env2["list"]
	renderer2.apply_entry(_entry("s1", 0, "assistant", "streaming", 1, {"text": "你好"}))
	renderer2.apply_entry(_entry("s1", 0, "assistant", "streaming", 2, {"text": "你好，世界"}))
	renderer2.apply_entry(_entry("s1", 0, "assistant", "complete", 3, {"text": "你好，世界！"}))
	_check(list2.get_child_count() == 1, "streaming: one mounted control")
	var rich := _find_descendant_rich(list2.get_child(0))
	_check(rich != null and rich.get_parsed_text().contains("你好，世界！"), "streaming: final markdown body shown")
	_check(rich != null and not rich.get_parsed_text().contains("▍"), "streaming: cursor removed on complete")
	# 不高于已接受修订的更新保持控件不变。
	var changed: bool = renderer2.apply_entry(_entry("s1", 0, "assistant", "streaming", 2, {"text": "旧文本"}))
	_check(not changed, "stale revision: control unchanged")
	_check(rich != null and rich.get_parsed_text().contains("你好，世界！"), "stale revision: content unchanged")


func _run_selection_preservation_test() -> void:
	# 无头环境无法创建真实选区；这里验证选区保留策略的结构契约：
	# 纯追加的流式更新不重建富文本控件（引擎内已有选区不被触碰），
	# 内容被替换时重建控件（旧选区随之显式清除，不会指向旧文本）。
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	renderer.apply_entry(_entry("sel1", 0, "assistant", "complete", 1, {"text": "abcdef"}))
	var rich := _find_descendant_rich(list.get_child(0))
	_check(rich != null, "selection: rich exists")
	if rich == null:
		return
	renderer.apply_entry(_entry("sel1", 0, "assistant", "complete", 2, {"text": "abcdefXYZ"}))
	var rich2 := _find_descendant_rich(list.get_child(0))
	_check(rich2 == rich, "selection: append update keeps the same rich control (selection untouched)")
	_check(rich2 != null and rich2.get_parsed_text() == "abcdefXYZ", "selection: appended content is complete")
	renderer.apply_entry(_entry("sel1", 0, "assistant", "complete", 3, {"text": "全新内容"}))
	var rich3 := _find_descendant_rich(list.get_child(0))
	_check(rich3 != null and rich3 != rich2, "selection: replaced content rebuilds control (stale selection cleared)")
	_check(rich3 != null and rich3.get_selected_text() == "", "selection: rebuilt control has no selection")


# ─── 单条展示预算：预览/显示完整/复制/驱逐/重挂载 ────────────────────────────


func _run_oversized_budget_tests() -> void:
	var long_text := ""
	for index in range(250):
		long_text += "AB"
	# long_text = 500 chars

	# assistant：预览挂载 → 复制仍取完整 → 点击显示完整 → 驱逐后重挂载回到预览。
	var env := _make_renderer(100)
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	var entry := _entry("big1", 0, "assistant", "complete", 1, {"text": long_text})
	renderer.apply_entry(entry)
	var root := list.get_child(0)
	var rich := _find_descendant_rich(root)
	_check(rich != null and rich.get_parsed_text().length() < long_text.length(), "oversized assistant: initial preview only")
	_check(_find_label_containing(root, "内容过长") != null, "oversized assistant: preview note shown")
	var show_btn := _find_button_by_text(root, "显示完整内容")
	_check(show_btn != null, "oversized assistant: display-complete action present")
	_check(renderer.copy_text_for_node(rich) == long_text, "oversized assistant: copy returns complete canonical text")
	if show_btn != null:
		show_btn.pressed.emit()
	var rich_full := _find_descendant_rich(root)
	_check(rich_full != null and rich_full.get_parsed_text().length() == long_text.length(), "oversized assistant: full content rendered after explicit action")
	renderer.forget_entry("big1")
	renderer.apply_entry(entry)
	var root2 := list.get_child(0)
	var rich2 := _find_descendant_rich(root2)
	_check(rich2 != null and rich2.get_parsed_text().length() < long_text.length(), "oversized assistant: remount returns to preview")
	_check(renderer.copy_text_for_node(rich2) == long_text, "oversized assistant: copy complete after remount")

	# user：同样的预算规则。
	var env_user := _make_renderer(100)
	var renderer_user: RefCounted = env_user["renderer"]
	var list_user: VBoxContainer = env_user["list"]
	renderer_user.apply_entry(_entry("big2", 0, "user", "complete", 1, {"text": long_text}))
	var user_rich := _find_descendant_rich(list_user.get_child(0))
	_check(user_rich != null and user_rich.get_parsed_text().length() < long_text.length(), "oversized user: preview only")
	_check(renderer_user.copy_text_for_node(user_rich) == long_text, "oversized user: copy complete")

	# thought 展开内容：预览 → 显示完整；复制恒为完整持久化内容。
	var env_th := _make_renderer(100)
	var renderer_th: RefCounted = env_th["renderer"]
	var list_th: VBoxContainer = env_th["list"]
	var thought_entry := _entry("big3", 0, "thought", "complete", 1, {"content": long_text, "token_count": 40, "duration_seconds": 2.0})
	renderer_th.apply_entry(thought_entry)
	var th_root := list_th.get_child(0)
	var toggle := _find_first_button(th_root)
	_check(toggle != null and toggle.text.contains("Thought for 2.00s"), "oversized thought: completed summary")
	if toggle != null:
		toggle.pressed.emit()
	var th_detail := _find_descendant_rich(th_root)
	_check(th_detail != null and th_detail.get_parsed_text().length() < long_text.length(), "oversized thought: expanded detail mounts preview")
	var th_show := _find_button_by_text(th_root, "显示完整内容")
	_check(th_show != null, "oversized thought: display-complete action present")
	_check(renderer_th.copy_text_for_node(_find_descendant_rich(th_root)) == long_text, "oversized thought: canonical copy returns complete persisted content")
	if th_show != null:
		th_show.pressed.emit()
	var th_detail_full := _find_descendant_rich(th_root)
	_check(th_detail_full != null and th_detail_full.get_parsed_text().length() == long_text.length(), "oversized thought: full detail after explicit action")

	# 工具原始详情：同样采用延迟完整渲染规则。
	var env_tool := _make_renderer(100)
	var renderer_tool: RefCounted = env_tool["renderer"]
	var list_tool: VBoxContainer = env_tool["list"]
	var tool_entry := _entry("big4", 0, "tool_activity", "resolved", 1, {
		"tool": "run_system_command", "args": {"command": "ls"}, "agent": "",
		"is_error": false, "result_summary": {"text": long_text}, "result_count": 1, "render_kind": null,
	})
	renderer_tool.apply_entry(tool_entry)
	var tool_root := list_tool.get_child(0)
	var detail_toggle := _find_button_by_text(tool_root, "详情")
	_check(detail_toggle != null, "oversized tool: details toggle present")
	if detail_toggle != null:
		detail_toggle.pressed.emit()
	var tool_richs := _find_all_rich(tool_root)
	var tool_detail_rich: RichTextLabel = tool_richs[tool_richs.size() - 1] if not tool_richs.is_empty() else null
	_check(tool_detail_rich != null and tool_detail_rich.get_parsed_text().length() < long_text.length(), "oversized tool: raw detail preview only")
	var tool_show := _find_button_by_text(tool_root, "显示完整内容")
	_check(tool_show != null, "oversized tool: display-complete action present")
	if tool_show != null:
		tool_show.pressed.emit()
	var tool_richs_full := _find_all_rich(tool_root)
	var tool_detail_full: RichTextLabel = tool_richs_full[tool_richs_full.size() - 1] if not tool_richs_full.is_empty() else null
	_check(tool_detail_full != null and tool_detail_full.get_parsed_text().length() >= long_text.length(), "oversized tool: full raw detail after explicit action")


# ─── Thought：摘要/展开/预算边界/终态/复制 ───────────────────────────────────


func _run_thought_tests() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	# 思考中：token 摘要；预算边界（1024）不产生新状态。
	renderer.apply_entry(_entry("t1", 0, "thought", "thinking", 1, {"content": "先看参数", "token_count": 1024, "duration_seconds": null}))
	var root := list.get_child(0)
	var toggle := _find_first_button(root)
	_check(toggle != null and toggle.text.contains("Thinking 1,024 Tokens"), "thought: budget boundary keeps thinking summary (got %s)" % (toggle.text if toggle != null else "<null>"))

	# 展开后完成：同一控件保持展开并切换为耗时摘要。
	if toggle != null:
		toggle.pressed.emit()
	var detail := _find_descendant_rich(root)
	_check(detail != null and detail.visible, "thought: expanded")
	_check(detail != null and detail.get_parent() != null and (detail.get_parent() as Control).visible, "thought: detail container visible when expanded")
	renderer.apply_entry(_entry("t1", 0, "thought", "complete", 2, {"content": "先看参数，再调重力", "token_count": 1024, "duration_seconds": 3.25}))
	_check(list.get_child_count() == 1, "thought: completion updates same control")
	var toggle2 := _find_first_button(root)
	var detail2 := _find_descendant_rich(root)
	_check(toggle2 != null and toggle2.text.contains("Thought for 3.25s"), "thought: completed summary after budget boundary")
	_check(detail2 != null and detail2.visible, "thought: expansion preserved across completion")
	_check(detail2 != null and detail2.get_parsed_text().contains("再调重力"), "thought: final persisted content shown")

	# 终态单向：更晚修订的 thinking 补丁必须被渲染层拒绝。
	var regressed: bool = renderer.apply_entry(_entry("t1", 0, "thought", "thinking", 9, {"content": "回退", "token_count": 1, "duration_seconds": null}))
	_check(not regressed, "thought: thinking patch after complete rejected")
	var toggle3 := _find_first_button(root)
	_check(toggle3 != null and toggle3.text.contains("Thought for 3.25s"), "thought: summary unchanged after rejected regression")

	# 复制与普通正文一致：规范复制适配器从持久化条目取内容（选中复制/复制全文同源）。
	var canonical: String = renderer.copy_text_for_node(_find_descendant_rich(root))
	_check(canonical == "先看参数，再调重力", "thought: canonical copy is persisted content (got %s)" % canonical)
	_check(not canonical.contains("Thought for") and not canonical.contains("Thinking"), "thought: copy excludes summary")
	# Thought 头部只有展开开关，不再有独立复制按钮。
	_check(_find_buttons(root).size() == 1, "thought: header exposes only the expand toggle")
	if toggle3 != null:
		toggle3.pressed.emit()

	# 历史/重挂载：默认折叠，内容完整可展开。
	renderer.render_all([_entry("t1", 0, "thought", "complete", 2, {"content": "先看参数，再调重力", "token_count": 1024, "duration_seconds": 3.25})])
	var hydrated := list.get_child(list.get_child_count() - 1)
	var toggle4 := _find_first_button(hydrated)
	var detail4 := _find_descendant_rich(hydrated)
	_check(toggle4 != null and toggle4.text.contains("Thought for 3.25s"), "thought: hydrated summary")
	_check(detail4 != null and not detail4.visible, "thought: hydrated collapsed by default")
	_check(detail4 != null and detail4.get_parsed_text().contains("先看参数"), "thought: hydrated content preserved")


func _run_malformed_markdown_test() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	var malformed := "# 标题 [未闭合 **粗体 `代码\n```\n未闭合代码块 [bar"
	renderer.apply_entry(_entry("md1", 0, "assistant", "complete", 1, {"text": malformed}))
	var root := list.get_child(0)
	var rich := _find_descendant_rich(root)
	_check(rich != null, "malformed markdown: node built")
	_check(rich != null and rich.get_parsed_text().contains("未闭合代码块"), "malformed markdown: remains readable")
	_check(rich != null and rich.selection_enabled, "malformed markdown: panel stays interactive")
	_check(renderer.copy_text_for_node(rich) == malformed, "malformed markdown: copy returns canonical text")
	var malformed_parsed := "" if rich == null else rich.get_parsed_text()
	_check(not malformed_parsed.contains("[lb]") and not malformed_parsed.contains("[rb]"), "malformed markdown: no escape garbage in rendered text")


# ─── 审批：解决态一行文本 + 补充用户条目 ────────────────────────────────────


func _run_approval_tests() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]

	# pending（可操作）→ 卡片；解决后同一根节点降级为一行文本。
	var pending := _entry("ap1", 0, "approval", "pending", 1, {
		"tool": "apply_text_edit", "args": {"path": "res://player.gd"}, "decision": null,
		"render_kind": "diff", "operation_summary": "修改", "affected_paths": ["res://player.gd"], "resolution_summary": null,
	})
	renderer.apply_entry(pending)
	var root := list.get_child(0)
	_check(_has_panel(root), "approval pending: card rendered")
	var resolved := _entry("ap1", 0, "approval", "approved", 2, {
		"tool": "apply_text_edit", "args": {"path": "res://player.gd"}, "decision": "approved",
		"render_kind": "diff", "operation_summary": "修改", "affected_paths": ["res://player.gd"], "resolution_summary": "已确认",
	})
	renderer.apply_entry(resolved)
	_check(list.get_child_count() == 1, "approval: resolved updates same control")
	_check(not _has_panel(root), "approval resolved: no card chrome")
	_check(_find_buttons(root).is_empty(), "approval resolved: no controls")
	var line_rich := _find_descendant_rich(root)
	var line := "" if line_rich == null else line_rich.get_parsed_text().strip_edges()
	_check(line == "已确认：修改 res://player.gd", "approval resolved: one-line permission result (got %s)" % line)

	# 重载后呈现同样的一行形态。
	var env_reload := _make_renderer()
	var renderer_reload: RefCounted = env_reload["renderer"]
	var list_reload: VBoxContainer = env_reload["list"]
	renderer_reload.render_all([resolved])
	var reloaded := list_reload.get_child(0)
	_check(not _has_panel(reloaded) and _find_buttons(reloaded).is_empty(), "approval reload: one-line text node only")
	var reload_rich := _find_descendant_rich(reloaded)
	_check(reload_rich != null and reload_rich.get_parsed_text().strip_edges() == "已确认：修改 res://player.gd", "approval reload: same one-line text")

	# 旧条目（缺少新字段）：仅回退到持久化 typed 字段，仍不得猜测。
	var legacy := _entry("ap2", 1, "approval", "rejected", 2, {
		"tool": "apply_text_edit", "args": {"path": "scripts/legacy.gd"}, "decision": "rejected", "render_kind": "diff",
	})
	var env_legacy := _make_renderer()
	var renderer_legacy: RefCounted = env_legacy["renderer"]
	var list_legacy: VBoxContainer = env_legacy["list"]
	renderer_legacy.render_all([legacy])
	var legacy_rich := _find_descendant_rich(list_legacy.get_child(0))
	var legacy_line := "" if legacy_rich == null else legacy_rich.get_parsed_text().strip_edges()
	_check(legacy_line.contains("已拒绝") and legacy_line.contains("scripts/legacy.gd"), "approval legacy: typed fallback line (got %s)" % legacy_line)
	var missing := _entry("ap3", 2, "approval", "approved", 2, {"decision": "approved"})
	renderer_legacy.apply_entry(missing)
	var missing_rich := _find_descendant_rich(list_legacy.get_child(1))
	var missing_line := "" if missing_rich == null else missing_rich.get_parsed_text().strip_edges()
	_check(missing_line.contains("未提供"), "approval missing fields: labelled unavailable (got %s)" % missing_line)

	# 用户补充输入是独立 kind=user 条目，绝不并入权限结果行。
	var env_supp := _make_renderer()
	var renderer_supp: RefCounted = env_supp["renderer"]
	var list_supp: VBoxContainer = env_supp["list"]
	renderer_supp.render_all([
		resolved,
		_entry("u9", 1, "user", "complete", 1, {"text": "speed = 300", "client_message_id": "m9", "has_context": false}),
	])
	_check(list_supp.get_child_count() == 2, "supplemental input: separate user entry")
	var approval_rich := _find_descendant_rich(list_supp.get_child(0))
	var user_rich := _find_descendant_rich(list_supp.get_child(1))
	_check(approval_rich != null and not approval_rich.get_parsed_text().contains("speed = 300"), "supplemental input: not merged into permission line")
	_check(user_rich != null and user_rich.get_parsed_text().contains("speed = 300"), "supplemental input: rendered as user message after approval")


# ─── 工具：紧凑状态卡 + 部分失败警示 ────────────────────────────────────────


func _run_tool_tests() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]

	# 紧凑结果卡：头部 + 一行状态；小结果不出现详情折叠。
	renderer.apply_entry(_entry("tl1", 0, "tool_activity", "running", 1, {
		"tool": "read_file", "args": {"path": "scripts/player.gd"}, "agent": "",
		"is_error": false, "result_summary": null, "result_count": null, "render_kind": null,
	}))
	var root := list.get_child(0)
	var running_rich := _find_descendant_rich(root)
	_check(running_rich != null and running_rich.get_parsed_text().contains("…"), "tool running: in-progress marker")
	renderer.apply_entry(_entry("tl1", 0, "tool_activity", "resolved", 2, {
		"tool": "read_file", "args": {"path": "scripts/player.gd"}, "agent": "",
		"is_error": false, "result_summary": {"kind": "read", "path": "scripts/player.gd", "line_start": 1, "line_end": 41},
		"result_count": 1, "render_kind": null,
	}))
	_check(list.get_child_count() == 1, "tool: resolved updates same control (no duplicate card)")
	var resolved_rich := _find_descendant_rich(root)
	_check(resolved_rich != null and resolved_rich.get_parsed_text().contains("player.gd"), "tool resolved: compact header")

	# 回归：grep 命中可能来自 service.log 的完整模型请求。历史水合只能创建
	# 有上限的状态摘要，不能把十几万字符同步交给 RichTextLabel.fit_content。
	var huge_log_line := "model request " + "x".repeat(12000)
	var huge_matches: Array = []
	for index in range(10):
		huge_matches.append({"path": "logs/service.log", "line": index + 1, "text": huge_log_line})
	renderer.apply_entry(_entry("tl_grep_large", 1, "tool_activity", "resolved", 1, {
		"tool": "grep_code", "args": {"pattern": "request"}, "agent": "",
		"is_error": false,
		"result_summary": {
			"kind": "grep", "pattern": "request", "include": "**/*",
			"match_count": 10, "matches": huge_matches, "truncated": false,
		},
		"result_count": 10, "render_kind": null,
	}))
	var grep_text := ""
	for rich_value in _find_all_rich(list.get_child(1)):
		grep_text += (rich_value as RichTextLabel).get_parsed_text() + "\n"
	_check(grep_text.length() < 1200, "tool grep: large history summary is bounded")
	_check(grep_text.contains("more match(es) omitted"), "tool grep: omitted matches are labelled")

	# 部分失败：失败 + 可能已修改 → 警示且不以成功呈现。
	renderer.apply_entry(_entry("tl2", 2, "tool_activity", "failed", 1, {
		"tool": "apply_text_edit", "args": {"path": "scripts/player.gd"}, "agent": "",
		"is_error": true, "result_summary": {"text": "apply failed", "possible_modifications": true},
		"result_count": null, "render_kind": "diff",
	}))
	var failed_root := list.get_child(2)
	var failed_text := ""
	for rich_value in _find_all_rich(failed_root):
		failed_text += (rich_value as RichTextLabel).get_parsed_text() + "\n"
	_check(failed_text.contains("执行失败"), "partial failure: failure shown")
	_check(failed_text.contains("部分文件可能已被修改"), "partial failure: modification warning shown")
	_check(not failed_text.contains("✓"), "partial failure: not presented as success")


func _find_all_rich(node: Node) -> Array:
	var result: Array = []
	for child in node.get_children():
		if child is RichTextLabel:
			result.append(child)
		result.append_array(_find_all_rich(child))
	return result


# ─── 错误与状态条目 ─────────────────────────────────────────────────────────


func _run_error_and_status_tests() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]

	# 错误：上下文 + 原因 + 修改状态；未声明可重试则不出现重试按钮。
	renderer.apply_entry(_entry("er1", 0, "error", "complete", 1, {
		"text": "写入失败", "context": "修改 res://player.gd", "modification_status": "部分文件可能已被修改", "retryable": false,
	}))
	var error_root := list.get_child(0)
	var error_text := ""
	for rich_value in _find_all_rich(error_root):
		error_text += (rich_value as RichTextLabel).get_parsed_text() + "\n"
	_check(error_text.contains("修改 res://player.gd"), "error: operation context shown")
	_check(error_text.contains("写入失败"), "error: readable reason shown")
	_check(error_text.contains("部分文件可能已被修改"), "error: modification status shown")
	_check(_find_buttons(error_root).is_empty(), "error: no retry unless payload declares retryable")

	# payload 明确声明可重试时才出现重试按钮。
	renderer.apply_entry(_entry("er2", 1, "error", "complete", 1, {"text": "超时", "retryable": true}))
	var retry_btn := _find_button_by_text(list.get_child(1), "重试")
	_check(retry_btn != null, "error: retry shown when payload declares retryable")

	# 进度/校验/计划：仅消费 typed 字段。
	renderer.apply_entry(_entry("pg1", 2, "progress", "running", 1, {"step_index": 1, "total_steps": 3, "title": "读取脚本", "summary": null}))
	var progress_text := _find_descendant_rich(list.get_child(2)).get_parsed_text()
	_check(progress_text.contains("Step 1/3") and progress_text.contains("读取脚本"), "progress: typed fields rendered")
	renderer.apply_entry(_entry("vf1", 3, "verification", "passed", 1, {"file_path": "res://a.gd", "phase": "syntax", "issues_count": 0, "summary": "无问题"}))
	var verify_text := _find_descendant_rich(list.get_child(3)).get_parsed_text()
	_check(verify_text.contains("Verify passed") and verify_text.contains("无问题"), "verification: typed fields rendered")
	renderer.apply_entry(_entry("pl1", 4, "plan", "complete", 1, {"summary": "两步", "steps": [{"title": "读取", "status": "pending"}]}))
	var plan_text := _find_descendant_rich(list.get_child(4)).get_parsed_text()
	_check(plan_text.contains("Plan created") and plan_text.contains("读取"), "plan: typed fields rendered")


# ─── 瞬时提示：可丢弃、不重挂载 ─────────────────────────────────────────────


func _run_transient_host_tests() -> void:
	var container := VBoxContainer.new()
	var log_renderer := LogEntryRenderer.new()
	log_renderer.theme_colors = {}
	var host := TransientNoticeHost.new()
	host.node_factory = log_renderer
	host.attach(container)

	host.show_keyed("waiting", "等待模型响应...", "system")
	_check(container.get_child_count() == 1, "transient: waiting notice mounted")
	host.show_keyed("waiting", "等待模型响应...（仍在等待）", "system")
	_check(container.get_child_count() == 1, "transient: keyed notice replaces previous")

	# 水合替换：提示被直接丢弃，只有 typed 条目被渲染。
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	host.clear_all()
	renderer.render_all([
		_entry("u1", 0, "user", "complete", 1, {"text": "你好"}),
		_entry("a1", 1, "assistant", "complete", 1, {"text": "你好！"}),
	])
	_check(container.get_child_count() == 0, "transient: discarded on hydration")
	_check(list.get_child_count() == 2, "transient: only typed entries rendered from snapshot")

	# 请求完成：带键提示被丢弃且不再重挂载。
	host.show_keyed("command", "正在执行命令 /compact …", "system")
	host.discard_keyed("command")
	_check(container.get_child_count() == 0, "transient: discarded on completion")

	# 提示与 durable 条目共用一条时间线：先出现的本地提示不能在后续历史
	# 水合时被独立的尾置容器排到所有消息之后。
	var timeline := VBoxContainer.new()
	var timeline_host := TransientNoticeHost.new()
	timeline_host.node_factory = log_renderer
	timeline_host.attach(timeline)
	timeline_host.show_notice("（无历史记录）", "system")
	var timeline_renderer := TranscriptRenderer.new()
	timeline_renderer.log_renderer = log_renderer
	timeline_renderer.theme_colors = {}
	timeline_renderer.attach(timeline)
	timeline_renderer.render_all([_entry("later", 1, "assistant", "complete", 1, {"text": "后续历史"})])
	var first_timeline_id := str(timeline.get_child(0).get_meta("transcript_entry_id")) if timeline.get_child(0).has_meta("transcript_entry_id") else ""
	var second_timeline_id := str(timeline.get_child(1).get_meta("transcript_entry_id")) if timeline.get_child(1).has_meta("transcript_entry_id") else ""
	_check(timeline.get_child_count() == 2 and first_timeline_id == "", "transient: pre-hydration notice keeps its timeline position")
	_check(second_timeline_id == "later", "transient: later durable entry follows notice")

	var waiting_timeline := VBoxContainer.new()
	var waiting_renderer := TranscriptRenderer.new()
	waiting_renderer.log_renderer = log_renderer
	waiting_renderer.theme_colors = {}
	waiting_renderer.attach(waiting_timeline)
	waiting_renderer.apply_entry(_entry("optimistic", -1, "user", "complete", 1, {"text": "你好"}))
	var waiting_host := TransientNoticeHost.new()
	waiting_host.node_factory = log_renderer
	waiting_host.attach(waiting_timeline)
	waiting_host.show_keyed("waiting", "等待模型响应...", "system")
	var optimistic_id := str(waiting_timeline.get_child(0).get_meta("transcript_entry_id")) if waiting_timeline.get_child(0).has_meta("transcript_entry_id") else ""
	_check(waiting_timeline.get_child_count() == 2 and optimistic_id == "optimistic", "transient: waiting follows its optimistic user entry")


# ─── 虚拟视口：稳定测量 + transient 挂载位置 ───────────────────────────────


func _run_viewport_layout_tests() -> void:
	var list := VBoxContainer.new()
	var transient_mount := VBoxContainer.new()
	var root := StableMinimumRoot.new()
	# 模拟 RichTextLabel.fit_content 的首帧旧尺寸；它不能写入虚拟 spacer 缓存。
	root.size = Vector2(640, 14080)
	var renderer := ViewportRenderer.new()
	renderer.root = root
	var store := ViewportStore.new()
	var viewport := TranscriptViewport.new()
	viewport.attach(list, renderer, store, ScrollContainer.new(), transient_mount)
	viewport._measure_mounted()
	var key := viewport._height_key(store.entry)
	_check(absf(float(viewport._measurements.get(key, 0.0)) - 28.0) < 0.01, "viewport: caches stable minimum height instead of stale fit-content size")
	_check(list.get_child_count() == 4, "viewport: spacer, mounts, transient mount, spacer installed")
	_check(list.get_child(2) == transient_mount, "viewport: transient mount precedes bottom spacer")
	var host := TransientNoticeHost.new()
	host.node_factory = LogEntryRenderer.new()
	host.attach(transient_mount)
	host.show_notice("显示错误", "error")
	_check(transient_mount.get_child_count() == 1 and list.get_child(3) != transient_mount, "viewport: error notice is not appended after bottom spacer")


# ─── kind 守卫：缺少/未知 kind 一律拒绝 ─────────────────────────────────────


func _run_kind_guard_tests() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	var no_kind: bool = renderer.apply_entry({"entry_id": "x1", "ordinal": 0, "revision": 1, "payload": {"text": "Thought: 伪装"}})
	_check(not no_kind and list.get_child_count() == 0, "kind guard: missing kind rejected")
	var unknown_kind: bool = renderer.apply_entry(_entry("x2", 0, "bogus", "complete", 1, {"text": "Thought: 伪装"}))
	_check(not unknown_kind and list.get_child_count() == 0, "kind guard: unknown kind rejected")
	# Thought 卡片只能来自 typed kind=thought：文本中的 Thought 前缀不产生卡片。
	renderer.apply_entry(_entry("x3", 0, "assistant", "complete", 1, {"text": "Thought: 这不是思考条目"}))
	var root := list.get_child(0)
	_check(_find_button_by_text(root, "复制") == null and _find_first_button(root) == null, "kind guard: assistant text never becomes Thought card")

# ─── Markdown 单次转换：流式与完成态一致，无二次转义乱码 ─────────────────────


func _run_markdown_single_pass_test() -> void:
	# 双重转换回归：Markdown→BBCode 只能发生一次（在 make_rich_text 内部）。
	# 二次转换会把 BBCode 当纯文本转义，显示 "[lb]b]" 之类乱码
	# （症状：流式正常，完成态整条重建后出现乱码）。
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	var md_text := "# 标题\n**加粗** 和 `code`\n```\nvar x = arr[0]\n```"
	renderer.apply_entry(_entry("mds1", 0, "assistant", "complete", 1, {"text": md_text}))
	var rich := _find_descendant_rich(list.get_child(0))
	var parsed := "" if rich == null else rich.get_parsed_text()
	_check(rich != null and parsed.contains("加粗"), "single pass: readable content rendered")
	_check(not parsed.contains("[lb]") and not parsed.contains("[rb]"), "single pass: no [lb]/[rb] garbage")
	_check(not parsed.contains("[b]") and not parsed.contains("[/b]"), "single pass: no literal bbcode tags")
	_check(parsed.contains("arr[0]"), "single pass: brackets in code preserved")

	# 流式追加路径与完成态整条重建必须渲染出相同文本（不截断内联语法时）。
	var env2 := _make_renderer()
	var renderer2: RefCounted = env2["renderer"]
	var list2: VBoxContainer = env2["list"]
	renderer2.apply_entry(_entry("mds2", 0, "assistant", "streaming", 1, {"text": "**加粗**"}))
	renderer2.apply_entry(_entry("mds2", 0, "assistant", "streaming", 2, {"text": "**加粗** 完成"}))
	var stream_rich := _find_descendant_rich(list2.get_child(0))
	var stream_parsed := "" if stream_rich == null else stream_rich.get_parsed_text()
	renderer2.apply_entry(_entry("mds2", 0, "assistant", "complete", 3, {"text": "**加粗** 完成"}))
	var final_rich := _find_descendant_rich(list2.get_child(0))
	var final_parsed := "" if final_rich == null else final_rich.get_parsed_text()
	_check(stream_parsed == final_parsed, "single pass: streaming and complete render identically (got %s vs %s)" % [stream_parsed, final_parsed])
	_check(not final_parsed.contains("[lb]"), "single pass: completion rebuild adds no garbage")

# ─── 完成不重建：无闪烁 + 边界自愈 ───────────────────────────────────────────


func _run_completion_no_rebuild_test() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]

	# 内容不变的完成修订：同一富文本控件保留（不重建、不闪烁），光标摘除。
	renderer.apply_entry(_entry("nr1", 0, "assistant", "streaming", 1, {"text": "**加粗**"}))
	renderer.apply_entry(_entry("nr1", 0, "assistant", "streaming", 2, {"text": "**加粗** 完成"}))
	var rich_before := _find_descendant_rich(list.get_child(0))
	_check(rich_before != null and rich_before.get_parsed_text() == "加粗 完成", "no rebuild: streamed content correct")
	renderer.apply_entry(_entry("nr1", 0, "assistant", "complete", 3, {"text": "**加粗** 完成"}))
	var rich_after := _find_descendant_rich(list.get_child(0))
	_check(rich_after == rich_before, "no rebuild: completion keeps the same rich control (no flicker)")
	_check(rich_after != null and rich_after.get_parsed_text() == "加粗 完成", "no rebuild: content unchanged")
	var cursor := _find_label_containing(list.get_child(0), "▍")
	_check(cursor != null and not cursor.visible, "no rebuild: streaming cursor hidden on complete")

	# 分块边界切断 **粗体**：流式期显示字面星号，完成比对后一次性自愈。
	renderer.apply_entry(_entry("nr2", 1, "assistant", "streaming", 1, {"text": "**加"}))
	renderer.apply_entry(_entry("nr2", 1, "assistant", "streaming", 2, {"text": "**加粗**"}))
	var heal_before := _find_descendant_rich(list.get_child(1))
	_check(heal_before != null and heal_before.get_parsed_text().contains("**"), "heal: streaming shows literal asterisks")
	renderer.apply_entry(_entry("nr2", 1, "assistant", "complete", 3, {"text": "**加粗**"}))
	var heal_after := _find_descendant_rich(list.get_child(1))
	var heal_parsed := "" if heal_after == null else heal_after.get_parsed_text()
	_check(heal_parsed == "加粗", "heal: completion rebuild fixes boundary artifacts (got %s)" % heal_parsed)


func _run_thought_streaming_no_rebuild_test() -> void:
	var env := _make_renderer()
	var renderer: RefCounted = env["renderer"]
	var list: VBoxContainer = env["list"]
	renderer.apply_entry(_entry("tnr", 0, "thought", "thinking", 1, {"content": "先看", "token_count": 3, "duration_seconds": null}))
	var root := list.get_child(0)
	var toggle := _find_first_button(root)
	if toggle != null:
		toggle.pressed.emit()
	var detail_before := _find_descendant_rich(root)
	_check(detail_before != null and detail_before.get_parsed_text().contains("先看"), "thought streaming: initial detail rendered")

	# 思考流式更新：原地追加，不重建控件（展开浏览时不闪烁）。
	renderer.apply_entry(_entry("tnr", 0, "thought", "thinking", 2, {"content": "先看参数", "token_count": 6, "duration_seconds": null}))
	var detail_mid := _find_descendant_rich(root)
	_check(detail_mid == detail_before, "thought streaming: detail updated in place (no rebuild)")
	_check(detail_mid != null and detail_mid.get_parsed_text().contains("先看参数"), "thought streaming: content appended")

	# 内容不变的完成修订：同样不重建。
	renderer.apply_entry(_entry("tnr", 0, "thought", "complete", 3, {"content": "先看参数", "token_count": 6, "duration_seconds": 1.5}))
	var detail_after := _find_descendant_rich(root)
	_check(detail_after == detail_before, "thought completion: no rebuild when content unchanged")
	_check(detail_after != null and detail_after.visible, "thought completion: expansion preserved")
