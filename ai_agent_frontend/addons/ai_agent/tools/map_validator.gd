@tool
extends RefCounted


static func validate_region(
	filled: Dictionary,
	region: Dictionary,
	dimension: int,
	start_value,
	goal_value,
	walkable_is_filled: bool,
	path_algorithm: String = "bfs",
	waypoints_value = null,
	entrances_value = null,
	exits_value = null
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
	var multi := check_multi_point_connectivity(
		filled,
		region,
		dimension,
		walkable_is_filled,
		path_algorithm,
		waypoints_value,
		entrances_value,
		exits_value
	)
	if not multi.is_empty():
		result["multi_connectivity"] = multi
		if not bool(multi.get("reachable", true)):
			issues.append("one or more entrance/exit/waypoint path constraints are not reachable")
			result["passed"] = false

	if start_value == null or goal_value == null:
		return result

	var start := coord_from_input(start_value, dimension)
	var goal := coord_from_input(goal_value, dimension)
	var connectivity := check_connectivity(filled, start, goal, region, dimension, walkable_is_filled, path_algorithm)
	result["connectivity"] = connectivity
	if not bool(connectivity.get("reachable", false)):
		issues.append("goal is not reachable from start within the region")
		result["passed"] = false
		result["repair_plan"] = build_connectivity_repair_plan(filled, start, goal, region, dimension, walkable_is_filled)
	else:
		result["path"] = connectivity.get("path", [])
	return result


static func check_multi_point_connectivity(
	filled: Dictionary,
	region: Dictionary,
	dimension: int,
	walkable_is_filled: bool,
	path_algorithm: String,
	waypoints_value,
	entrances_value,
	exits_value
) -> Dictionary:
	var waypoints := coords_array_from_input(waypoints_value, dimension)
	var entrances := coords_array_from_input(entrances_value, dimension)
	var exits := coords_array_from_input(exits_value, dimension)
	var result := {"reachable": true, "segments": [], "pairs": []}
	if not waypoints.is_empty():
		for i in range(waypoints.size() - 1):
			var segment := check_connectivity(filled, waypoints[i], waypoints[i + 1], region, dimension, walkable_is_filled, path_algorithm)
			segment["from"] = coord_payload(waypoints[i], dimension)
			segment["to"] = coord_payload(waypoints[i + 1], dimension)
			(result["segments"] as Array).append(segment)
			if not bool(segment.get("reachable", false)):
				result["reachable"] = false
	if not entrances.is_empty() and not exits.is_empty():
		for entrance in entrances:
			var reachable_exit := false
			for exit in exits:
				var pair := check_connectivity(filled, entrance, exit, region, dimension, walkable_is_filled, path_algorithm)
				pair["from"] = coord_payload(entrance, dimension)
				pair["to"] = coord_payload(exit, dimension)
				(result["pairs"] as Array).append(pair)
				reachable_exit = reachable_exit or bool(pair.get("reachable", false))
			if not reachable_exit:
				result["reachable"] = false
	if (result["segments"] as Array).is_empty() and (result["pairs"] as Array).is_empty():
		return {}
	return result


static func build_connectivity_repair_plan(
	filled: Dictionary,
	start: Vector3i,
	goal: Vector3i,
	region: Dictionary,
	dimension: int,
	walkable_is_filled: bool
) -> Array:
	if not in_region(start, region) or not in_region(goal, region):
		return []
	var path := manhattan_path(start, goal, dimension)
	var routed := find_path_astar(filled, start, goal, region, dimension, walkable_is_filled)
	if bool(routed.get("reachable", false)):
		path = []
		for point in routed.get("path", []):
			path.append(coord_from_input(point, dimension))
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
	walkable_is_filled: bool,
	path_algorithm: String = "bfs"
) -> Dictionary:
	if not in_region(start, region) or not in_region(goal, region):
		return {"reachable": false, "reason": "start or goal is outside the validated region"}
	if not is_walkable(filled, start, walkable_is_filled):
		return {"reachable": false, "reason": "start cell is not walkable"}
	if not is_walkable(filled, goal, walkable_is_filled):
		return {"reachable": false, "reason": "goal cell is not walkable"}
	if path_algorithm.to_lower() == "astar" or path_algorithm.to_lower() == "a*":
		return find_path_astar(filled, start, goal, region, dimension, walkable_is_filled)
	var visited := {}
	var came_from := {}
	var queue: Array = [start]
	visited[coord_key(start)] = 0
	var cursor := 0
	while cursor < queue.size():
		var current: Vector3i = queue[cursor]
		cursor += 1
		if current == goal:
			return {
				"reachable": true,
				"algorithm": "bfs",
				"distance": int(visited[coord_key(current)]),
				"path": _reconstruct_path_from_parents(came_from, start, goal, dimension),
			}
		for offset in neighbor_offsets(dimension):
			var next: Vector3i = current + offset
			var next_key := coord_key(next)
			if visited.has(next_key) or not in_region(next, region):
				continue
			if not is_walkable(filled, next, walkable_is_filled):
				continue
			came_from[next_key] = coord_payload(current, dimension)
			visited[next_key] = int(visited[coord_key(current)]) + 1
			queue.append(next)
	return {"reachable": false, "algorithm": "bfs", "reason": "no walkable path connects start and goal", "visited": visited.size()}


static func find_path_astar(
	filled: Dictionary,
	start: Vector3i,
	goal: Vector3i,
	region: Dictionary,
	dimension: int,
	walkable_is_filled: bool
) -> Dictionary:
	if not in_region(start, region) or not in_region(goal, region):
		return {"reachable": false, "algorithm": "astar", "reason": "start or goal is outside the validated region"}
	if not is_walkable(filled, start, walkable_is_filled):
		return {"reachable": false, "algorithm": "astar", "reason": "start cell is not walkable"}
	if not is_walkable(filled, goal, walkable_is_filled):
		return {"reachable": false, "algorithm": "astar", "reason": "goal cell is not walkable"}
	var open: Array = [start]
	var open_keys := {coord_key(start): true}
	var came_from := {}
	var g_score := {coord_key(start): 0}
	var f_score := {coord_key(start): heuristic(start, goal, dimension)}
	while not open.is_empty():
		var current_index := _lowest_score_index(open, f_score)
		var current: Vector3i = open[current_index]
		open.remove_at(current_index)
		open_keys.erase(coord_key(current))
		if current == goal:
			var path := _reconstruct_path_from_parents(came_from, start, goal, dimension)
			return {
				"reachable": true,
				"algorithm": "astar",
				"distance": int(g_score.get(coord_key(goal), path.size() - 1)),
				"path": path,
				"visited": g_score.size(),
			}
		for offset in neighbor_offsets(dimension):
			var next: Vector3i = current + offset
			var next_key := coord_key(next)
			if not in_region(next, region) or not is_walkable(filled, next, walkable_is_filled):
				continue
			var tentative_g := int(g_score.get(coord_key(current), 0)) + 1
			if tentative_g >= int(g_score.get(next_key, 2147483647)):
				continue
			came_from[next_key] = coord_payload(current, dimension)
			g_score[next_key] = tentative_g
			f_score[next_key] = tentative_g + heuristic(next, goal, dimension)
			if not open_keys.has(next_key):
				open.append(next)
				open_keys[next_key] = true
	return {"reachable": false, "algorithm": "astar", "reason": "no walkable path connects start and goal", "visited": g_score.size()}


static func heuristic(a: Vector3i, b: Vector3i, dimension: int) -> int:
	var result: int = absi(a.x - b.x) + absi(a.y - b.y)
	if dimension == 3:
		result += absi(a.z - b.z)
	return result


static func _lowest_score_index(open: Array, f_score: Dictionary) -> int:
	var best_index := 0
	var best_score := 2147483647
	for i in range(open.size()):
		var coords: Vector3i = open[i]
		var score := int(f_score.get(coord_key(coords), 2147483647))
		if score < best_score:
			best_score = score
			best_index = i
	return best_index


static func _reconstruct_path_from_parents(came_from: Dictionary, start: Vector3i, goal: Vector3i, dimension: int) -> Array:
	var current := goal
	var path: Array = [coord_payload(current, dimension)]
	while current != start:
		var key := coord_key(current)
		if not came_from.has(key):
			break
		current = coord_from_input(came_from[key], dimension)
		path.push_front(coord_payload(current, dimension))
	return path


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


static func coords_array_from_input(value, dimension: int) -> Array:
	var coords: Array = []
	if not (value is Array):
		return coords
	for item in value:
		if item is Dictionary:
			coords.append(coord_from_input(item, dimension))
	return coords


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
