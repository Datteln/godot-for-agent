@tool
extends EditorPlugin

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")
const FrontendLogger = preload("res://addons/ai_agent/logging/frontend_logger.gd")
const ServiceManager = preload("res://addons/ai_agent/service/service_manager.gd")
const AgentStateStore = preload("res://addons/ai_agent/state/agent_state_store.gd")
const UnifiedUndoManager = preload("res://addons/ai_agent/undo/unified_undo_manager.gd")
const ChatPanel = preload("res://addons/ai_agent/ui/chat_panel.gd")

var _service: Node
var _state_store: Node
var _undo_manager: Node
var _chat_panel: Control


func _enter_tree() -> void:
	ConfigMigrations.apply_defaults(get_editor_interface())
	FrontendLogger.info(get_editor_interface(), "Plugin", "Entering AI Agent plugin.")

	_state_store = AgentStateStore.new()
	_state_store.name = "AgentStateStore"
	add_child(_state_store)

	_service = ServiceManager.new()
	_service.name = "AgentServiceManager"
	_service.editor_interface = get_editor_interface()
	add_child(_service)

	_undo_manager = UnifiedUndoManager.new()
	_undo_manager.name = "UnifiedUndoManager"
	_undo_manager.undo_redo = get_undo_redo()
	add_child(_undo_manager)

	_chat_panel = ChatPanel.new()
	_chat_panel.name = "AIAgentDock"
	_chat_panel.editor_interface = get_editor_interface()
	_chat_panel.service = _service
	_chat_panel.state_store = _state_store
	_chat_panel.undo_manager = _undo_manager
	add_control_to_dock(DOCK_SLOT_RIGHT_BL, _chat_panel)

	_service.start()


func _exit_tree() -> void:
	FrontendLogger.info(get_editor_interface(), "Plugin", "Exiting AI Agent plugin.")
	if _chat_panel != null:
		remove_control_from_docks(_chat_panel)
		_chat_panel.queue_free()
		_chat_panel = null

	if _service != null:
		_service.stop()
		_service.queue_free()
		_service = null

	if _undo_manager != null:
		_undo_manager.queue_free()
		_undo_manager = null

	if _state_store != null:
		_state_store.queue_free()
		_state_store = null
