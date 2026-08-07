class_name HistoryController
extends RefCounted

## Owns bounded history/snapshot command dispatch; it never polls continuously.

var _client: Node


func configure(client: Node) -> void:
	_client = client


func fetch_initial() -> void:
	_require_client()
	_client.fetch_session_history()


func fetch_page(limit: int, before: int) -> void:
	_require_client()
	_client.fetch_session_history(limit, before)


func fetch_snapshot() -> void:
	_require_client()
	_client.fetch_chat_snapshot()


func _require_client() -> void:
	if _client == null:
		push_error("HistoryController is not configured")
