extends SceneTree

const ChatEventSocket = preload("res://addons/ai_agent/service/chat_event_socket.gd")


class FakeSocket:
	var sent: Array[String] = []
	var closed := false

	func get_ready_state() -> int:
		return WebSocketPeer.STATE_OPEN

	func send_text(value: String) -> int:
		sent.append(value)
		return OK

	func close() -> void:
		closed = true


func _init() -> void:
	var client = ChatEventSocket.new()
	var fake = FakeSocket.new()
	client._socket = fake
	client._session_id = "s1"
	var received: Array[Dictionary] = []
	var gaps: Array[Dictionary] = []
	client.event_received.connect(func(event: Dictionary): received.append(event))
	client.history_gap_received.connect(func(details: Dictionary): gaps.append(details))

	client._handle_message({"version": 1, "type": "subscribed"})
	assert(client._state == client.STATE_SUBSCRIBED)
	client._handle_event({"event_id": "s1:1", "seq": 1, "type": "text", "payload": {}})
	client._handle_event({"event_id": "s1:1", "seq": 1, "type": "text", "payload": {}})
	assert(received.size() == 1)
	assert(client._highest_contiguous_seq == 1)
	assert(fake.sent.size() == 1)
	var ack = JSON.parse_string(fake.sent[0])
	assert(ack is Dictionary and ack.get("type") == "ack" and int(ack.get("seq")) == 1)

	client._handle_event({"event_id": "s1:3", "seq": 3, "type": "text", "payload": {}})
	assert(gaps.size() == 1)
	assert(fake.closed)
	assert(client._awaiting_history_hydration)
	assert(fake.sent.all(func(message: String): return not message.contains("user_message")))
	quit()
