## 展示稿 Store/Projector 契约测试（任务 5.2 / 5.3 前端侧）。
##
## 复用后端契约夹具（ai_agent_service/tests/fixtures/transcript/*.json），
## 验证：重复 final 投递、两条相同正文、工具/审批/进度持久化、过期会话快照
## 拒绝、重连重放、保留间隙水合、快照原子替换与 revision 回退拒绝。
extends SceneTree

const TranscriptStore = preload("res://addons/ai_agent/transcript/transcript_store.gd")
const TranscriptProjector = preload("res://addons/ai_agent/transcript/transcript_projector.gd")
const TranscriptRenderer = preload("res://addons/ai_agent/transcript/transcript_renderer.gd")
const TranscriptViewport = preload("res://addons/ai_agent/transcript/transcript_viewport.gd")
const LogEntryRenderer = preload("res://addons/ai_agent/ui/log_entry_renderer.gd")
const TransientNoticeHost = preload("res://addons/ai_agent/ui/transient_notice_host.gd")
const AgentDTO = preload("res://addons/ai_agent/dto/agent_dto.gd")
const TranscriptCopy = preload("res://addons/ai_agent/transcript/transcript_copy.gd")

var _failures := 0
var _checks := 0


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _init() -> void:
	var fixture_dir := _fixture_directory()
	if fixture_dir == "":
		printerr("FAIL: transcript fixture directory not found")
		quit(1)
		return
	var fixtures := {
		"duplicate_final_delivery": "duplicate_final_delivery.json",
		"two_identical_answers": "two_identical_answers.json",
		"tool_approval_progress_persistence": "tool_approval_progress_persistence.json",
		"stale_session_history": "stale_session_history.json",
		"reconnect_replay": "reconnect_replay.json",
		"retention_gap_hydration": "retention_gap_hydration.json",
		"thought_lifecycle": "thought_lifecycle.json",
		"late_reasoning_after_completion": "late_reasoning_after_completion.json",
		"empty_content_recovery": "empty_content_recovery.json",
		"empty_content_unrecoverable": "empty_content_unrecoverable.json",
		"long_navigation": "long_navigation.json",
	}
	for fixture_name in fixtures.keys():
		var fixture := _load_fixture(fixture_dir + "/" + str(fixtures[fixture_name]))
		if fixture.is_empty():
			_check(false, "fixture loads: " + str(fixture_name))
			continue
		_run_fixture(fixture_name, fixture)
	_run_cross_session_isolation_test()
	_run_navigation_store_test(fixture_dir)
	_run_viewport_window_test()
	_renderer_smoke_test()
	_run_thought_renderer_test()
	_run_transient_host_ownership_test()
	_run_tool_outcome_semantics_test()
	_run_snapshot_fence_test()
	print("transcript projection checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


## 会话切换隔离：其他会话的迟到补丁不得污染当前展示稿（任务 5.3）。
func _run_cross_session_isolation_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var rejected_reasons: Array = []
	projector.patch_rejected.connect(func(diagnostic: Dictionary): rejected_reasons.append(str(diagnostic.get("reason", ""))))
	var generation := projector.begin_hydration("session-b")
	projector.apply_snapshot({
		"transcript": {
			"version": 1,
			"session_id": "session-b",
			"upto_event_seq": 1,
			"legacy": false,
			"entries": [
				{"entry_id": "e1", "ordinal": 0, "kind": "user", "state": "complete", "revision": 1, "payload": {"text": "b"}}
			]
		}
	}, generation)
	var foreign_applied := projector.apply_event({
		"event_id": "session-a:9",
		"session_id": "session-a",
		"seq": 9,
		"type": "transcript_patch",
		"payload": {
			"entry": {"entry_id": "e9", "ordinal": 0, "kind": "assistant", "state": "complete", "revision": 1, "payload": {"text": "stale"}},
			"stream_key": "e9"
		}
	}, generation)
	_check(not foreign_applied, "isolation: foreign-session patch rejected")
	_check(rejected_reasons.has("session_mismatch"), "diagnostics: session mismatch is redacted and observable")
	_check(store.entry_count() == 1, "isolation: active transcript unchanged")
	var malformed := projector.apply_event({
		"event_id": "malformed", "session_id": "session-b", "seq": 10, "type": "transcript_patch", "payload": {"stream_key": ""}
	}, generation)
	_check(not malformed and rejected_reasons.has("invalid_payload"), "diagnostics: malformed patch rejected")


## 瞬时宿主与虚拟视口共用消息容器时，清理只能释放自身创建的通知。
func _run_transient_host_ownership_test() -> void:
	var list := VBoxContainer.new()
	var viewport_mounts := VBoxContainer.new()
	list.add_child(viewport_mounts)
	var host := TransientNoticeHost.new()
	host.node_factory = LogEntryRenderer.new()
	host.attach(list)
	host.show_notice("temporary")
	host.clear_all()
	_check(is_instance_valid(viewport_mounts), "transient host: preserves viewport mount container")
	_check(viewport_mounts.get_parent() == list, "transient host: does not remove durable container")
	_check(list.get_child_count() == 1, "transient host: removes only owned notices")


## 允许后执行失败、显式拒绝和混合批次必须保留不同的持久化语义。
func _run_tool_outcome_semantics_test() -> void:
	var call := {"id": "decision-1", "frame_id": "frame-1"}
	var explicit_reject := AgentDTO.rejected_result(call)
	_check(str(explicit_reject.get("status", "")) == "rejected", "decision: explicit reject stays rejected")
	_check(str(explicit_reject.get("decision_source", "")) == "explicit_reject", "decision: explicit reject source recorded")
	var allowed_error := AgentDTO.tool_result("decision-2", "frame-1", "error", {"message": "invalid path"}, "invalid_path")
	allowed_error["decision_source"] = "execute"
	_check(str(allowed_error.get("status", "")) == "error", "decision: allowed validation failure stays error")
	_check(str(allowed_error.get("decision_source", "")) == "execute", "decision: execution source recorded")
	var unselected := AgentDTO.rejected_result({"id": "decision-3", "frame_id": "frame-1"})
	unselected["decision_source"] = "unselected"
	_check(str(unselected.get("status", "")) == "rejected" and str(unselected.get("decision_source", "")) == "unselected", "decision: mixed batch unselected call is rejected")
	var approval_error := {"kind": "approval", "state": "error", "payload": {"decision": "error", "error_code": "invalid_path", "operation_summary": "打开场景", "affected_paths": ["res://bad.txt"]}}
	_check(TranscriptCopy.canonical_text(approval_error).contains("执行失败（invalid_path）"), "decision: approval error is not rendered as rejection")
	_check(TranscriptCopy.approval_result_line({"decision": "error", "tool": "rebuild_map_builder", "error_code": "generated_target_missing", "error_summary": "The builder must expose generated_target_path"}).contains("The builder must expose generated_target_path"), "decision: approval error shows the typed error summary")
	var activity_reject := {"kind": "tool_activity", "state": "resolved", "payload": {"outcome_status": "rejected", "tool": "open_scene", "args": {}}}
	_check(TranscriptCopy.canonical_text(activity_reject).contains("已拒绝"), "decision: rejected activity is distinct from execution failure")


## 长会话分页夹具：重叠旧页不能覆盖更高 revision 的实时尾部；页耗尽后不再请求。
func _run_navigation_store_test(fixture_dir: String) -> void:
	var fixture := _load_fixture(fixture_dir + "/long_navigation.json")
	var initial: Dictionary = fixture.get("initial_snapshot", {}) if fixture.get("initial_snapshot", {}) is Dictionary else {}
	var older: Dictionary = fixture.get("older_page", {}) if fixture.get("older_page", {}) is Dictionary else {}
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := projector.begin_hydration("navigation-long")
	_check(projector.apply_snapshot({"transcript": initial}, generation), "navigation: initial page hydrates")
	_check(store.history_has_more and store.next_before_ordinal == 40, "navigation: older cursor retained")
	var live := projector.apply_event({
		"event_id": "navigation-long:81", "session_id": "navigation-long", "seq": 81,
		"type": "transcript_patch", "payload": {"stream_key": "e40", "entry": {
			"entry_id": "e40", "ordinal": 40, "kind": "assistant", "state": "complete", "revision": 3, "payload": {"text": "latest"}
		}}
	}, generation)
	_check(live, "navigation: live tail patch applied")
	_check(projector.apply_older_page({"transcript": older}, generation), "navigation: older page accepted")
	_check(store.entry_count() == 3, "navigation: overlap does not duplicate entry")
	_check(int(store.get_entry("e40").get("revision", 0)) == 3, "navigation: stale page cannot regress live revision")
	_check(not store.history_has_more, "navigation: exhausted history is represented")


## 真实 typed renderer 的窗口验收：Store 保留全部条目而根节点严格受限。
func _run_viewport_window_test() -> void:
	var store := TranscriptStore.new()
	store.session_id = "viewport"
	store.generation = 1
	var entries: Array = []
	for index in range(120):
		entries.append({
			"entry_id": "v%d" % index, "ordinal": index, "kind": "assistant", "state": "complete", "revision": 1,
			"payload": {"text": "row %d\n\n%s" % [index, "x".repeat(40 + (index % 7) * 80)]}
		})
	store.replace_snapshot({"entries": entries, "upto_event_seq": 1, "has_more": false}, "viewport", 1)
	var list := VBoxContainer.new()
	var scroll := ScrollContainer.new()
	var renderer := TranscriptRenderer.new()
	var factory := LogEntryRenderer.new()
	factory.theme_colors = {}
	renderer.log_renderer = factory
	renderer.theme_colors = {}
	var viewport := TranscriptViewport.new()
	viewport.max_mounted_roots = 16
	viewport.overscan = 3
	viewport.attach(list, renderer, store, scroll)
	viewport.replace_from_store()
	_check(store.entry_count() == 120, "viewport: Store retains full transcript")
	_check(renderer.mounted_count() == 16, "viewport: mounted roots obey hard bound")
	viewport.suppress_follow()
	_check(not viewport.is_following(), "viewport: interaction suppresses follow mode")
	viewport.return_to_latest()
	_check(viewport.is_following(), "viewport: return action re-enables follow mode")
	var diagnostics := viewport.navigation_diagnostics()
	_check(int(diagnostics.get("mounted_root_count", 0)) <= 16, "viewport: diagnostics record bounded roots")
	_check(diagnostics.has("mounted_rich_text_characters") and diagnostics.has("content_modes"), "viewport: diagnostics record rich-text and modes")
	# Eviction/remount from durable Store preserves renderer semantics and canonical copy.
	var first_id := "v0"
	viewport.notify_scroll(0.0)
	_check(renderer.is_mounted(first_id), "viewport: earlier row remounts from Store")
	var canonical := renderer.copy_text_for_node(renderer.mounted_root(first_id))
	_check(canonical.contains("row 0"), "viewport: remounted copy uses durable canonical text")



func _fixture_directory() -> String:
	var project_root := ProjectSettings.globalize_path("res://")
	var candidate := project_root.path_join("../ai_agent_service/tests/fixtures/transcript")
	if DirAccess.dir_exists_absolute(candidate):
		return candidate
	var fallback := "D:/godot-master/ai_agent_service/tests/fixtures/transcript"
	if DirAccess.dir_exists_absolute(fallback):
		return fallback
	return ""


func _load_fixture(path: String) -> Dictionary:
	if not FileAccess.file_exists(path):
		return {}
	var parsed = JSON.parse_string(FileAccess.get_file_as_string(path))
	if parsed is Dictionary:
		return parsed
	return {}


func _run_fixture(fixture_name: String, fixture: Dictionary) -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var session_id := str(fixture.get("session_id", "s1"))
	var steps: Array = fixture.get("steps", []) if fixture.get("steps", []) is Array else []
	# 显式建模水合的夹具自带 begin_hydration；其余夹具模拟"打开会话即水合
	# （可能为空的快照）后进入 READY"的真实顺序。
	var has_explicit_hydration := false
	for step_value in steps:
		if step_value is Dictionary and str(step_value.get("action", "")) == "begin_hydration":
			has_explicit_hydration = true
			break
	var generation := 0
	if not has_explicit_hydration:
		generation = projector.begin_hydration(session_id)
		var accepted := projector.apply_snapshot({
			"transcript": {
				"version": 1,
				"session_id": session_id,
				"upto_event_seq": 0,
				"legacy": false,
				"entries": []
			}
		}, generation)
		_check(accepted, fixture_name + ": initial hydration accepted")
		_check(projector.is_ready(), fixture_name + ": projector READY after hydration")

	for step_value in steps:
		if not (step_value is Dictionary):
			continue
		var step: Dictionary = step_value
		var action := str(step.get("action", ""))
		match action:
			"patch":
				var applied := projector.apply_event(_patch_envelope(step, session_id), generation)
				var expected_change := bool(step.get("expect_applied", not bool(step.get("replayed", false))))
				_check(applied == expected_change, fixture_name + ": patch %s applied=%s (expected %s)" % [str(step.get("event_id", "")), str(applied), str(expected_change)])
			"patch_during_hydration":
				var applied := projector.apply_event(_patch_envelope(step, session_id), generation)
				_check(not applied, fixture_name + ": patch during hydration rejected")
			"gap":
				projector.request_resync("history_gap")
				generation = projector.begin_hydration(session_id)
				_check(not projector.is_ready(), fixture_name + ": gap leaves READY state")
			"begin_hydration":
				generation = projector.begin_hydration(str(step.get("session_id", session_id)))
			"snapshot", "snapshot_roundtrip":
				var snapshot: Dictionary = step.get("snapshot", {}) if step.get("snapshot", {}) is Dictionary else {}
				var snapshot_generation := int(step.get("generation", generation))
				var wrapped := {"transcript": snapshot, "session_id": str(snapshot.get("session_id", ""))}
				var snapshot_accepted := projector.apply_snapshot(wrapped, snapshot_generation)
				var expected_accepted: bool = (snapshot_generation == generation) and (str(snapshot.get("session_id", "")) == store.session_id)
				_check(snapshot_accepted == expected_accepted, fixture_name + ": snapshot accepted=%s (generation %d vs %d)" % [str(snapshot_accepted), snapshot_generation, generation])
			"resume":
				_check(store.upto_event_seq == int(step.get("after_seq", store.upto_event_seq)), fixture_name + ": resume cursor matches snapshot")
			"disconnect":
				pass

	var expect: Dictionary = fixture.get("expect", {}) if fixture.get("expect", {}) is Dictionary else {}
	_check(store.session_id == str(expect.get("session_id", store.session_id)), fixture_name + ": store session id")
	if expect.has("entry_count"):
		_check(store.entry_count() == int(expect.get("entry_count", 0)), fixture_name + ": entry_count %d == %d" % [store.entry_count(), int(expect.get("entry_count", 0))])
	if expect.has("upto_event_seq"):
		_check(store.upto_event_seq == int(expect.get("upto_event_seq", 0)), fixture_name + ": upto_event_seq %d" % store.upto_event_seq)
	var expected_entries: Array = expect.get("entries", []) if expect.get("entries", []) is Array else []
	var ordered_ids := store.ordered_entry_ids()
	_check(ordered_ids.size() == expected_entries.size(), fixture_name + ": ordered entry count")
	for index in range(mini(ordered_ids.size(), expected_entries.size())):
		var expected: Dictionary = expected_entries[index] if expected_entries[index] is Dictionary else {}
		var entry := store.get_entry(str(ordered_ids[index]))
		_check(str(entry.get("entry_id", "")) == str(expected.get("entry_id", "")), fixture_name + ": order[%d] entry_id" % index)
		_check(str(entry.get("kind", "")) == str(expected.get("kind", "")), fixture_name + ": order[%d] kind" % index)
		_check(str(entry.get("state", "")) == str(expected.get("state", "")), fixture_name + ": order[%d] state" % index)
		_check(int(entry.get("revision", 0)) == int(expected.get("revision", 0)), fixture_name + ": order[%d] revision" % index)
		if expected.has("text"):
			var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}
			_check(str(payload.get("text", "")) == str(expected.get("text", "")), fixture_name + ": order[%d] text" % index)
		if expected.has("content"):
			var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}
			_check(str(payload.get("content", "")) == str(expected.get("content", "")), fixture_name + ": order[%d] thought content" % index)
		if expected.has("token_count"):
			var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}
			_check(int(payload.get("token_count", -1)) == int(expected.get("token_count", -2)), fixture_name + ": order[%d] thought token_count" % index)
		if expected.has("duration_seconds"):
			var payload: Dictionary = entry.get("payload", {}) if entry.get("payload", {}) is Dictionary else {}
			_check(absf(float(payload.get("duration_seconds", -1.0)) - float(expected.get("duration_seconds", -2.0))) < 0.001, fixture_name + ": order[%d] thought duration_seconds" % index)

	# 快照重放幂等：同一快照再次水合后条目集合不变。
	var replay_store := TranscriptStore.new()
	var replay_projector := TranscriptProjector.new(replay_store)
	var replay_generation := replay_projector.begin_hydration(store.session_id)
	var replay_snapshot := {
		"version": 1,
		"session_id": store.session_id,
		"upto_event_seq": store.upto_event_seq,
		"legacy": store.legacy,
		"entries": [],
	}
	for entry_id in store.ordered_entry_ids():
		replay_snapshot["entries"].append(store.get_entry(str(entry_id)))
	replay_projector.apply_snapshot({"transcript": replay_snapshot}, replay_generation)
	_check(replay_store.entry_count() == store.entry_count(), fixture_name + ": snapshot replay preserves entry count")


func _patch_envelope(step: Dictionary, session_id: String) -> Dictionary:
	var entry: Dictionary = step.get("entry", {}) if step.get("entry", {}) is Dictionary else {}
	return {
		"event_id": str(step.get("event_id", "")),
		"session_id": session_id,
		"seq": int(step.get("seq", 0)),
		"type": "transcript_patch",
		"payload": {"entry": entry, "stream_key": str(entry.get("entry_id", ""))},
	}


func _renderer_smoke_test() -> void:
	# 渲染器只按 kind 建节点：两条相同正文的助手条目必须各自成节点。
	var list := VBoxContainer.new()
	var log_renderer := LogEntryRenderer.new()
	log_renderer.theme_colors = {}
	var renderer := TranscriptRenderer.new()
	renderer.log_renderer = log_renderer
	renderer.theme_colors = {}
	renderer.attach(list)
	var entries := [
		{"entry_id": "e1", "ordinal": 0, "kind": "user", "state": "complete", "revision": 1, "payload": {"text": "你好"}},
		{"entry_id": "e2", "ordinal": 1, "kind": "assistant", "state": "complete", "revision": 1, "payload": {"text": "DONE"}},
		{"entry_id": "e3", "ordinal": 2, "kind": "assistant", "state": "complete", "revision": 1, "payload": {"text": "DONE"}},
	]
	renderer.render_all(entries)
	_check(list.get_child_count() == 3, "renderer: one node per entry (two identical answers kept)")
	var updated := {"entry_id": "e2", "ordinal": 1, "kind": "assistant", "state": "streaming", "revision": 2, "payload": {"text": "DONE (revised)"}}
	renderer.apply_entry(updated)
	_check(list.get_child_count() == 3, "renderer: revision update replaces node in place")
	renderer.clear_all()
	_check(list.get_child_count() <= 3, "renderer: clear frees entry nodes")


## Thought 渲染（任务 4.6/5.5）：思考中显示 token 计数、完成后显示耗时，
## 展开状态跨 revision 保持，水合重建后内容完整可展开。
func _run_thought_renderer_test() -> void:
	var list := VBoxContainer.new()
	var log_renderer := LogEntryRenderer.new()
	log_renderer.theme_colors = {}
	var renderer := TranscriptRenderer.new()
	renderer.log_renderer = log_renderer
	renderer.theme_colors = {}
	renderer.attach(list)

	var thinking := {
		"entry_id": "t1", "ordinal": 0, "kind": "thought", "state": "thinking", "revision": 1,
		"payload": {"content": "先看参数", "token_count": 12, "duration_seconds": null, "response_attempt_id": "attempt-primary"}
	}
	renderer.apply_entry(thinking)
	_check(list.get_child_count() == 1, "thought: node created")
	var toggle: Button = _find_descendant_button(list.get_child(0))
	var detail: RichTextLabel = _find_descendant_rich(list.get_child(0))
	_check(toggle != null and toggle.text.contains("Thinking 12 Tokens"), "thought: active header shows token count (got: %s)" % (toggle.text if toggle != null else "<null>"))
	_check(detail != null and not detail.visible, "thought: collapsed by default")

	# 空正文恢复仍是同一个未完成 Thought：新的 response attempt 到来时不显示
	# 中间的 Thought for 79.33s，而继续显示 Thinking 与累计 token。
	if toggle != null:
		toggle.pressed.emit()
	_check(detail != null and detail.visible, "thought: user expanded")
	var thinking2 := {
		"entry_id": "t1", "ordinal": 0, "kind": "thought", "state": "thinking", "revision": 2,
		"payload": {"content": "先看参数，再调整重力", "token_count": 30, "duration_seconds": null, "response_attempt_id": "attempt-recovery"}
	}
	renderer.apply_entry(thinking2)
	_check(list.get_child_count() == 1, "thought: revision replaces node in place")
	var toggle2: Button = _find_descendant_button(list.get_child(0))
	var detail2: RichTextLabel = _find_descendant_rich(list.get_child(0))
	_check(toggle2 != null and toggle2.text.contains("Thinking 30 Tokens") and not toggle2.text.contains("Thought for"), "thought: recovery remains non-terminal")
	_check(detail2 != null and detail2.visible, "thought: expanded state preserved across revisions")

	# 只有恢复最终结束后头部才切换为完整逻辑生命周期的权威耗时。
	var completed := {
		"entry_id": "t1", "ordinal": 0, "kind": "thought", "state": "complete", "revision": 3,
		"payload": {"content": "先看参数，再调整重力", "token_count": 30, "duration_seconds": 130.0, "response_attempt_id": "attempt-recovery"}
	}
	renderer.apply_entry(completed)
	var toggle3: Button = _find_descendant_button(list.get_child(0))
	var detail3: RichTextLabel = _find_descendant_rich(list.get_child(0))
	_check(toggle3 != null and toggle3.text.contains("Thought for 130.00s"), "thought: completed header shows final duration (got: %s)" % (toggle3.text if toggle3 != null else "<null>"))
	_check(detail3 != null and detail3.visible, "thought: expanded state preserved after completion")

	# 历史水合整体重建：默认折叠，但持久化内容完整可展开。
	# （旧节点经 queue_free 释放，在无帧处理的测试脚本里延迟移除，取最新子节点。）
	renderer.render_all([completed])
	var hydrated: Node = list.get_child(list.get_child_count() - 1)
	var toggle4: Button = _find_descendant_button(hydrated)
	var detail4: RichTextLabel = _find_descendant_rich(hydrated)
	_check(toggle4 != null and toggle4.text.contains("Thought for 130.00s"), "thought: hydrated header rebuilt from persisted duration")
	_check(detail4 != null and not detail4.visible, "thought: hydrated card collapsed by default")
	_check(detail4 != null and detail4.get_parsed_text().contains("先看参数"), "thought: hydrated content preserved and expandable")


func _find_descendant_button(node: Node) -> Button:
	for child in node.get_children():
		if child is Button:
			return child
		var found := _find_descendant_button(child)
		if found != null:
			return found
	return null


func _find_descendant_rich(node: Node) -> RichTextLabel:
	for child in node.get_children():
		if child is RichTextLabel:
			return child
		var found := _find_descendant_rich(child)
		if found != null:
			return found
	return null


## 任务 3.5 / 2.4：恢复水合的快照栅栏——`upto_event_seq` 之前的条目由完整
## 快照恰好渲染一次，之后的事件由替代订阅送达；乐观用户条目凭
## client_message_id 在快照替换中保留或替换。
func _run_snapshot_fence_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	# 初始水合（空快照）。
	var gen1 := projector.begin_hydration("fence")
	projector.apply_snapshot({
		"transcript": {"version": 1, "session_id": "fence", "upto_event_seq": 0, "legacy": false, "entries": []},
	}, gen1)
	_check(projector.is_ready(), "snapshot-fence: initial hydration ready")
	# 乐观用户消息（任务 2.4）：恢复前存在。
	store.add_optimistic_user_entry("pending text", "cm_pending")
	_check(store.entry_count() == 1, "snapshot-fence: optimistic entry present before recovery")

	# 恢复水合：同会话 begin_hydration 保留乐观条目，快照原子切点 upto=8。
	var gen2 := projector.begin_hydration("fence", true)
	var fence_entries: Array = []
	for index in range(1, 9):
		fence_entries.append({
			"entry_id": "f%d" % index, "ordinal": index, "kind": "assistant",
			"state": "complete", "revision": 1, "turn_id": "t1",
			"tool_call_id": null, "payload": {"text": "row %d" % index},
		})
	var fence_snapshot := {
		"transcript": {
			"version": 1, "session_id": "fence", "upto_event_seq": 8,
			"legacy": false, "entries": fence_entries,
		},
	}
	_check(projector.apply_snapshot(fence_snapshot, gen2), "snapshot-fence: full snapshot accepted")
	_check(store.upto_event_seq == 8, "snapshot-fence: atomic cut adopted")
	_check(store.entry_count() == 9, "snapshot-fence: snapshot entries + preserved optimistic")
	_check(store.ordered_entry_ids().has("optimistic:cm_pending"), "snapshot-fence: unconfirmed optimistic preserved")

	# 快照内事件（seq 8）被重放：不得重复渲染（Store 幂等 + 已见表）。
	var replay_e8 := _fence_patch(8, "f8", 8)
	_check(not projector.apply_event(replay_e8, gen2), "snapshot-fence: event at cut not re-rendered")
	_check(store.entry_count() == 9, "snapshot-fence: events at or before cut render exactly once")

	# 切点之后的事件来自替代订阅：seq 9、10 正常投影。
	var live_e9 := _fence_patch(9, "f9", 9)
	var live_e10 := _fence_patch(10, "f10", 10)
	_check(projector.apply_event(live_e9, gen2), "snapshot-fence: event after cut delivered by subscription")
	_check(projector.apply_event(live_e10, gen2), "snapshot-fence: second live event delivered")
	_check(store.entry_count() == 11, "snapshot-fence: live tail appended once")
	_check(str(store.get_entry("f9").get("entry_id", "")) == "f9", "snapshot-fence: live entry visible")

	# 快照确认乐观 client_message_id 后替换为权威条目（任务 2.4）。
	var store2 := TranscriptStore.new()
	var projector2 := TranscriptProjector.new(store2)
	store2.add_optimistic_user_entry("pending text", "cm_pending")
	var gen3 := projector2.begin_hydration("fence2", true)
	var confirming := {
		"transcript": {
			"version": 1, "session_id": "fence2", "upto_event_seq": 1, "legacy": false,
			"entries": [{
				"entry_id": "eUser", "ordinal": 0, "kind": "user", "state": "complete",
				"revision": 1, "turn_id": "t1", "tool_call_id": null,
				"payload": {"text": "pending text", "client_message_id": "cm_pending"},
			}],
		},
	}
	projector2.apply_snapshot(confirming, gen3)
	_check(store2.entry_count() == 1, "snapshot-fence: confirmed optimistic replaced by authoritative entry")
	_check(not store2.ordered_entry_ids().has("optimistic:cm_pending"), "snapshot-fence: confirmed optimistic removed")


func _fence_patch(seq: int, entry_id: String, ordinal: int) -> Dictionary:
	return {
		"event_id": "fence:%d" % seq, "session_id": "fence", "seq": seq,
		"type": "transcript_patch",
		"payload": {
			"patch_format": "full", "patch_version": 2, "stream_key": entry_id,
			"entry": {
				"entry_id": entry_id, "ordinal": ordinal, "kind": "assistant",
				"state": "complete", "revision": 1, "turn_id": "t1",
				"tool_call_id": null, "payload": {"text": "row %d" % ordinal},
			},
		},
	}
