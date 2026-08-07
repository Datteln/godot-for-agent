class_name ToolApprovalController
extends RefCounted

## Owns one suspended frontend-tool batch and preserves assistant-declared order.

var _confirm_calls: Array = []
var _leading_results: Array = []
var _ordered_calls: Array = []


func prepare(confirm_calls: Array, leading_results: Array, ordered_calls: Array) -> void:
	_confirm_calls = confirm_calls.duplicate(true)
	_leading_results = leading_results.duplicate(true)
	_ordered_calls = ordered_calls.duplicate(true)


func clear() -> void:
	_confirm_calls.clear()
	_leading_results.clear()
	_ordered_calls.clear()


func leading_results() -> Array:
	return _leading_results.duplicate(true)


func ordered_calls() -> Array:
	return _ordered_calls.duplicate(true)


func confirmation_index(tool_use_id: String) -> int:
	for index in range(_confirm_calls.size()):
		var call = _confirm_calls[index]
		if call is Dictionary and str(call.get("id", "")) == tool_use_id:
			return index
	return -1
