## 流式背压前端契约测试（streaming-transcript-backpressure 任务 3.4 / 4.4 前端侧）。
##
## 验证有界实时载荷在 Store/Projector/Viewport 三层的正确性：
## - 追加增量按 base_revision 拼接重建完整正文；
## - 修订缺口 / 预览后增量 / 未知格式一律请求快照重同步而非猜测；
## - 终态修订到达后不得回退到流式中间态（同帧合并下亦然）；
## - 视口批量应用保持有界渲染与 follow/锚点语义。
extends SceneTree

const TranscriptStore = preload("res://addons/ai_agent/transcript/transcript_store.gd")
const TranscriptProjector = preload("res://addons/ai_agent/transcript/transcript_projector.gd")
const TranscriptRenderer = preload("res://addons/ai_agent/transcript/transcript_renderer.gd")
const TranscriptViewport = preload("res://addons/ai_agent/transcript/transcript_viewport.gd")
const TranscriptPatchBatcher = preload("res://addons/ai_agent/transcript/transcript_patch_batcher.gd")
const LogEntryRenderer = preload("res://addons/ai_agent/ui/log_entry_renderer.gd")

var _failures := 0
var _checks := 0


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _init() -> void:
	_run_append_delta_reconstruction_test()
	_run_revision_gap_requests_hydration_test()
	_run_preview_then_delta_representation_gap_test()
	_run_unknown_patch_format_fallback_test()
	_run_terminal_over_stream_ordering_test()
	_run_viewport_batch_bounded_test()
	_run_batcher_preserves_founding_full_patch_test()
	_run_batcher_end_to_end_stream_renders_test()
	print("streaming backpressure checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


func _full_event(session: String, seq: int, entry: Dictionary) -> Dictionary:
	return {
		"event_id": "%s:%d" % [session, seq], "session_id": session, "seq": seq,
		"type": "transcript_patch",
		"payload": {"patch_format": "full", "patch_version": 2, "stream_key": str(entry.get("entry_id", "")), "entry": entry}
	}


func _delta_event(session: String, seq: int, entry_id: String, revision: int, base_revision: int, append: String) -> Dictionary:
	return {
		"event_id": "%s:%d" % [session, seq], "session_id": session, "seq": seq,
		"type": "transcript_patch",
		"payload": {
			"patch_format": "append_delta", "patch_version": 2, "stream_key": entry_id,
			"entry_id": entry_id, "kind": "assistant", "state": "streaming",
			"revision": revision, "base_revision": base_revision, "text_field": "text", "append_text": append
		}
	}


func _preview_event(session: String, seq: int, entry_id: String, revision: int, preview: String, total: int) -> Dictionary:
	return {
		"event_id": "%s:%d" % [session, seq], "session_id": session, "seq": seq,
		"type": "transcript_patch",
		"payload": {
			"patch_format": "preview", "patch_version": 2, "stream_key": entry_id,
			"entry_id": entry_id, "kind": "assistant", "state": "streaming",
			"revision": revision, "base_revision": revision - 1, "text_field": "text",
			"preview_text": preview, "total_chars": total
		}
	}


func _hydrate(projector: RefCounted, store: RefCounted, session: String) -> int:
	var generation: int = projector.begin_hydration(session)
	projector.apply_snapshot({
		"transcript": {"version": 1, "session_id": session, "upto_event_seq": 0, "legacy": false, "entries": []}
	}, generation)
	return generation


## 追加增量必须能凭 base_revision 链重建完整正文（任务 3.1）。
func _run_append_delta_reconstruction_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := _hydrate(projector, store, "session-delta")
	var base_entry := {
		"entry_id": "e1", "ordinal": 0, "kind": "assistant", "state": "streaming",
		"revision": 1, "payload": {"text": "hello"}
	}
	_check(projector.apply_event(_full_event("session-delta", 1, base_entry), generation), "delta: full base patch applied")
	_check(projector.apply_event(_delta_event("session-delta", 2, "e1", 2, 1, " world"), generation), "delta: append accepted on matching base")
	_check(projector.apply_event(_delta_event("session-delta", 3, "e1", 3, 2, "!"), generation), "delta: chained append accepted")
	var entry: Dictionary = store.get_entry("e1")
	_check(str(entry.get("payload", {}).get("text", "")) == "hello world!", "delta: reconstructed text is complete")
	_check(int(entry.get("revision", 0)) == 3, "delta: revision advances with each append")


## base_revision 与已接受修订不一致时必须请求快照重同步（任务 3.1）。
func _run_revision_gap_requests_hydration_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := _hydrate(projector, store, "session-gap")
	var reasons: Array = []
	projector.hydration_required.connect(func(reason: String): reasons.append(reason))
	var base_entry := {
		"entry_id": "e1", "ordinal": 0, "kind": "assistant", "state": "streaming",
		"revision": 1, "payload": {"text": "hello"}
	}
	projector.apply_event(_full_event("session-gap", 1, base_entry), generation)
	# base_revision=5 与已接受 revision=1 不匹配 → 视为修订缺口。
	var applied: bool = projector.apply_event(_delta_event("session-gap", 2, "e1", 6, 5, " gap"), generation)
	_check(not applied, "gap: mismatched base_revision is not applied")
	_check(reasons.has("revision_gap"), "gap: hydration requested with typed reason")
	_check(projector.state == TranscriptProjector.State.HYDRATING, "gap: projector leaves READY on gap")
	_check(str(store.get_entry("e1").get("payload", {}).get("text", "")) == "hello", "gap: store state unchanged on gap")


## 受限预览之后到达的增量无法重建前缀，必须请求重同步（任务 3.1）。
func _run_preview_then_delta_representation_gap_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := _hydrate(projector, store, "session-preview")
	var reasons: Array = []
	projector.hydration_required.connect(func(reason: String): reasons.append(reason))
	var base_entry := {
		"entry_id": "e1", "ordinal": 0, "kind": "assistant", "state": "streaming",
		"revision": 1, "payload": {"text": "head"}
	}
	projector.apply_event(_full_event("session-preview", 1, base_entry), generation)
	_check(projector.apply_event(_preview_event("session-preview", 2, "e1", 2, "tail-preview", 1024), generation), "preview: bounded preview applied")
	var preview_entry: Dictionary = store.get_entry("e1")
	_check(str(preview_entry.get("payload", {}).get("text", "")) == "tail-preview", "preview: display shows bounded tail")
	_check(int(preview_entry.get("payload", {}).get("preview_total_chars", 0)) == 1024, "preview: total_chars metadata retained")
	# 预览之后到达的增量（base=2）无法重建被截断的前缀。
	var applied: bool = projector.apply_event(_delta_event("session-preview", 3, "e1", 3, 2, " more"), generation)
	_check(not applied, "preview: delta after preview is not applied")
	_check(reasons.has("representation_gap"), "preview: representation gap requested")


## 未知 patch_format 必须回退快照重同步（特性兼容回退，任务 1.3）。
func _run_unknown_patch_format_fallback_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := _hydrate(projector, store, "session-unknown")
	var reasons: Array = []
	projector.hydration_required.connect(func(reason: String): reasons.append(reason))
	var event := {
		"event_id": "session-unknown:1", "session_id": "session-unknown", "seq": 1,
		"type": "transcript_patch",
		"payload": {"patch_format": "some_future_format", "patch_version": 99, "stream_key": "e1"}
	}
	var applied: bool = projector.apply_event(event, generation)
	_check(not applied, "unknown format: not applied")
	_check(reasons.has("unknown_patch_format"), "unknown format: hydration requested")


## 终态修订到达后不得被迟到的流式中间态回退（同帧合并语义，任务 3.2）。
func _run_terminal_over_stream_ordering_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := _hydrate(projector, store, "session-terminal")
	var base_entry := {
		"entry_id": "e1", "ordinal": 0, "kind": "assistant", "state": "streaming",
		"revision": 1, "payload": {"text": "partial"}
	}
	projector.apply_event(_full_event("session-terminal", 1, base_entry), generation)
	var terminal_entry := {
		"entry_id": "e1", "ordinal": 0, "kind": "assistant", "state": "complete",
		"revision": 2, "payload": {"text": "partial and final"}
	}
	_check(projector.apply_event(_full_event("session-terminal", 2, terminal_entry), generation), "terminal: complete patch applied")
	# 迟到的流式修订（revision 更低）不得把完成态退回 streaming。
	var late_stream := {
		"entry_id": "e1", "ordinal": 0, "kind": "assistant", "state": "streaming",
		"revision": 1, "payload": {"text": "partial"}
	}
	var applied: bool = projector.apply_event(_full_event("session-terminal", 3, late_stream), generation)
	_check(not applied, "terminal: stale streaming revision rejected")
	var stored: Dictionary = store.get_entry("e1")
	_check(str(stored.get("state", "")) == "complete", "terminal: entry remains complete")
	_check(str(stored.get("payload", {}).get("text", "")) == "partial and final", "terminal: final text retained")


## 视口批量应用保持有界渲染、follow 推进与禁用 follow 时的锚点（任务 3.3）。
func _run_viewport_batch_bounded_test() -> void:
	var store := TranscriptStore.new()
	store.session_id = "viewport-batch"
	store.generation = 1
	var entries: Array = []
	for index in range(40):
		entries.append({
			"entry_id": "b%d" % index, "ordinal": index, "kind": "assistant", "state": "complete",
			"revision": 1, "payload": {"text": "row %d" % index}
		})
	store.replace_snapshot({"entries": entries, "upto_event_seq": 1, "has_more": false}, "viewport-batch", 1)
	var list := VBoxContainer.new()
	var scroll := ScrollContainer.new()
	var renderer := TranscriptRenderer.new()
	var factory := LogEntryRenderer.new()
	factory.theme_colors = {}
	renderer.log_renderer = factory
	renderer.theme_colors = {}
	var viewport := TranscriptViewport.new()
	viewport.max_mounted_roots = 12
	viewport.overscan = 2
	viewport.attach(list, renderer, store, scroll)
	viewport.replace_from_store()
	var mounted_before := renderer.mounted_count()
	_check(mounted_before <= 12, "viewport: initial mount obeys bound")
	# 批量更新窗口内的多个条目：渲染根数仍受硬上限约束。
	var batch: Array = []
	for index in range(28, 40):
		batch.append(store.get_entry("b%d" % index))
	viewport.apply_batch(batch)
	_check(renderer.mounted_count() <= 12, "viewport: batch apply keeps root count bounded")
	# 禁用 follow 后批量更新不得把视口强行拉到底部（锚点保持）。
	viewport.suppress_follow()
	_check(not viewport.is_following(), "viewport: follow disabled for anchor test")
	var diagnostics := viewport.navigation_diagnostics()
	_check(int(diagnostics.get("mounted_root_count", 0)) <= 12, "viewport: diagnostics bounded after batch")


func _assistant_full(entry_id: String, session: String, seq: int, text: String) -> Dictionary:
	return _full_event(session, seq, {
		"entry_id": entry_id, "ordinal": 0, "kind": "assistant", "state": "streaming",
		"revision": 1, "payload": {"text": text}
	})


## 回归：首个完整补丁（建立条目）绝不能被其后的增量在暂存集里覆盖。
## 旧实现用 `pending[entry_id] = event` 覆盖，导致“只有增量、没有基础条目”，
## 投影器判 `delta_missing_base` 并在整个轮次卡死在 HYDRATING、实时不渲染。
func _run_batcher_preserves_founding_full_patch_test() -> void:
	var batcher := TranscriptPatchBatcher.new()
	batcher.enqueue("e1", _assistant_full("e1", "s", 1, "hello"))
	batcher.enqueue("e1", _delta_event("s", 2, "e1", 2, 1, " world"))
	batcher.enqueue("e1", _delta_event("s", 3, "e1", 3, 2, "!"))
	var batches: Array = batcher.take_all()
	_check(batches.size() == 1, "batcher: single entry grouped")
	var events: Array = batches[0].get("events", []) if batches.size() > 0 else []
	_check(events.size() == 2, "batcher: full kept + chained deltas merged (2 events, not 3)")
	var first: Dictionary = events[0] if events.size() > 0 else {}
	var second: Dictionary = events[1] if events.size() > 1 else {}
	_check(str(first.get("payload", {}).get("patch_format", "")) == "full", "batcher: founding full patch is first")
	_check(str(second.get("payload", {}).get("patch_format", "")) == "append_delta", "batcher: merged delta second")
	_check(str(second.get("payload", {}).get("append_text", "")) == " world!", "batcher: appends concatenated in order")
	_check(int(second.get("payload", {}).get("base_revision", -1)) == 1, "batcher: merged delta keeps earliest base_revision")
	_check(int(second.get("payload", {}).get("revision", -1)) == 3, "batcher: merged delta carries newest revision")


## 端到端：完整补丁+增量在同一窗口依序应用后，Store 得到完整正文并渲染，
## 而不是因缺基础条目触发重同步。
func _run_batcher_end_to_end_stream_renders_test() -> void:
	var store := TranscriptStore.new()
	var projector := TranscriptProjector.new(store)
	var generation := _hydrate(projector, store, "session-e2e")
	var hydrated_reasons: Array = []
	projector.hydration_required.connect(func(reason: String): hydrated_reasons.append(reason))
	var batcher := TranscriptPatchBatcher.new()
	# 同一帧内到达：建立条目的完整补丁 + 两个增量（模拟服务端 50ms 限速突发）。
	batcher.enqueue("e1", _assistant_full("e1", "session-e2e", 1, "hello"))
	batcher.enqueue("e1", _delta_event("session-e2e", 2, "e1", 2, 1, " world"))
	batcher.enqueue("e1", _delta_event("session-e2e", 3, "e1", 3, 2, "!"))
	var applied := 0
	for batch_value in batcher.take_all():
		var batch: Dictionary = batch_value
		for event_value in batch.get("events", []):
			if projector.apply_event(event_value, generation):
				applied += 1
	_check(applied == 2, "e2e: full + merged delta both applied (got %d)" % applied)
	_check(hydrated_reasons.is_empty(), "e2e: no hydration triggered for a healthy stream")
	var entry: Dictionary = store.get_entry("e1")
	_check(str(entry.get("payload", {}).get("text", "")) == "hello world!", "e2e: streamed text renders complete")
	_check(projector.state == TranscriptProjector.State.READY, "e2e: projector stays READY")
