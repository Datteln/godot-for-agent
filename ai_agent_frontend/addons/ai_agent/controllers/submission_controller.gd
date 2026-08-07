class_name SubmissionController
extends RefCounted

## Owns HTTP command dispatch. Responses remain acknowledgements; presentation uses WebSocket.

var _client: Node


func configure(client: Node) -> void:
	_client = client


func submit_user(text: String, context: Dictionary, model: Variant) -> void:
	_require_client()
	_client.send_user_message(text, context, model)


func submit_tool_results(results: Array, model: Variant) -> void:
	_require_client()
	_client.send_tool_results(results, model)


func discard_pending() -> void:
	_require_client()
	_client.discard_pending()


func reset_session() -> void:
	_require_client()
	_client.reset_session()


func interrupt() -> void:
	_require_client()
	_client.interrupt_current()


func _require_client() -> void:
	if _client == null:
		push_error("SubmissionController is not configured")
