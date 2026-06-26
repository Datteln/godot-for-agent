@tool
extends RefCounted

# 连通性校验的核心抽象：用一个可插拔的「移动模型」(movement) 描述角色到底怎么移动，
# 而不是写死「空格子就算可走、四向相邻就算连通」。这样同一套 BFS/A* 框架就能覆盖任意玩法：
#   - "grid"：纯抽象邻格连通，无重力（战棋、俯视、解谜、迷宫）。
#   - "leap"：受重力约束——落点必须脚下有支撑，且只能跳到「水平间隔/上升/下落」限制内的
#             其它落点（2D 平台跳跃、3D 带跳跃高度限制的攀爬都用它）。
#   - "free"：无重力、不需要地面支撑，只受单步最大距离约束（飞行、游泳、幽灵类移动）。
# 关键原则：模型的能力参数（max_horizontal_gap / max_rise / max_fall / max_step）必须由调用方
# 按角色控制器里的真实移动能力换算成「格」数传进来，而不是凭空假设——只有这样，连通性「通过」
# 才真正等价于「这套移动能力下到得了」，而不是「天空是连续的」。


## 把工具输入解析成一个移动模型配置。未指定时回退到与历史行为一致的 "grid" 模型。
static func movement_from_input(input: Dictionary, dimension: int) -> Dictionary:
	var model := str(input.get("movement_model", "grid")).strip_edges().to_lower()
	if not (model in ["grid", "leap", "free"]):
		model = "grid"
	return {
		"model": model,
		"dimension": dimension,
		"walkable_is_filled": bool(input.get("walkable_is_filled", false)),
		"support_offset": _support_offset_from_input(input, dimension),
		# leap：跳跃/攀爬能力（按格计）。水平间隔与上升高度通常受跳跃距离/高度限制；
		# 下落默认给一个较宽松的上限（下落一般不是限制因素），但仍可显式收紧。
		"max_horizontal_gap": maxi(1, int(input.get("max_horizontal_gap", 1))),
		"max_rise": maxi(0, int(input.get("max_rise", 1))),
		"max_fall": maxi(0, int(input.get("max_fall", 64))),
		# free：单步最大移动距离（曼哈顿格数）。
		"max_step": maxi(1, int(input.get("max_step", 1))),
	}


## 计算「朝向地面（重力方向）」的单位偏移。默认：2D 瓦片坐标 y 向下增大，地面在 +y；
## 3D 网格坐标 y 向上，地面在 -y。可用 gravity_axis("x"/"y"/"z")+gravity_sign(1/-1) 覆盖。
static func _support_offset_from_input(input: Dictionary, dimension: int) -> Vector3i:
	var axis := str(input.get("gravity_axis", "")).strip_edges().to_lower()
	if axis in ["x", "y", "z"]:
		var sign_value := 1 if int(input.get("gravity_sign", 1)) >= 0 else -1
		match axis:
			"x":
				return Vector3i(sign_value, 0, 0)
			"z":
				return Vector3i(0, 0, sign_value)
			_:
				return Vector3i(0, sign_value, 0)
	return Vector3i(0, 1, 0) if dimension == 2 else Vector3i(0, -1, 0)


static func validate_region(
	filled: Dictionary,
	region: Dictionary,
	dimension: int,
	start_value,
	goal_value,
	movement: Dictionary,
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
		"movement_model": str(movement.get("model", "grid")),
	}
	var multi := check_multi_point_connectivity(
		filled,
		region,
		dimension,
		movement,
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
	var connectivity := check_connectivity(filled, start, goal, region, dimension, movement, path_algorithm)
	result["connectivity"] = connectivity
	if not bool(connectivity.get("reachable", false)):
		issues.append("goal is not reachable from start under the '%s' movement model" % str(movement.get("model", "grid")))
		result["passed"] = false
		result["repair_plan"] = build_connectivity_repair_plan(filled, start, goal, region, dimension, movement)
	else:
		result["path"] = connectivity.get("path", [])
	return result


static func check_multi_point_connectivity(
	filled: Dictionary,
	region: Dictionary,
	dimension: int,
	movement: Dictionary,
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
			var segment := check_connectivity(filled, waypoints[i], waypoints[i + 1], region, dimension, movement, path_algorithm)
			segment["from"] = coord_payload(waypoints[i], dimension)
			segment["to"] = coord_payload(waypoints[i + 1], dimension)
			(result["segments"] as Array).append(segment)
			if not bool(segment.get("reachable", false)):
				result["reachable"] = false
	if not entrances.is_empty() and not exits.is_empty():
		for entrance in entrances:
			var reachable_exit := false
			for exit in exits:
				var pair := check_connectivity(filled, entrance, exit, region, dimension, movement, path_algorithm)
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
	movement: Dictionary
) -> Array:
	if not in_region(start, region) or not in_region(goal, region):
		return []
	var path := manhattan_path(start, goal, dimension)
	var routed := find_path_astar(filled, start, goal, region, dimension, movement)
	if bool(routed.get("reachable", false)):
		path = []
		for point in routed.get("path", []):
			path.append(coord_from_input(point, dimension))
	if str(movement.get("model", "grid")) == "leap":
		# leap 失败的本质是「脚下没有连续支撑」，靠清空空气没用——要在脚下那一行补地面/平台。
		# 把脚步路径每一格正下方的支撑格收集出来，建议 fill 成地面，搭出一条可走的桥。
		var support_offset: Vector3i = movement.get("support_offset", Vector3i(0, 1, 0))
		var seen := {}
		var bridge: Array = []
		for coords in path:
			var support: Vector3i = coords + support_offset
			if in_region(support, region) and not seen.has(coord_key(support)):
				seen[coord_key(support)] = true
				bridge.append(coord_payload(support, dimension))
		return [{
			"type": "connectivity_bridge",
			"action": "fill",
			"cells": bridge,
			"cells_count": bridge.size(),
			"note": "Fill these support cells with ground/platform tiles (supply source_id/atlas for 2D or item for 3D) to bridge the gap, then validate again.",
		}]
	var cells: Array = []
	for coords in path:
		if in_region(coords, region):
			cells.append(coord_payload(coords, dimension))
	return [{
		"type": "connectivity_corridor",
		"action": "fill" if bool(movement.get("walkable_is_filled", false)) else "erase",
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
	movement: Dictionary,
	path_algorithm: String = "bfs"
) -> Dictionary:
	var model := str(movement.get("model", "grid"))
	if not in_region(start, region) or not in_region(goal, region):
		return {"reachable": false, "reason": "start or goal is outside the validated region"}
	if not is_standable(filled, start, region, movement):
		return {"reachable": false, "reason": _not_standable_reason("start", model)}
	if not is_standable(filled, goal, region, movement):
		return {"reachable": false, "reason": _not_standable_reason("goal", model)}
	if path_algorithm.to_lower() == "astar" or path_algorithm.to_lower() == "a*":
		return find_path_astar(filled, start, goal, region, dimension, movement)
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
				"movement_model": model,
				"distance": int(visited[coord_key(current)]),
				"path": _reconstruct_path_from_parents(came_from, start, goal, dimension),
			}
		for next in movement_neighbors(filled, current, region, movement):
			var next_key := coord_key(next)
			if visited.has(next_key):
				continue
			came_from[next_key] = coord_payload(current, dimension)
			visited[next_key] = int(visited[coord_key(current)]) + 1
			queue.append(next)
	return {"reachable": false, "algorithm": "bfs", "movement_model": model, "reason": "no reachable path connects start and goal under the '%s' movement model" % model, "visited": visited.size()}


static func find_path_astar(
	filled: Dictionary,
	start: Vector3i,
	goal: Vector3i,
	region: Dictionary,
	dimension: int,
	movement: Dictionary
) -> Dictionary:
	var model := str(movement.get("model", "grid"))
	if not in_region(start, region) or not in_region(goal, region):
		return {"reachable": false, "algorithm": "astar", "reason": "start or goal is outside the validated region"}
	if not is_standable(filled, start, region, movement):
		return {"reachable": false, "algorithm": "astar", "reason": _not_standable_reason("start", model)}
	if not is_standable(filled, goal, region, movement):
		return {"reachable": false, "algorithm": "astar", "reason": _not_standable_reason("goal", model)}
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
				"movement_model": model,
				"distance": int(g_score.get(coord_key(goal), path.size() - 1)),
				"path": path,
				"visited": g_score.size(),
			}
		for next in movement_neighbors(filled, current, region, movement):
			var next_key := coord_key(next)
			var tentative_g := int(g_score.get(coord_key(current), 0)) + 1
			if tentative_g >= int(g_score.get(next_key, 2147483647)):
				continue
			came_from[next_key] = coord_payload(current, dimension)
			g_score[next_key] = tentative_g
			f_score[next_key] = tentative_g + heuristic(next, goal, dimension)
			if not open_keys.has(next_key):
				open.append(next)
				open_keys[next_key] = true
	return {"reachable": false, "algorithm": "astar", "movement_model": model, "reason": "no reachable path connects start and goal under the '%s' movement model" % model, "visited": g_score.size()}


static func _not_standable_reason(which: String, model: String) -> String:
	if model == "leap":
		return "%s cell is not a valid foothold (must be empty with solid support directly below)" % which
	return "%s cell is not walkable" % which


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


## 某格在「基础含义」上是否可走：默认空格可走；walkable_is_filled=true 时反转为实心可走。
static func _base_walkable(filled: Dictionary, coords: Vector3i, movement: Dictionary) -> bool:
	var is_filled := filled.has(coord_key(coords))
	return is_filled if bool(movement.get("walkable_is_filled", false)) else not is_filled


## leap 模型下「脚下是否有支撑」：正下方那一格是实心（不可走）才算站得住。
## 支撑格落在校验区域之外时无法判断，按「地面在区域外延续」给予通过，避免区域裁剪造成的假阴性
## （真正的空洞——脚下也是空气且都在区域内——仍会被判为不可站）。
static func _has_support(filled: Dictionary, coords: Vector3i, region: Dictionary, movement: Dictionary) -> bool:
	var support: Vector3i = coords + (movement.get("support_offset", Vector3i(0, 1, 0)) as Vector3i)
	if not in_region(support, region):
		return true
	return not _base_walkable(filled, support, movement)


## 某格能否作为一个「落脚点」被站立/占据。grid/free 只要基础可走；leap 还要求脚下有支撑。
static func is_standable(filled: Dictionary, coords: Vector3i, region: Dictionary, movement: Dictionary) -> bool:
	if not _base_walkable(filled, coords, movement):
		return false
	if str(movement.get("model", "grid")) == "leap":
		return _has_support(filled, coords, region, movement)
	return true


## 按移动模型生成 current 的可达邻居（已过滤 in_region + 可站立）。
static func movement_neighbors(filled: Dictionary, current: Vector3i, region: Dictionary, movement: Dictionary) -> Array:
	match str(movement.get("model", "grid")):
		"leap":
			return _leap_neighbors(filled, current, region, movement)
		"free":
			return _free_neighbors(filled, current, region, movement)
		_:
			return _grid_neighbors(filled, current, region, movement)


static func _grid_neighbors(filled: Dictionary, current: Vector3i, region: Dictionary, movement: Dictionary) -> Array:
	var result: Array = []
	for offset in neighbor_offsets(int(movement.get("dimension", 2))):
		var next: Vector3i = current + offset
		if in_region(next, region) and is_standable(filled, next, region, movement):
			result.append(next)
	return result


static func _free_neighbors(filled: Dictionary, current: Vector3i, region: Dictionary, movement: Dictionary) -> Array:
	var dimension := int(movement.get("dimension", 2))
	var step := maxi(1, int(movement.get("max_step", 1)))
	var result: Array = []
	var z_range: Array = range(-step, step + 1) if dimension == 3 else range(0, 1)
	for dx in range(-step, step + 1):
		for dy in range(-step, step + 1):
			for dz in z_range:
				if dx == 0 and dy == 0 and dz == 0:
					continue
				if absi(dx) + absi(dy) + absi(dz) > step:
					continue
				var next := current + Vector3i(dx, dy, dz)
				if in_region(next, region) and is_standable(filled, next, region, movement):
					result.append(next)
	return result


static func _leap_neighbors(filled: Dictionary, current: Vector3i, region: Dictionary, movement: Dictionary) -> Array:
	var dimension := int(movement.get("dimension", 2))
	var gap := maxi(1, int(movement.get("max_horizontal_gap", 1)))
	var max_rise := maxi(0, int(movement.get("max_rise", 1)))
	var max_fall := maxi(0, int(movement.get("max_fall", 64)))
	var support_offset: Vector3i = movement.get("support_offset", Vector3i(0, 1, 0))
	var down_axis := _down_axis(support_offset)
	var down_sign := _axis_value(support_offset, down_axis)
	var radius := maxi(gap, maxi(max_rise, max_fall))
	var result: Array = []
	var z_range: Array = range(-radius, radius + 1) if dimension == 3 else range(0, 1)
	for dx in range(-radius, radius + 1):
		for dy in range(-radius, radius + 1):
			for dz in z_range:
				if dx == 0 and dy == 0 and dz == 0:
					continue
				var delta := Vector3i(dx, dy, dz)
				# 沿重力轴的位移：正=下落，负=上升。
				var down_amount := _axis_value(delta, down_axis) * down_sign
				if down_amount < 0 and -down_amount > max_rise:
					continue
				if down_amount > 0 and down_amount > max_fall:
					continue
				# 水平位移（非重力轴）取切比雪夫距离，允许 3D 里的斜跳。
				var horizontal := _horizontal_magnitude(delta, down_axis)
				if horizontal > gap:
					continue
				var next := current + delta
				if in_region(next, region) and is_standable(filled, next, region, movement):
					result.append(next)
	return result


static func _down_axis(support_offset: Vector3i) -> int:
	if support_offset.x != 0:
		return 0
	if support_offset.z != 0:
		return 2
	return 1


static func _axis_value(v: Vector3i, axis: int) -> int:
	match axis:
		0:
			return v.x
		2:
			return v.z
		_:
			return v.y


static func _horizontal_magnitude(delta: Vector3i, down_axis: int) -> int:
	var result := 0
	if down_axis != 0:
		result = maxi(result, absi(delta.x))
	if down_axis != 1:
		result = maxi(result, absi(delta.y))
	if down_axis != 2:
		result = maxi(result, absi(delta.z))
	return result


static func neighbor_offsets(dimension: int) -> Array:
	if dimension == 3:
		return [
			Vector3i(1, 0, 0), Vector3i(-1, 0, 0),
			Vector3i(0, 1, 0), Vector3i(0, -1, 0),
			Vector3i(0, 0, 1), Vector3i(0, 0, -1),
		]
	return [Vector3i(1, 0, 0), Vector3i(-1, 0, 0), Vector3i(0, 1, 0), Vector3i(0, -1, 0)]
