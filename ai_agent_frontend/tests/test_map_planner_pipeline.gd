extends SceneTree

const MapTools = preload("res://addons/ai_agent/tools/map_tools.gd")
const ToolExecutor = preload("res://addons/ai_agent/tools/tool_executor.gd")
const REGISTRY_PATH := "res://.ai_agent_service/map_agent/resource_registry.json"

var failures: Array[String] = []
var original_registry_exists := false
var original_registry_text := ""


class FakeRevisionTracker:
	extends Node

	var revision := 0

	func current_revision(_key: String) -> int:
		return revision


func _init() -> void:
	_capture_registry()
	if _write_fixture_registry():
		_test_snapshot_revision_and_completeness_gates()
		_test_frontier_recompute_failure_blocks_execution()
		_test_resource_registry_drift()
		_test_shadow_parity_2d_tilemap_layer()
		_test_shadow_parity_multilayer_tilemap()
		_test_shadow_parity_3d_gridmap()
		_test_legacy_raw_batch_path_removed()
		_test_pre_mutation_approval_rejection()
	_restore_registry()
	if failures.is_empty():
		print("MAP_PLANNER_PIPELINE_TESTS_PASSED")
		quit(0)
		return
	for failure in failures:
		push_error(failure)
	quit(1)


func _check(condition: bool, message: String) -> void:
	if not condition:
		failures.append(message)


func _capture_registry() -> void:
	var absolute := ProjectSettings.globalize_path(REGISTRY_PATH)
	original_registry_exists = FileAccess.file_exists(absolute)
	if original_registry_exists:
		original_registry_text = FileAccess.get_file_as_string(absolute)


func _fixture_registry() -> Dictionary:
	return {
		"fixture_2d": {
			"kind": "tile",
			"footprint": {"width": 1, "height": 1},
			"required_cells": 1,
			"source_id": 4,
			"atlas_coords": {"x": 6, "y": 8},
			"alternative_tile": 2,
			"tags": ["fixture"],
		},
		"fixture_3d": {
			"kind": "mesh",
			"mode": "3d",
			"footprint": {"width": 1, "height": 1, "depth": 1},
			"required_cells": 1,
			"item": 7,
			"orientation": 3,
			"tags": ["fixture"],
		},
	}


func _write_fixture_registry() -> bool:
	var absolute := ProjectSettings.globalize_path(REGISTRY_PATH)
	var directory_error := DirAccess.make_dir_recursive_absolute(absolute.get_base_dir())
	if directory_error != OK and directory_error != ERR_ALREADY_EXISTS:
		failures.append("could not create temporary resource-registry directory")
		return false
	var file := FileAccess.open(absolute, FileAccess.WRITE)
	if file == null:
		failures.append("could not open temporary resource registry")
		return false
	file.store_string(JSON.stringify(_fixture_registry(), "\t"))
	file.close()
	return true


func _restore_registry() -> void:
	var absolute := ProjectSettings.globalize_path(REGISTRY_PATH)
	if original_registry_exists:
		var file := FileAccess.open(absolute, FileAccess.WRITE)
		if file == null:
			failures.append("could not restore original resource registry")
			return
		file.store_string(original_registry_text)
		file.close()
		return
	if FileAccess.file_exists(absolute):
		var remove_error := DirAccess.remove_absolute(absolute)
		if remove_error != OK:
			failures.append("could not remove temporary resource registry")


func _snapshot_input() -> Dictionary:
	return {
		"authoritative_snapshot_id": "mapsnap-fixture",
		"authoritative_snapshot_digest": "digest-fixture",
		"authoritative_snapshot_target": "Map/Main",
		"authoritative_snapshot_layer": 2,
		"authoritative_snapshot_revision": 7,
		"authoritative_snapshot_coverage_complete": true,
		"authoritative_snapshot_traversal_complete": true,
		"authoritative_snapshot_frontier_complete": true,
		"_canonical_map_revision": 7,
	}


func _test_snapshot_revision_and_completeness_gates() -> void:
	var stale := _snapshot_input()
	stale["_canonical_map_revision"] = 8
	var stale_error := MapTools._authoritative_snapshot_error(stale, "Map/Main", 2)
	_check(
		str(stale_error.get("error_code", "")) == "authoritative_snapshot_revision_stale",
		"stale authoritative snapshot was not rejected",
	)
	var incomplete := _snapshot_input()
	incomplete["authoritative_snapshot_coverage_complete"] = false
	var coverage_error := MapTools._authoritative_snapshot_error(incomplete, "Map/Main", 2)
	_check(
		str(coverage_error.get("error_code", "")) == "authoritative_snapshot_incomplete",
		"incomplete trajectory coverage did not block validation",
	)
	_check(
		str(coverage_error.get("incomplete_field", ""))
		== "authoritative_snapshot_coverage_complete",
		"coverage failure did not identify its exact snapshot field",
	)


func _test_frontier_recompute_failure_blocks_execution() -> void:
	var input := _snapshot_input()
	input["authoritative_snapshot_frontier_complete"] = false
	var error := MapTools._authoritative_snapshot_error(input, "Map/Main", 2)
	_check(
		str(error.get("error_code", "")) == "authoritative_snapshot_incomplete",
		"missing recomputed frontier did not block execution",
	)
	_check(
		str(error.get("incomplete_field", ""))
		== "authoritative_snapshot_frontier_complete",
		"frontier recomputation failure was not typed",
	)


func _test_resource_registry_drift() -> void:
	var drifted := (_live_registry()["fixture_2d"] as Dictionary).duplicate(true)
	drifted["source_id"] = 999
	var input := {
		"_authoritative_snapshot_digest_verified": true,
		"_authoritative_resource_bindings": {"fixture_2d": drifted},
	}
	var operation := {"action": "fill", "resource": "fixture_2d"}
	var error := MapTools._snapshot_resource_binding_error(input, operation)
	_check(
		str(error.get("error_code", "")) == "resource_registry_drift",
		"resource-registry drift did not require snapshot refresh",
	)


func _compile_fixture(target: Node, dimension: int, map_layer: int, resource: String) -> Dictionary:
	var entry: Dictionary = (_live_registry()[resource] as Dictionary).duplicate(true)
	var input := {
		"_authoritative_snapshot_digest_verified": true,
		"_authoritative_resource_bindings": {resource: entry},
	}
	var result := {
		"ok": true,
		"edit_map_batches": [
			{
				"tool": "edit_map",
				"operations": [
					{
						"action": "fill",
						"x": 1,
						"y": 2,
						"z": 3,
						"width": 1,
						"height": 1,
						"depth": 1,
						"resource": resource,
					}
				],
			}
		],
	}
	return MapTools._compile_planned_resources(result, input, target, map_layer, dimension)


func _live_registry() -> Dictionary:
	var document := MapTools._read_json_resource(REGISTRY_PATH)
	var data = document.get("data", {})
	return data if data is Dictionary else {}


func _compiled_operation(result: Dictionary) -> Dictionary:
	var batches = result.get("edit_map_batches", [])
	if not (batches is Array) or (batches as Array).is_empty():
		return {}
	var batch = (batches as Array)[0]
	if not (batch is Dictionary):
		return {}
	var operations = (batch as Dictionary).get("operations", [])
	if not (operations is Array) or (operations as Array).is_empty():
		return {}
	var operation = (operations as Array)[0]
	return operation if operation is Dictionary else {}


func _check_2d_parity(result: Dictionary, fixture_name: String) -> void:
	_check(
		bool(result.get("ok", false)),
		fixture_name + " semantic compilation failed: " + str(result),
	)
	var operation := _compiled_operation(result)
	_check(int(operation.get("source_id", -1)) == 4, fixture_name + " source_id drift")
	_check(int(operation.get("atlas_x", -1)) == 6, fixture_name + " atlas_x drift")
	_check(int(operation.get("atlas_y", -1)) == 8, fixture_name + " atlas_y drift")
	_check(
		int(operation.get("alternative_tile", -1)) == 2,
		fixture_name + " alternative_tile drift",
	)


func _test_shadow_parity_2d_tilemap_layer() -> void:
	var target := TileMapLayer.new()
	var result := _compile_fixture(target, 2, 0, "fixture_2d")
	_check_2d_parity(result, "TileMapLayer fixture")
	target.free()


func _test_shadow_parity_multilayer_tilemap() -> void:
	var target := TileMap.new()
	target.add_layer(1)
	target.set_layer_name(0, "Background")
	target.set_layer_name(1, "Foreground")
	var result := _compile_fixture(target, 2, 1, "fixture_2d")
	_check_2d_parity(result, "multilayer TileMap fixture")
	target.free()


func _test_shadow_parity_3d_gridmap() -> void:
	var target := GridMap.new()
	var result := _compile_fixture(target, 3, 0, "fixture_3d")
	_check(
		bool(result.get("ok", false)),
		"GridMap semantic compilation failed: " + str(result),
	)
	var operation := _compiled_operation(result)
	_check(int(operation.get("item", -1)) == 7, "GridMap item drift")
	_check(int(operation.get("orientation", -1)) == 3, "GridMap orientation drift")
	target.free()


func _test_legacy_raw_batch_path_removed() -> void:
	var raw_operation := {
		"action": "fill",
		"source_id": 4,
		"atlas_x": 6,
		"atlas_y": 8,
	}
	var resolved := MapTools._apply_registry_fallback_to_operation(raw_operation, 2)
	_check(resolved.is_empty(), "legacy raw atlas path still resolves a semantic resource")
	var contract := MapTools._validate_operation_resource_contract(raw_operation, resolved, 2)
	_check(
		str(contract.get("error_code", "")) == "unregistered_map_resource",
		"raw atlas operation was not rejected after shadow parity",
	)


func _test_pre_mutation_approval_rejection() -> void:
	var executor = ToolExecutor.new()
	var tracker := FakeRevisionTracker.new()
	tracker.revision = 9
	executor.map_revision_tracker = tracker
	var input := {
		"target_path": "Map/Main",
		"map_layer": 2,
		"expected_revision": 8,
		"approval_id": "approval-fixture",
		"approval_batch_fingerprint": "batch-fixture",
		"approval_snapshot_id": "mapsnap-fixture",
		"approval_snapshot_digest": "digest-fixture",
		"approval_target_path": "Map/Main",
		"approval_map_layer": 2,
		"approval_expected_revision": 8,
	}
	var error: Dictionary = executor._validate_map_write_revision(input, "Map/Main")
	_check(
		str(error.get("error_code", "")) == "map_revision_conflict",
		"stale approval reached the mutation boundary",
	)
	_check(not input.has("map_transaction_id"), "pre-mutation rejection created a transaction")
	executor.free()
	tracker.free()
