class_name ChatTimelineContracts
extends RefCounted

## Canonical Chat Timeline 的封闭、可序列化合同与校验入口。

const SCHEMA_VERSION := 1
const ITEM_KINDS := {
	"message": true,
	"reasoning": true,
	"system": true,
	"error": true,
	"tool_call": true,
	"tool_result": true,
	"preview": true,
}
const ROLES := {"user": true, "assistant": true, "system": true, "error": true, "tool": true}
const BLOCK_TYPES := {"markdown": true, "plain_text": true, "reasoning": true, "event": true, "tool": true, "code": true}
const LIFECYCLES := {"provisional": true, "committed": true, "discarded": true}
const MUTATION_KINDS := {
	"insert": true,
	"patch": true,
	"finalize": true,
	"discard": true,
	"remove": true,
	"prepend_page": true,
	"reset_epoch": true,
}


static func validate_item(item: Dictionary) -> Dictionary:
	var item_id := str(item.get("item_id", "")).strip_edges()
	if item_id.is_empty():
		return _invalid("missing_item_id")
	if str(item.get("session_epoch", "")).strip_edges().is_empty():
		return _invalid("missing_session_epoch")
	if not ITEM_KINDS.has(str(item.get("kind", ""))):
		return _invalid("invalid_item_kind")
	if not ROLES.has(str(item.get("role", ""))):
		return _invalid("invalid_item_role")
	if not LIFECYCLES.has(str(item.get("lifecycle", ""))):
		return _invalid("invalid_item_lifecycle")
	var order_key: Variant = item.get("order_key", [])
	if not (order_key is Array) or order_key.is_empty():
		return _invalid("invalid_order_key")
	var blocks: Variant = item.get("content_blocks", [])
	if not (blocks is Array) or blocks.is_empty():
		return _invalid("missing_content_blocks")
	for raw_block in blocks:
		if not (raw_block is Dictionary):
			return _invalid("invalid_content_block")
		if not BLOCK_TYPES.has(str(raw_block.get("type", ""))):
			return _invalid("invalid_content_block_type")
		if _contains_object(raw_block):
			return _invalid("content_block_not_serializable")
	if not (item.get("source", {}) is Dictionary):
		return _invalid("invalid_source_identity")
	if _contains_object(item):
		return _invalid("item_not_serializable")
	return {"ok": true, "item": normalize_item(item)}


static func normalize_item(item: Dictionary) -> Dictionary:
	var normalized := item.duplicate(true)
	normalized["schema_version"] = SCHEMA_VERSION
	normalized["item_id"] = str(normalized.get("item_id", ""))
	normalized["session_epoch"] = str(normalized.get("session_epoch", ""))
	normalized["kind"] = str(normalized.get("kind", "system"))
	normalized["role"] = str(normalized.get("role", "system"))
	normalized["lifecycle"] = str(normalized.get("lifecycle", "committed"))
	normalized["status"] = str(normalized.get("status", ""))
	normalized["copy_text"] = str(normalized.get("copy_text", ""))
	normalized["style_token"] = str(normalized.get("style_token", normalized.get("role", "system")))
	normalized["source"] = normalized.get("source", {}).duplicate(true)
	var normalized_blocks: Array = []
	for raw_block in normalized.get("content_blocks", []):
		normalized_blocks.append(raw_block.duplicate(true))
	normalized["content_blocks"] = normalized_blocks
	return normalized


static func validate_mutation(mutation: Dictionary) -> Dictionary:
	var kind := str(mutation.get("kind", ""))
	if not MUTATION_KINDS.has(kind):
		return _invalid("unknown_mutation")
	if kind == "reset_epoch":
		if str(mutation.get("session_epoch", "")).strip_edges().is_empty():
			return _invalid("missing_session_epoch")
		return {"ok": true}
	if kind == "prepend_page":
		var items: Variant = mutation.get("items", [])
		if not (items is Array):
			return _invalid("invalid_prepend_page")
		for raw_item in items:
			if not (raw_item is Dictionary) or not bool(validate_item(raw_item).get("ok", false)):
				return _invalid("invalid_prepend_item")
		return {"ok": true}
	if kind == "insert":
		var item: Variant = mutation.get("item", {})
		if not (item is Dictionary):
			return _invalid("missing_insert_item")
		return validate_item(item)
	if str(mutation.get("item_id", "")).strip_edges().is_empty() and str(mutation.get("preview_id", "")).strip_edges().is_empty():
		return _invalid("missing_mutation_identity")
	if kind == "patch" and not (mutation.get("patch", {}) is Dictionary):
		return _invalid("invalid_patch")
	return {"ok": true}


static func _contains_object(value: Variant) -> bool:
	if value is Object:
		return true
	if value is Dictionary:
		for child in value.values():
			if _contains_object(child):
				return true
	elif value is Array:
		for child in value:
			if _contains_object(child):
				return true
	return false


static func _invalid(reason: String) -> Dictionary:
	return {"ok": false, "reason": reason}
