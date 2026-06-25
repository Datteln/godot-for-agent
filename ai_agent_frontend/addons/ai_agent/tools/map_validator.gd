@tool
extends RefCounted


static func validate_region(
	filled: Dictionary,
	region: Dictionary,
	dimension: int,
	start_value,
	goal_value,
	walkable_is_filled: bool
) -> Dictionary:
	var total := int(region["width"]) * int(region["height"]) * int(region["depth"])
	var filled_count := filled.size()
	var issues: Array = []
	var result := {
		"filled_cells": filled_count,
		"empty_cells": total - filled_count,
		"issues": issues,
		"passed": true,
		"repair_plan": [],
	}
	if start_value == null or goal_value == null:
		return result

	var start := coord_from_input(start_value, dimension)
	var goal := coord_from_input(goal_value, dimension)
	var connectivity := check_connectivity(filled, start, goal, region, dimension, walkable_is_filled)
	result["connectivity"] = connectivity
	if not bool(connectivity.get("reachable", false)):
		issues.append("goal is not reachable from start within the region")
		result["passed"] = false
		result["repair_plan"] = build_connectivity_repair_plan(start, goal, region, dimension, walkable_is_filled)
	return result


static func build_connectivity_repair_plan(
	start: Vector3i,
	goal: Vector3i,
	region: Dictionary,
	dimension: int,
	walkable_is_filled: bool
) -> Array:
	if not in_region(start, region) or not in_region(goal, region):
		return []
	var path := manhattan_path(start, goal, dimension)
	var cells: Array = []
	for coords in path:
		if in_region(coords, region):
			cells.append(coord_payload(coords, dimension))
	return [{
		"type": "connectivity_corridor",
		"action": "fill" if walkable_is_filled else "erase",
		"cells": cells,
		"cells_count": cells.size(),
		"note": "Apply this corridor to connect start and goal, then validate again.",
	}]


static func check_connectivity(
	filled: Dictionary,
	start: Vector3i,
	goal: Vector3i,
	region: Dictionary,
	dimension: int,
	walkable_is_filled: bool
) -> Dictionary:
	if not in_region(start, region) or not in_region(goal, region):
		return {"reachable": false, "reason": "start or goal is outside the validated region"}
	if not is_walkable(filled, start, walkable_is_filled):
		return {"reachable": false, "reason": "start cell is not walkable"}
	if not is_walkable(filled, goal, walkable_is_filled):
		return {"reachable": false, "reason": "goal cell is not walkable"}
	var visited := {}
	var queue: Array = [start]
	visited[coord_key(start)] = 0
	var cursor := 0
	while cursor < queue.size():
		var current: Vector3i = queue[cursor]
		cursor += 1
		if current == goal:
			return {"reachable": true, "distance": int(visited[coord_key(current)])}
		for offset in neighbor_offsets(dimension):
			var next: Vector3i = current + offset
			var next_key := coord_key(next)
			if visited.has(next_key) or not in_region(next, region):
				continue
			if not is_walkable(filled, next, walkable_is_filled):
				continue
			visited[next_key] = int(visited[coord_key(current)]) + 1
			queue.append(next)
	return {"reachable": false, "reason": "no walkable path connects start and goal", "visited": visited.size()}


static func manhattan_path(start: Vector3i, goal: Vector3i, dimension: int) -> Array:
	var path: Array = []
	var current := start
	path.append(current)
	while current.x != goal.x:
		current.x += 1 if goal.x > current.x else -1
		path.append(current)
	while current.y != goal.y:
		current.y += 1 if goal.y > current.y else -1
		path.append(current)
	if dimension == 3:
		while current.z != goal.z:
			current.z += 1 if goal.z > current.z else -1
			path.append(current)
	return path


static func region_from_input(input: Dictionary, dimension: int) -> Dictionary:
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var z := int(input.get("z", 0)) if dimension == 3 else 0
	var width := max(1, int(input.get("width", 1)))
	var height := max(1, int(input.get("height", 1)))
	var depth := max(1, int(input.get("depth", 1))) if dimension == 3 else 1
	return {
		"x": x, "y": y, "z": z,
		"width": width, "height": height, "depth": depth,
		"min_x": x, "max_x": x + width - 1,
		"min_y": y, "max_y": y + height - 1,
		"min_z": z, "max_z": z + depth - 1,
	}


static func coord_from_input(value, dimension: int) -> Vector3i:
	if value is Dictionary:
		return Vector3i(
			int(value.get("x", 0)),
			int(value.get("y", 0)),
			int(value.get("z", 0)) if dimension == 3 else 0
		)
	return Vector3i.ZERO


static func coord_payload(coords: Vector3i, dimension: int) -> Dictionary:
	if dimension == 3:
		return {"x": coords.x, "y": coords.y, "z": coords.z}
	return {"x": coords.x, "y": coords.y}


static func coord_key(coords: Vector3i) -> String:
	return "%d,%d,%d" % [coords.x, coords.y, coords.z]


static func in_region(coords: Vector3i, region: Dictionary) -> bool:
	return coords.x >= int(region["min_x"]) and coords.x <= int(region["max_x"]) \
		and coords.y >= int(region["min_y"]) and coords.y <= int(region["max_y"]) \
		and coords.z >= int(region["min_z"]) and coords.z <= int(region["max_z"])


static func is_walkable(filled: Dictionary, coords: Vector3i, walkable_is_filled: bool) -> bool:
	var is_filled := filled.has(coord_key(coords))
	return is_filled if walkable_is_filled else not is_filled


static func neighbor_offsets(dimension: int) -> Array:
	if dimension == 3:
		return [
			Vector3i(1, 0, 0), Vector3i(-1, 0, 0),
			Vector3i(0, 1, 0), Vector3i(0, -1, 0),
			Vector3i(0, 0, 1), Vector3i(0, 0, -1),
		]
	return [Vector3i(1, 0, 0), Vector3i(-1, 0, 0), Vector3i(0, 1, 0), Vector3i(0, -1, 0)]
