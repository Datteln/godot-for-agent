extends SceneTree

const HistoryController = preload("res://addons/ai_agent/controllers/history_controller.gd")
const RecoveryController = preload("res://addons/ai_agent/controllers/recovery_controller.gd")
const StreamingController = preload("res://addons/ai_agent/controllers/chat_streaming_controller.gd")
const SubmissionController = preload("res://addons/ai_agent/controllers/submission_controller.gd")
const ToolApprovalController = preload("res://addons/ai_agent/controllers/tool_approval_controller.gd")


class FakeClient extends Node:
	var calls: Array = []

	func send_user_message(text: String, context: Dictionary, model: Variant) -> void:
		calls.append(["user", text, context, model])

	func send_tool_results(results: Array, model: Variant) -> void:
		calls.append(["tools", results, model])

	func discard_pending() -> void:
		calls.append(["discard"])

	func reset_session() -> void:
		calls.append(["reset"])

	func interrupt_current() -> void:
		calls.append(["interrupt"])

	func fetch_session_history(limit := 40, before := 0) -> void:
		calls.append(["history", limit, before])

	func fetch_chat_snapshot() -> void:
		calls.append(["snapshot"])


func _init() -> void:
	var client := FakeClient.new()
	var submission := SubmissionController.new()
	submission.configure(client)
	submission.submit_user("hello", {"paths": []}, null)
	submission.submit_tool_results([{"tool_use_id": "c1"}], "model")
	submission.interrupt()
	if client.calls.size() != 3:
		_fail("submission controller did not own command dispatch")
		return

	var history := HistoryController.new()
	history.configure(client)
	history.fetch_page(20, 41)
	history.fetch_snapshot()
	if client.calls[-2] != ["history", 20, 41] or client.calls[-1] != ["snapshot"]:
		_fail("history controller command identity changed")
		return

	var approval := ToolApprovalController.new()
	approval.prepare(
		[{"id": "confirm"}],
		[{"tool_use_id": "leading"}],
		[{"id": "confirm"}, {"id": "silent"}]
	)
	if approval.confirmation_index("confirm") != 0:
		_fail("approval controller lost confirmation identity")
		return
	if approval.ordered_calls().size() != 2 or approval.leading_results().size() != 1:
		_fail("approval controller changed declared order")
		return

	var recovery := RecoveryController.new()
	recovery.begin_reset(3, "old", "new")
	var reset := recovery.failure_recovery()
	if reset.get("state") != 3 or reset.get("previous_session_id") != "old":
		_fail("recovery controller lost rollback identity")
		return

	var streaming := StreamingController.new()
	streaming.enqueue({"seq": 2})
	streaming.enqueue({"seq": 1})
	var batch := streaming.take_batch(1, 100)
	if batch.size() != 1 or int(batch[0].get("seq", 0)) != 1 or not streaming.has_pending():
		_fail("stream controller did not preserve ordered bounded delivery")
		return

	client.free()
	quit()


func _fail(message: String) -> void:
	push_error(message)
	quit(1)
