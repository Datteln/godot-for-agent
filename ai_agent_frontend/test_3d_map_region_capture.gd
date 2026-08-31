## GridMap 三维 map_region 解析、边界校验和视觉证据元数据回归测试。
extends SceneTree

const SceneTools = preload("res://addons/ai_agent/tools/scene_tools.gd")

var _failures := 0
var _checks := 0


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures += 1
		printerr("FAIL: ", label)


func _init() -> void:
	var root := Node3D.new()
	root.name = "Root"
	var grid := GridMap.new()
	grid.name = "Grid"
	grid.cell_size = Vector3(2.0, 3.0, 4.0)
	grid.position = Vector3(10.0, 20.0, 30.0)
	root.add_child(grid)
	_run_valid_region_test(root, grid)
	_run_validation_tests(root, grid)
	_run_camera_restore_test()
	print("3D map region capture checks: %d, failures: %d" % [_checks, _failures])
	quit(1 if _failures > 0 else 0)


func _run_valid_region_test(root: Node, grid: GridMap) -> void:
	var target := {
		"type": "map_region",
		"path": "Grid",
		"cell_bounds": {"x": 2, "y": 3, "z": 4, "width": 2, "height": 1, "depth": 3},
	}
	var result := SceneTools._gridmap_region_world_aabb(grid, target, root)
	_check(bool(result.get("ok", false)), "valid GridMap region resolves")
	_check(str(result.get("capture_scope", "")) == "map_region", "GridMap capture scope is map_region")
	var bounds: AABB = result.get("world_aabb", AABB())
	_check(bounds.size.is_equal_approx(Vector3(4.0, 3.0, 12.0)), "world AABB spans requested cell cuboid")
	var facts: Dictionary = result.get("spatial_facts", {})
	var requested: Dictionary = facts.get("cell_bounds", {}).get("value", {})
	_check(int(requested.get("z", -1)) == 4 and int(requested.get("depth", 0)) == 3, "result reports requested three-dimensional bounds")
	_check(facts.has("world_aabb") and facts.has("occupancy"), "result includes bounded visual evidence metadata")
	_check(result.has("warnings"), "empty GridMap region carries an explicit visual-only warning")


func _run_validation_tests(root: Node, grid: GridMap) -> void:
	var valid_bounds := {"x": 0, "y": 0, "z": 0, "width": 1, "height": 1, "depth": 1}
	var wrong_node := SceneTools._gridmap_region_world_aabb(Node3D.new(), {"type": "map_region", "cell_bounds": valid_bounds}, root)
	_check(str(wrong_node.get("error_code", "")) == "invalid_target", "non-GridMap target is rejected")
	var missing_depth := SceneTools._gridmap_region_world_aabb(grid, {"type": "map_region", "cell_bounds": {"x": 0, "y": 0, "width": 1, "height": 1}}, root)
	_check(str(missing_depth.get("error_code", "")) == "invalid_cell_bounds", "missing z/depth is rejected")
	var invalid_size := SceneTools._gridmap_region_world_aabb(grid, {"type": "map_region", "cell_bounds": {"x": 0, "y": 0, "z": 0, "width": 0, "height": 1, "depth": 1}}, root)
	_check(str(invalid_size.get("error_code", "")) == "invalid_cell_bounds", "non-positive dimensions are rejected")
	var map_layer := SceneTools._gridmap_region_world_aabb(grid, {"type": "map_region", "map_layer": 0, "cell_bounds": valid_bounds}, root)
	_check(str(map_layer.get("error_code", "")) == "map_layer_not_applicable", "GridMap map_layer is rejected")


func _run_camera_restore_test() -> void:
	var camera := Camera3D.new()
	camera.projection = Camera3D.PROJECTION_PERSPECTIVE
	camera.fov = 63.0
	camera.size = 7.0
	var prior := Transform3D(Basis.IDENTITY, Vector3(1.0, 2.0, 3.0))
	var applied := Transform3D(Basis.IDENTITY, Vector3(10.0, 20.0, 30.0))
	camera.global_transform = applied
	var restored := SceneTools._restore_3d_camera_after_capture(camera, applied, prior, Camera3D.PROJECTION_PERSPECTIVE, 63.0, 7.0, true)
	_check(bool(restored.get("restored", false)), "shared 3D capture lifecycle restores an unchanged camera")
	_check(camera.global_transform.is_equal_approx(prior), "camera transform is restored after bounded GridMap capture")
	_check(is_equal_approx(camera.fov, 63.0) and is_equal_approx(camera.size, 7.0), "camera projection settings are restored")
