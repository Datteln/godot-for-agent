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

var state: int = State.HYDRATING
var generation: int = 0

var _store: RefCounted


func _init(store: RefCounted) -> void:
	_store = store


## 开始一次新的水合：递增 generation、回到 HYDRATING、清空旧展示态。
func begin_hydration(new_session_id: String) -> int:
	generation += 1
	state = State.HYDRATING
	_store.clear()
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
	state = State.READY
	snapshot_replaced.emit(changed)
	return true


## 应用一条 WebSocket 事件包络；只有 READY 状态下的 transcript_patch 会
## 改变 Store。返回是否改变了展示态。
func apply_event(envelope: Dictionary, event_generation: int) -> bool:
	if state != State.READY or event_generation != generation:
		return false
	if str(envelope.get("session_id", "")) != _store.session_id:
		return false
	if str(envelope.get("type", "")) != "transcript_patch":
		return false
	var payload_value: Variant = envelope.get("payload", {})
	if not (payload_value is Dictionary):
		return false
	var payload: Dictionary = payload_value
	var event_id := str(envelope.get("event_id", ""))
	var seq := int(envelope.get("seq", 0))
	var applied: bool = _store.apply_patch(payload, event_id)
	if applied:
		if seq > _store.upto_event_seq:
			_store.upto_event_seq = seq
		var entry_value: Variant = payload.get("entry", {})
		var entry_id := ""
		if entry_value is Dictionary:
			entry_id = str(entry_value.get("entry_id", ""))
		patch_applied.emit(entry_id)
	else:
		# 即便补丁被去重/拒绝，也要推进游标：该事件已被“处理”，重连时不必重放。
		if event_id != "" and seq > _store.upto_event_seq:
			_store.upto_event_seq = seq
	return applied


## gap/resync 到达时停止接受实时补丁，等待重新水合。
func request_resync(reason: String) -> void:
	if state == State.HYDRATING:
		return
	state = State.HYDRATING
	hydration_required.emit(reason)


## 当前是否处于可接受实时补丁的状态。
func is_ready() -> bool:
	return state == State.READY
