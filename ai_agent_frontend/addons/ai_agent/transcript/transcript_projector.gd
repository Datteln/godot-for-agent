## 展示稿投影器与水合状态机（任务 4.2 / 4.4）。
##
## 状态机：HYDRATING → REPLACE_SNAPSHOT → READY（→ SUBSCRIBED 由 socket 状态
## 另行表达）。在 HYDRATING 中不接受任何实时补丁；快照校验（session_id +
## generation）通过后才原子替换 Store 并进入 READY。会话切换、gap/resync 都会
## 回到 HYDRATING 并递增 generation，使迟到的旧会话/旧世代响应被整体拒绝。
extends RefCounted

const TranscriptStore = preload("res://addons/ai_agent/transcript/transcript_store.gd")

enum State { HYDRATING, REPLACE_SNAPSHOT, READY }

## 水合完成、Store 已被快照原子替换。
signal snapshot_replaced(changed_entry_ids: Array)
## 一条实时补丁被成功应用。
signal patch_applied(entry_id: String)
## 投影器要求重新水合（gap/resync/会话失效）。
signal hydration_required(reason: String)
## 每个实时补丁在导航边界的拒绝原因；不得传入完整正文。
signal patch_rejected(diagnostic: Dictionary)

var state: int = State.HYDRATING
var generation: int = 0

var _store: RefCounted
## entry_id -> 最近一次被接受的实时表示（"full" 或 "preview"）。
## preview 之后到达的 append_delta 无法重建精确正文，必须走快照重同步。
var _entry_formats: Dictionary = {}


func _init(store: RefCounted) -> void:
	_store = store


## 开始一次新的水合：递增 generation、回到 HYDRATING、清空旧展示态。
func begin_hydration(new_session_id: String) -> int:
	generation += 1
	state = State.HYDRATING
	_store.clear()
	_entry_formats.clear()
	_store.session_id = new_session_id
	_store.generation = generation
	return generation


## 应用历史快照。校验 session 与 generation 后原子替换 Store 并进入 READY。
## 返回是否接受该快照（False 表示过期/串会话，调用方不得继续渲染）。
func apply_snapshot(response: Dictionary, response_generation: int) -> bool:
	if state != State.HYDRATING:
		# 已在 READY 时收到快照 = 重新水合流程；同样要求 generation 匹配。
		if response_generation != generation:
			return false
	var transcript_value: Variant = response.get("transcript", null)
	if not (transcript_value is Dictionary):
		return false
	var transcript: Dictionary = transcript_value
	var snapshot_session := str(transcript.get("session_id", ""))
	if snapshot_session != _store.session_id or response_generation != generation:
		return false
	state = State.REPLACE_SNAPSHOT
	var changed: Array = _store.replace_snapshot(transcript, snapshot_session, generation)
	_entry_formats.clear()
	state = State.READY
	snapshot_replaced.emit(changed)
	return true


## 应用一条 WebSocket 事件包络；只有 READY 状态下的 transcript_patch 会
## 改变 Store。返回是否改变了展示态。
##
## 有界实时表示（任务 3.1）：载荷 `patch_format` 区分完整补丁（`full`）、
## 追加增量（`append_delta`）与受限预览（`preview`）。增量仅在已接受修订
## 等于其 `base_revision` 时可应用；修订缺口、表示缺口或未知格式一律转入
## 既有的快照重同步路径，而不是猜测内容。
func apply_event(envelope: Dictionary, event_generation: int) -> bool:
	if state != State.READY:
		_reject_patch(envelope, "projector_not_ready")
		return false
	if event_generation != generation:
		_reject_patch(envelope, "generation_mismatch")
		return false
	if str(envelope.get("session_id", "")) != _store.session_id:
		_reject_patch(envelope, "session_mismatch")
		return false
	if str(envelope.get("type", "")) != "transcript_patch":
		return false
	var payload_value: Variant = envelope.get("payload", {})
	if not (payload_value is Dictionary):
		_reject_patch(envelope, "invalid_payload")
		return false
	var payload: Dictionary = payload_value
	var resolved: Dictionary = _resolve_patch_payload(envelope, payload)
	if resolved.is_empty():
		# _resolve_patch_payload 已按缺口类型请求重同步或记录拒绝。
		return false
	var event_id := str(envelope.get("event_id", ""))
	var seq := int(envelope.get("seq", 0))
	var applied: bool = _store.apply_patch(resolved, event_id)
	if applied:
		if seq > _store.upto_event_seq:
			_store.upto_event_seq = seq
		var entry_value: Variant = resolved.get("entry", {})
		var entry_id := ""
		if entry_value is Dictionary:
			entry_id = str(entry_value.get("entry_id", ""))
		_entry_formats[entry_id] = str(resolved.get("_representation", "full"))
		patch_applied.emit(entry_id)
	else:
		_reject_patch(envelope, "duplicate_or_non_newer_revision")
		# 即便补丁被去重/拒绝，也要推进游标：该事件已被“处理”，重连时不必重放。
		if event_id != "" and seq > _store.upto_event_seq:
			_store.upto_event_seq = seq
	return applied


## 把任意表示的补丁载荷解析为“完整条目补丁”形态；无法安全解析时请求重同步。
func _resolve_patch_payload(envelope: Dictionary, payload: Dictionary) -> Dictionary:
	var patch_format := str(payload.get("patch_format", "full"))
	match patch_format:
		"full":
			var raw_entry: Variant = payload.get("entry", null)
			if not (raw_entry is Dictionary) or str(raw_entry.get("entry_id", "")) == "":
				_reject_patch(envelope, "invalid_payload")
				return {}
			return {"entry": raw_entry, "stream_key": str(payload.get("stream_key", "")), "_representation": "full"}
		"append_delta":
			return _resolve_append_delta(envelope, payload)
		"preview":
			return _resolve_preview(envelope, payload)
		_:
			# 未知表示 = 特性不兼容：回退到既有快照重同步（任务 1.3）。
			_reject_patch(envelope, "unknown_patch_format")
			request_resync("unknown_patch_format")
			return {}


## 应用追加增量：已接受修订必须等于 base_revision，且此前不是受限预览。
func _resolve_append_delta(envelope: Dictionary, payload: Dictionary) -> Dictionary:
	var entry_id := str(payload.get("entry_id", ""))
	var base_revision := int(payload.get("base_revision", -1))
	var text_field := str(payload.get("text_field", ""))
	if entry_id == "" or text_field == "":
		_reject_patch(envelope, "invalid_payload")
		return {}
	var existing: Dictionary = _store.get_entry(entry_id)
	if existing.is_empty():
		_reject_patch(envelope, "delta_missing_base")
		request_resync("delta_missing_base")
		return {}
	if int(existing.get("revision", -1)) != base_revision:
		_reject_patch(envelope, "revision_gap")
		request_resync("revision_gap")
		return {}
	if str(_entry_formats.get(entry_id, "full")) == "preview":
		_reject_patch(envelope, "representation_gap")
		request_resync("representation_gap")
		return {}
	var merged: Dictionary = existing.duplicate(true)
	merged["revision"] = int(payload.get("revision", base_revision))
	if str(payload.get("state", "")) != "":
		merged["state"] = str(payload.get("state", ""))
	var merged_payload: Dictionary = merged.get("payload", {}) if merged.get("payload", {}) is Dictionary else {}
	merged_payload[text_field] = str(merged_payload.get(text_field, "")) + str(payload.get("append_text", ""))
	var meta_value: Variant = payload.get("meta", null)
	if meta_value is Dictionary:
		for meta_key in (meta_value as Dictionary).keys():
			merged_payload[meta_key] = (meta_value as Dictionary)[meta_key]
	merged["payload"] = merged_payload
	return {
		"entry": merged,
		"stream_key": str(payload.get("stream_key", entry_id)),
		"_representation": "full",
	}


## 应用受限预览：以末尾预览文本替换显示内容，保留条目身份与修订号。
func _resolve_preview(envelope: Dictionary, payload: Dictionary) -> Dictionary:
	var entry_id := str(payload.get("entry_id", ""))
	var text_field := str(payload.get("text_field", ""))
	if entry_id == "" or text_field == "":
		_reject_patch(envelope, "invalid_payload")
		return {}
	var existing: Dictionary = _store.get_entry(entry_id)
	if existing.is_empty():
		_reject_patch(envelope, "preview_missing_base")
		request_resync("preview_missing_base")
		return {}
	var merged: Dictionary = existing.duplicate(true)
	merged["revision"] = int(payload.get("revision", int(existing.get("revision", 1))))
	if str(payload.get("state", "")) != "":
		merged["state"] = str(payload.get("state", ""))
	var merged_payload: Dictionary = merged.get("payload", {}) if merged.get("payload", {}) is Dictionary else {}
	merged_payload[text_field] = str(payload.get("preview_text", ""))
	merged_payload["preview_total_chars"] = int(payload.get("total_chars", 0))
	merged["payload"] = merged_payload
	return {
		"entry": merged,
		"stream_key": str(payload.get("stream_key", entry_id)),
		"_representation": "preview",
	}


## 合并同一 READY 世代的旧页；初始水合始终走 apply_snapshot 原子替换。
func apply_older_page(response: Dictionary, response_generation: int) -> bool:
	if state != State.READY or response_generation != generation:
		return false
	var transcript_value: Variant = response.get("transcript", null)
	if not (transcript_value is Dictionary):
		return false
	var transcript: Dictionary = transcript_value
	var page_session := str(transcript.get("session_id", ""))
	transcript["has_more"] = bool(response.get("has_more", transcript.get("has_more", false)))
	transcript["next_before_ordinal"] = response.get("next_before_ordinal", transcript.get("next_before_ordinal", -1))
	_store.merge_older_page(transcript, page_session, generation)
	return page_session == _store.session_id


func _reject_patch(envelope: Dictionary, reason: String) -> void:
	patch_rejected.emit({
		"reason": reason,
		"event_id": str(envelope.get("event_id", "")),
		"seq": int(envelope.get("seq", 0)),
		"session_id": str(envelope.get("session_id", "")),
		"generation": generation,
	})


## gap/resync 到达时停止接受实时补丁，等待重新水合。
func request_resync(reason: String) -> void:
	if state == State.HYDRATING:
		return
	state = State.HYDRATING
	hydration_required.emit(reason)


## 当前是否处于可接受实时补丁的状态。
func is_ready() -> bool:
	return state == State.READY
