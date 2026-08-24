extends SceneTree

const ConfigMigrations = preload("res://addons/ai_agent/config/config_migrations.gd")

var _failures := 0
var _assertions := 0


func _init() -> void:
	_check(ConfigMigrations.normalize_permission_mode("full_access") == "auto_approve", "legacy full_access migrates to auto_approve")
	_check(ConfigMigrations.normalize_permission_mode("read_only") == "read_only", "valid permission mode is preserved")
	_check(ConfigMigrations.normalize_permission_mode("unexpected") == "default", "unknown permission mode safely falls back to default")
	_check(float(ConfigMigrations.DEFAULTS["ai_agent/chat_request_timeout_sec"]) == 360.0, "chat idle timeout default is 360 seconds")
	quit(1 if _failures > 0 else 0)


func _check(condition: bool, label: String) -> void:
	_assertions += 1
	if not condition:
		_failures += 1
		push_error("assertion failed: " + label)
	if _assertions == 4 and _failures == 0:
		print("PASS: test_config_migrations (%d assertions)" % _assertions)
