@tool
extends RefCounted


static func parse(input: Dictionary, context: Dictionary) -> Dictionary:
	var prompt := str(input.get("prompt", input.get("request", ""))).strip_edges()
	var lower := prompt.to_lower()
	var mode := _detect_mode(input, lower, context)
	var task := _detect_task(input, lower)
	var region := _detect_region(input, lower, mode)
	var objects := _detect_objects(lower, mode)
	var constraints := _detect_constraints(lower, task)
	return {
		"mode": mode,
		"task": task,
		"theme": _detect_theme(input, lower),
		"region": region,
		"objects": objects,
		"constraints": constraints,
		"style": {
			"density": _detect_density(input, lower),
			"seed": int(input.get("seed", 0)),
			"noise": bool(input.get("noise", _mentions_any(lower, ["自然", "随机", "noise", "散布", "分布"]))),
		},
		"source_prompt": prompt,
		"context_summary": {
			"maps_count": (context.get("maps", []) as Array).size() if context.get("maps", []) is Array else 0,
			"has_resource_registry": bool(context.get("resource_registry", {}).get("exists", false)) if context.get("resource_registry", {}) is Dictionary else false,
			"has_spatial_index": bool(context.get("spatial_index", {}).get("exists", false)) if context.get("spatial_index", {}) is Dictionary else false,
		},
	}


static func _detect_mode(input: Dictionary, lower: String, context: Dictionary) -> String:
	var explicit := str(input.get("mode", "auto")).to_lower()
	if explicit in ["2d", "3d"]:
		return explicit
	if _mentions_any(lower, ["3d", "gridmap", "地牢", "房间", "mesh", "三维"]):
		return "3d"
	if _mentions_any(lower, ["2d", "tilemap", "森林", "村庄", "河", "平台", "二维"]):
		return "2d"
	var maps = context.get("maps", [])
	if maps is Array and (maps as Array).size() == 1:
		var only_map: Dictionary = maps[0]
		return "3d" if int(only_map.get("dimension", 2)) == 3 else "2d"
	return "2d"


static func _detect_task(input: Dictionary, lower: String) -> String:
	var explicit := str(input.get("task", "")).to_lower()
	if explicit != "":
		return explicit
	if _mentions_any(lower, ["删除", "删掉", "清除", "erase", "delete"]):
		return "erase"
	if _mentions_any(lower, ["替换", "换成", "replace"]):
		return "replace"
	if _mentions_any(lower, ["保存模板", "存成模板", "save blueprint", "template"]):
		return "save_blueprint"
	if _mentions_any(lower, ["再来一个", "复用", "apply blueprint"]):
		return "apply_blueprint"
	if _mentions_any(lower, ["装饰", "decorate"]):
		return "decorate"
	return "generate"


static func _detect_theme(input: Dictionary, lower: String) -> String:
	var explicit := str(input.get("theme", "")).strip_edges()
	if explicit != "":
		return explicit
	if _mentions_any(lower, ["精灵", "elf"]):
		return "elf"
	if _mentions_any(lower, ["森林", "forest"]):
		return "forest"
	if _mentions_any(lower, ["地牢", "dungeon"]):
		return "dungeon"
	if _mentions_any(lower, ["村庄", "village"]):
		return "village"
	if _mentions_any(lower, ["河", "river"]):
		return "river"
	return "generic"


static func _detect_region(input: Dictionary, lower: String, mode: String) -> Dictionary:
	if input.has("x") or input.has("y") or input.has("width") or input.has("height"):
		return {
			"type": "rect",
			"x": int(input.get("x", 0)),
			"y": int(input.get("y", 0)),
			"z": int(input.get("z", 0)) if mode == "3d" else 0,
			"width": max(1, int(input.get("width", 12 if mode == "3d" else 40))),
			"height": max(1, int(input.get("height", 12 if mode == "3d" else 30))),
			"depth": max(1, int(input.get("depth", 1))),
		}
	if _mentions_any(lower, ["左上", "top-left"]):
		return {"type": "named_region", "name": "top_left"}
	if _mentions_any(lower, ["中心", "中间", "center"]):
		return {"type": "named_region", "name": "center"}
	if _mentions_any(lower, ["河边", "near river", "river bank"]):
		return {"type": "near_tag", "tag": "water", "radius": 8}
	return {
		"type": "rect",
		"x": 0,
		"y": 0,
		"z": 0,
		"width": 12 if mode == "3d" else 40,
		"height": 12 if mode == "3d" else 30,
		"depth": 1,
	}


static func _detect_objects(lower: String, mode: String) -> Array:
	var objects: Array = []
	_add_object_if_mentioned(objects, lower, ["河", "river", "水"], "river", 1)
	_add_object_if_mentioned(objects, lower, ["路", "道路", "path", "road"], "path", 1)
	_add_object_if_mentioned(objects, lower, ["树", "tree"], "tree", 12)
	_add_object_if_mentioned(objects, lower, ["房屋", "house"], "house", 4)
	_add_object_if_mentioned(objects, lower, ["篝火", "campfire"], "campfire", 1)
	_add_object_if_mentioned(objects, lower, ["墙", "wall"], "wall", 1)
	_add_object_if_mentioned(objects, lower, ["门", "door"], "door", 2)
	_add_object_if_mentioned(objects, lower, ["火把", "torch"], "torch", 4)
	_add_object_if_mentioned(objects, lower, ["宝箱", "chest"], "chest", 1)
	if objects.is_empty() and mode == "3d":
		objects = [{"name": "floor", "count": 1}, {"name": "wall", "count": 1}, {"name": "door", "count": 2}]
	elif objects.is_empty():
		objects = [{"name": "ground", "count": 1}]
	return objects


static func _detect_constraints(lower: String, task: String) -> Array:
	var constraints: Array = []
	if task in ["generate", "replace", "decorate"]:
		constraints.append("preserve_existing_unmentioned_content")
	if _mentions_any(lower, ["连通", "可达", "path", "road", "门"]):
		constraints.append("walkable_path_connected")
	if _mentions_any(lower, ["不能重叠", "no overlap", "不重叠"]):
		constraints.append("no_overlap")
	if _mentions_any(lower, ["河边", "near river", "river bank"]):
		constraints.append("near_water")
	if _mentions_any(lower, ["必须经过", "经过中心", "pass through", "waypoint"]):
		constraints.append("must_pass_center")
	return constraints


static func _detect_density(input: Dictionary, lower: String) -> String:
	var explicit := str(input.get("density", "")).to_lower()
	if explicit in ["low", "medium", "high"]:
		return explicit
	if _mentions_any(lower, ["稀疏", "low"]):
		return "low"
	if _mentions_any(lower, ["密集", "dense", "high"]):
		return "high"
	return "medium"


static func _add_object_if_mentioned(out: Array, lower: String, needles: Array, object_name: String, count: int) -> void:
	if _mentions_any(lower, needles):
		out.append({"name": object_name, "count": count})


static func _mentions_any(text: String, needles: Array) -> bool:
	for needle in needles:
		if text.find(str(needle).to_lower()) != -1:
			return true
	return false
