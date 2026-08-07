@tool
extends RefCounted

## Explicit frontend ownership boundaries. Concrete services/controllers depend on
## these operations instead of assigning one another's transport or Session fields.


class SubmissionClientPort extends RefCounted:
	signal response_received(response: Dictionary)
	signal request_failed(problem: Dictionary)

	func submit_user(_command: Dictionary) -> void:
		push_error("SubmissionClientPort.submit_user is abstract")

	func submit_tool_results(_command: Dictionary) -> void:
		push_error("SubmissionClientPort.submit_tool_results is abstract")

	func reset(_command: Dictionary) -> void:
		push_error("SubmissionClientPort.reset is abstract")


class EventSocketPort extends RefCounted:
	signal protocol_message_received(message: Dictionary)
	signal socket_closed(problem: Dictionary)

	func connect_stream(_resume: Dictionary) -> void:
		push_error("EventSocketPort.connect_stream is abstract")

	func acknowledge(_session_epoch: String, _accepted_seq: int) -> void:
		push_error("EventSocketPort.acknowledge is abstract")

	func close_stream() -> void:
		push_error("EventSocketPort.close_stream is abstract")


class EventAcceptorPort extends RefCounted:
	func accept(_message: Dictionary) -> Dictionary:
		push_error("EventAcceptorPort.accept is abstract")
		return {"accepted": false, "events": []}


class ChatControllerPort extends RefCounted:
	signal presentation_event(event: Dictionary)
	signal presentation_state_changed(snapshot: Dictionary)

	func send_user_text(_text: String, _context: Dictionary) -> void:
		push_error("ChatControllerPort.send_user_text is abstract")

	func interrupt() -> void:
		push_error("ChatControllerPort.interrupt is abstract")


class ChatPresentationPort extends RefCounted:
	func present_event(_event: Dictionary) -> void:
		push_error("ChatPresentationPort.present_event is abstract")

	func present_state(_snapshot: Dictionary) -> void:
		push_error("ChatPresentationPort.present_state is abstract")
