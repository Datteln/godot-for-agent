class_name ChatTimelineProjector
extends RefCounted

const Contracts = preload("res://addons/ai_agent/timeline/chat_timeline_contracts.gd")
const NON_PRESENTATION_EVENTS := {
	"agent_model_selected": true,
	"agent_model_fallback": true,
	"context_usage": true,
	"turn_progress": true,
	"transport_ping": true,
	"transport_pong": true,
}

## Accepted WebSocket 与 canonical history record 共用的纯投影器。


func project(event: Dictionary, fallback_epoch: String = "") -> Dictionary:
	var event_type := str(event.get("type", ""))
	var payload: Dictionary = event.get("payload", {}) if event.get("payload", {}) is Dictionary else {}
	var epoch := str(event.get("session_epoch", fallback_epoch)).strip_edges()
	if event_type.is_empty() or epoch.is_empty():
		return _invalid("invalid_event_identity")
	var order_key := _order_key(event, payload)
	var mutations: Array[Dictionary] = []
	if NON_PRESENTATION_EVENTS.has(event_type):
		return {"ok": true, "mutations": mutations}
	match event_type:
		"timeline_item":
			var raw_item: Variant = payload.get("item", {})
			if not (raw_item is Dictionary):
				return _invalid("missing_timeline_item")
			mutations.append({"kind": "insert", "item": raw_item.duplicate(true)})
		"agent_text_delta":
			mutations = _stream_mutations(event_type, payload, epoch, order_key, false)
		"agent_reasoning_delta":
			mutations = _stream_mutations(event_type, payload, epoch, order_key, true)
		"agent_reasoning_complete":
			mutations = _reasoning_complete_mutations(payload, epoch)
		"final":
			mutations = _final_mutations(payload, epoch, order_key)
		"submission_preview_committed":
			mutations = _preview_mutations(payload, "finalize")
		"submission_preview_discarded":
			mutations = _preview_mutations(payload, "discard")
		"tool_calls", "agent_tool_calls":
			mutations = _tool_call_mutations(payload, epoch, order_key)
		"front_tool_result":
			mutations = _front_tool_result_mutations(payload, epoch)
		"user_submitted":
			mutations.append(_insert_text_item(event, payload, epoch, order_key, "message", "user", "committed"))
		"system_message":
			mutations.append(_insert_text_item(event, payload, epoch, order_key, "system", "system", "committed"))
		"error":
			mutations.append(_insert_text_item(event, payload, epoch, order_key, "error", "error", "committed"))
		_:
			mutations.append(_insert_event_item(event, payload, epoch, order_key))
	for mutation in mutations:
		var validation := Contracts.validate_mutation(mutation)
		if not bool(validation.get("ok", false)):
			return _invalid(str(validation.get("reason", "invalid_mutation")))
	return {"ok": true, "mutations": mutations}


func _stream_mutations(event_type: String, payload: Dictionary, epoch: String, order_key: Array, reasoning: bool) -> Array[Dictionary]:
	var item_id := _message_item_id(payload, "reasoning" if reasoning else "assistant")
	order_key = _message_order_key(payload, reasoning)
	var text := str(payload.get("text", ""))
	var append_delta := bool(payload.get("append_delta", false))
	var preview_id := str(payload.get("preview_id", ""))
	var block := {
		"type": "reasoning" if reasoning else "markdown",
		"text": "",
		"header": "Thought" if reasoning else "",
		"token_count": int(payload.get("token_count", 0)),
	}
	var item := _item(
		item_id, epoch, order_key, "reasoning" if reasoning else "message",
		"assistant", [block], "provisional" if bool(payload.get("provisional", false)) else "committed",
		"streaming", "reasoning" if reasoning else "assistant", _source(payload, preview_id)
	)
	return [
		{"kind": "insert", "item": item},
		{
			"kind": "patch",
			"item_id": item_id,
			"patch": {
				"text": text,
				"append_text": append_delta,
				"block_index": 0,
				"copy_text": text,
				"status": "streaming",
				"token_count": int(payload.get("token_count", 0)),
				# 思考累计耗时（服务端每个 reasoning delta 附带），
				# 最后一条即思考总时长，供 committed 态显示 "Thought for x.xs"。
				"elapsed_ms": int(payload.get("elapsed_ms", 0)),
			}
		},
	]


func _reasoning_complete_mutations(payload: Dictionary, epoch: String) -> Array[Dictionary]:
	## reasoning 段结束信号：把该 Thinking 块 finalize，header 随即切换为
	## "Thought for x.xs"。与 submission preview 边界的 finalize 幂等（重复 finalize 无害）。
	var item_id := _message_item_id(payload, "reasoning")
	return [
		{"kind": "finalize", "item_id": item_id, "status": "complete"},
	]


func _final_mutations(payload: Dictionary, epoch: String, order_key: Array) -> Array[Dictionary]:
	var item_id := _message_item_id(payload, "assistant")
	order_key = _message_order_key(payload, false)
	var text := str(payload.get("text", ""))
	var item := _item(item_id, epoch, order_key, "message", "assistant", [{"type": "markdown", "text": text}], "committed", "complete", "assistant", _source(payload))
	return [
		{"kind": "insert", "item": item},
		{"kind": "patch", "item_id": item_id, "patch": {"text": text, "append_text": false, "block_index": 0, "copy_text": text}},
		{"kind": "finalize", "item_id": item_id, "status": "complete"},
	]


func _preview_mutations(payload: Dictionary, kind: String) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	var raw_ids: Variant = payload.get("preview_ids", [])
	if not (raw_ids is Array):
		return result
	var reason := str(payload.get("reason", ""))
	for raw_id in raw_ids:
		var preview_id := str(raw_id).strip_edges()
		if not preview_id.is_empty():
			# status 与 reason 双通道：status 驱动生命周期状态机，
			# reason 供渲染层展示失败原因（如 agent_turn_budget_exhausted）
			result.append(
				{"kind": kind, "preview_id": preview_id, "status": reason, "reason": reason}
			)
	return result


func _tool_call_mutations(payload: Dictionary, epoch: String, order_key: Array) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	var raw_calls: Variant = payload.get("calls", [])
	if not (raw_calls is Array):
		return result
	for index in range(raw_calls.size()):
		var raw_call: Variant = raw_calls[index]
		if not (raw_call is Dictionary):
			continue
		var call: Dictionary = raw_call
		var tool_use_id := str(call.get("id", call.get("tool_use_id", "%s" % index)))
		var source := _source(payload)
		source["tool_use_id"] = tool_use_id
		var item := _item(
			"tool:%s" % tool_use_id, epoch, [str(call.get("frame_id", payload.get("frame_id", "root"))), "tool:%s" % tool_use_id, 2], "tool_result", "tool",
			[{"type": "tool", "call": call.duplicate(true), "result": {}, "render_kind": str(call.get("render_kind", ""))}],
			"provisional", "pending", "tool", source
		)
		result.append({"kind": "insert", "item": item})
	return result


func _front_tool_result_mutations(payload: Dictionary, epoch: String) -> Array[Dictionary]:
	var call: Dictionary = payload.get("call", {}) if payload.get("call", {}) is Dictionary else {}
	var result: Dictionary = payload.get("result", {}) if payload.get("result", {}) is Dictionary else {}
	var tool_use_id := str(payload.get("tool_use_id", call.get("id", "")))
	var source := _source(payload)
	source["tool_use_id"] = tool_use_id
	var item_id := "tool:%s" % tool_use_id
	var blocks := [{"type": "tool", "call": call.duplicate(true), "result": result.duplicate(true)}]
	var item := _item(
		item_id,
		epoch,
		[str(payload.get("frame_id", "root")), "tool:%s" % tool_use_id, 2],
		"tool_result",
		"tool",
		blocks,
		"provisional",
		str(payload.get("status", result.get("status", ""))),
		"tool_result",
		source
	)
	return [
		{"kind": "insert", "item": item},
		{"kind": "patch", "item_id": item_id, "patch": {"content_blocks": blocks, "status": str(payload.get("status", result.get("status", "")))}},
		{"kind": "finalize", "item_id": item_id, "status": str(payload.get("status", result.get("status", "complete")))},
	]


func _insert_text_item(event: Dictionary, payload: Dictionary, epoch: String, order_key: Array, kind: String, role: String, lifecycle: String) -> Dictionary:
	var text := str(payload.get("text", ""))
	var item_id := _message_item_id(payload, "user") if role == "user" else str(event.get("item_id", payload.get("item_id", "event:%s" % str(event.get("event_id", event.get("seq", order_key.hash()))))))
	if role == "user":
		order_key = _message_order_key(payload, false)
	return {"kind": "insert", "item": _item(item_id, epoch, order_key, kind, role, [{"type": "markdown", "text": text}], lifecycle, str(payload.get("status", "")), role, _source(payload), text)}


func _insert_event_item(event: Dictionary, payload: Dictionary, epoch: String, order_key: Array) -> Dictionary:
	var event_type := str(event.get("type", "unknown"))
	var item_id := str(event.get("item_id", payload.get("item_id", "event:%s" % str(event.get("event_id", event.get("seq", order_key.hash()))))))
	var source := _source(payload)
	if payload.has("tool_use_id"):
		source["tool_use_id"] = str(payload.get("tool_use_id", ""))
	var kind := "tool_result" if event_type in ["server_tool_result", "front_tool_result"] else "system"
	var role := "tool" if kind == "tool_result" else "system"
	return {"kind": "insert", "item": _item(item_id, epoch, order_key, kind, role, [{"type": "event", "event_type": event_type, "payload": payload.duplicate(true)}], "committed", str(payload.get("status", "")), kind, source)}


func _item(item_id: String, epoch: String, order_key: Array, kind: String, role: String, blocks: Array, lifecycle: String, status: String, style_token: String, source: Dictionary, copy_text: String = "") -> Dictionary:
	return {
		"schema_version": Contracts.SCHEMA_VERSION,
		"item_id": item_id,
		"session_epoch": epoch,
		"order_key": order_key.duplicate(true),
		"kind": kind,
		"role": role,
		"content_blocks": blocks.duplicate(true),
		"lifecycle": lifecycle,
		"status": status,
		"copy_text": copy_text,
		"style_token": style_token,
		"source": source.duplicate(true),
	}


func _message_item_id(payload: Dictionary, channel: String) -> String:
	var explicit := str(payload.get("item_id", "")).strip_edges()
	if not explicit.is_empty():
		return explicit
	var message_id := str(payload.get("message_id", "")).strip_edges()
	if message_id.is_empty():
		var raw_index: Variant = payload.get("message_index", payload.get("timeline_message_index", 0))
		# JSON 数字在 Godot 中解析为 float；整数值必须规整为 int 字符串，
		# 否则 "f1:2.0" 与 delta 注入的字符串 "f1:2" 失配（item_not_found）
		var index_text := str(int(raw_index)) if raw_index is int or (raw_index is float and float(raw_index) == floorf(float(raw_index))) else str(raw_index)
		message_id = "%s:%s" % [str(payload.get("frame_id", "root")), index_text]
	return "%s:%s" % [channel, message_id]


func _source(payload: Dictionary, preview_id: String = "") -> Dictionary:
	return {
		"frame_id": str(payload.get("frame_id", "")),
		"message_id": str(payload.get("message_id", "")),
		"message_index": payload.get("message_index", payload.get("timeline_message_index", -1)),
		"turn_id": str(payload.get("turn_id", "")),
		"tool_use_id": str(payload.get("tool_use_id", "")),
		"artifact_id": str(payload.get("artifact_id", "")),
		"preview_id": preview_id if not preview_id.is_empty() else str(payload.get("preview_id", "")),
	}


func _order_key(event: Dictionary, payload: Dictionary) -> Array:
	var explicit: Variant = event.get("order_key", payload.get("order_key", []))
	if explicit is Array and not explicit.is_empty():
		return explicit.duplicate(true)
	return [int(event.get("seq", payload.get("history_index", 0))), int(payload.get("timeline_message_index", payload.get("message_index", 0))), int(payload.get("stream_segment", 0))]


func _message_order_key(payload: Dictionary, reasoning: bool) -> Array:
	return [
		str(payload.get("frame_order", payload.get("frame_id", "root"))),
		int(payload.get("timeline_message_index", payload.get("message_index", 0))),
		0 if reasoning else 1,
	]


func _invalid(reason: String) -> Dictionary:
	return {"ok": false, "reason": reason, "mutations": []}
