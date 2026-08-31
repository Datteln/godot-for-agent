## 地图选择描述的二维/三维分类回归测试。
extends SceneTree

const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")

var _failures := 0
var _checks := 0


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _init() -> void:
	_check_selected_node(TileMapLayer.new(), "TileMapLayer", 2, ["x", "y", "width", "height"])
	var legacy := ClassDB.instantiate("TileMap") as Node
	_check(legacy != null, "legacy TileMap remains available")
	if legacy != null:
		_check_selected_node(legacy, "TileMap", 2, ["x", "y", "width", "height"])
	_check_selected_node(GridMap.new(), "GridMap", 3, ["x", "y", "z", "width", "height", "depth"])
	var unsupported := MapTools._unsupported_selection_result([])
	_check(not bool(unsupported.get("ok", true)), "unsupported selection is unsuccessful")
	_check(str(unsupported.get("error_code", "")) == "unsupported_selection", "unsupported selection has typed error")
	print("dimension-aware map selection checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


func _check_selected_node(node: Node, expected_type: String, expected_dimension: int, expected_bounds: Array) -> void:
	var result := MapTools._describe_selected_map_node(node, "Map", false)
	_check(bool(result.get("ok", false)), expected_type + " selection succeeds")
	_check(str(result.get("type", "")) == expected_type, expected_type + " type is preserved")
	_check(int(result.get("dimension", 0)) == expected_dimension, expected_type + " dimension is reported")
	_check(result.get("region_bounds_fields", []) == expected_bounds, expected_type + " bounds guidance matches dimension")
	_check(bool(result.get("map_layer_applicable", false)) == (expected_dimension == 2), expected_type + " map_layer applicability is reported")
