extends SceneTree

## hide-tool-input-json-in-timeline 的渲染回归测试（headless）。
## 覆盖：compact 模式不渲染入参 JSON；默认模式与 diff 预览不变。

const ToolPreviewRenderer = preload("res://addons/ai_agent/ui/tool_preview_renderer.gd")

var failures: Array = []


func _initialize() -> void:
	_test_json_compact_hides_input()
	_test_json_default_keeps_input()
	_test_list_compact_hides_input()
	_test_diff_preview_unchanged()
	if failures.is_empty():
		print("ALL_GDSCRIPT_TESTS_PASSED")
		quit(0)
	else:
		print("GDSCRIPT_TEST_FAILURES:")
		for entry in failures:
			print("  - ", entry)
		quit(1)


func _check(condition: bool, name: String) -> void:
	if not condition:
		failures.append(name)


func _test_json_compact_hides_input() -> void:
	var call := {"name": "read_scene_tree", "input": {}}
	var node := ToolPreviewRenderer.render_call(call, {}, true)
	_check(node.get_child_count() == 1, "json_compact_title_only_empty_input")
	var call2 := {"name": "describe_map_region", "input": {"x": 41, "y": -13}}
	var node2 := ToolPreviewRenderer.render_call(call2, {}, true)
	_check(node2.get_child_count() == 1, "json_compact_title_only_nonempty_input")


func _test_json_default_keeps_input() -> void:
	var call := {"name": "describe_map_region", "input": {"x": 41, "y": -13}}
	var node := ToolPreviewRenderer.render_call(call, {})
	_check(node.get_child_count() == 2, "json_default_shows_input")


func _test_list_compact_hides_input() -> void:
	var call := {"name": "add_node", "input": {"type": "Node2D", "name": "X"}}
	var compact := ToolPreviewRenderer.render_call(call, {}, true)
	_check(compact.get_child_count() == 1, "list_compact_title_only")
	var full := ToolPreviewRenderer.render_call(call, {})
	_check(full.get_child_count() == 2, "list_default_shows_input")


func _test_diff_preview_unchanged() -> void:
	var call := {"name": "apply_text_edit", "input": {"path": "res://a.gd", "old_string": "x", "new_string": "y"}}
	var compact := ToolPreviewRenderer.render_call(call, {}, true)
	var full := ToolPreviewRenderer.render_call(call, {})
	_check(compact.get_child_count() == 2 and full.get_child_count() == 2, "diff_preview_in_both_modes")