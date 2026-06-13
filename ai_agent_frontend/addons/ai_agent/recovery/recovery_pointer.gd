@tool
extends RefCounted

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")


static func should_show(editor_interface: EditorInterface) -> bool:
	return bool(ConfigMigrations.get_value(editor_interface, "ai_agent/show_recovery_prompt"))


static func project_hash() -> String:
	return ProjectSettings.globalize_path("res://").sha256_text().left(16)
