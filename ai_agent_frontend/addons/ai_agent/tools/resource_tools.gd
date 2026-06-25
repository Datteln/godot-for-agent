@tool
extends RefCounted

const PathUtils = preload("res://addons/ai_agent/tools/path_utils.gd")


static func create_resource(input: Dictionary, undo_manager: Node) -> Dictionary:
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	var type_name := str(input.get("type", "Resource"))
	if path == "":
		return {"ok": false, "message": "path must be relative or res://"}
	if not PathUtils.is_write_allowed(path):
		return {"ok": false, "message": "writing to this path is not allowed: " + path, "error_code": "write_denied"}
	var instance = ClassDB.instantiate(type_name)
	if not (instance is Resource):
		return {"ok": false, "message": "Cannot instantiate resource type: " + type_name}
	var resource: Resource = instance
	var absolute := ProjectSettings.globalize_path(path)
	var before_exists := FileAccess.file_exists(absolute)
	var before_bytes := PackedByteArray()
	if before_exists:
		before_bytes = FileAccess.get_file_as_bytes(absolute)
	var err := ResourceSaver.save(resource, path)
	if err == OK and undo_manager != null:
		var after_bytes := FileAccess.get_file_as_bytes(absolute)
		undo_manager.record_binary_file_write(path, before_bytes, after_bytes, before_exists)
	return {"ok": err == OK, "path": path, "type": type_name, "error": err}


static func read_image_metadata(input: Dictionary) -> Dictionary:
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	if path == "":
		return {"ok": false, "message": "path must be relative or res://"}
	if not PathUtils.is_read_allowed(path):
		return {"ok": false, "message": "reading this path is not allowed: " + path, "error_code": "read_denied"}
	var image := Image.new()
	var err := image.load(path)
	if err != OK:
		return {"ok": false, "message": "failed to load image", "path": path, "error": err}
	var step = max(1, int(input.get("sample_step", 8)))
	var colors := {}
	for y in range(0, image.get_height(), step):
		for x in range(0, image.get_width(), step):
			var hex := _color_key(image.get_pixel(x, y))
			colors[hex] = int(colors.get(hex, 0)) + 1
	var dominant: Array = []
	for key in colors.keys():
		dominant.append({"hex": key, "count": colors[key]})
	dominant.sort_custom(func(a: Dictionary, b: Dictionary): return int(a["count"]) > int(b["count"]))
	if dominant.size() > 16:
		dominant = dominant.slice(0, 16)
	return {
		"ok": true,
		"path": path,
		"absolute_path": ProjectSettings.globalize_path(path),
		"width": image.get_width(),
		"height": image.get_height(),
		"format": image.get_format(),
		"dominant_colors": dominant
	}


static func create_sprite_frames_from_sheet(input: Dictionary, undo_manager: Node) -> Dictionary:
	var sheet_path := PathUtils.to_res_path(str(input.get("sheet_path", "")))
	var output_path := PathUtils.to_res_path(str(input.get("output_path", "")))
	var frame_width := max(1, int(input.get("frame_width", 0)))
	var frame_height := max(1, int(input.get("frame_height", 0)))
	if sheet_path == "" or output_path == "":
		return {"ok": false, "message": "sheet_path and output_path are required"}
	if not PathUtils.is_read_allowed(sheet_path):
		return {"ok": false, "message": "reading this path is not allowed: " + sheet_path, "error_code": "read_denied"}
	if not PathUtils.is_write_allowed(output_path):
		return {"ok": false, "message": "writing to this path is not allowed: " + output_path, "error_code": "write_denied"}
	var texture = load(sheet_path)
	if not (texture is Texture2D):
		return {"ok": false, "message": "sheet_path is not a Texture2D", "sheet_path": sheet_path}

	var frames := SpriteFrames.new()
	var columns = max(1, int(texture.get_width() / frame_width))
	var total = columns * max(1, int(texture.get_height() / frame_height))
	var animations: Array = input.get("animations", [])
	for animation in animations:
		if not (animation is Dictionary):
			continue
		var name := str(animation.get("name", "default"))
		var from_index := clamp(int(animation.get("from", 0)), 0, total - 1)
		var to_index := clamp(int(animation.get("to", from_index)), 0, total - 1)
		var fps := float(animation.get("fps", 8.0))
		var loop := bool(animation.get("loop", true))
		if not frames.has_animation(name):
			frames.add_animation(name)
		frames.set_animation_speed(name, fps)
		frames.set_animation_loop(name, loop)
		for index in range(from_index, to_index + 1):
			var atlas := AtlasTexture.new()
			atlas.atlas = texture
			atlas.region = Rect2(
				float((index % columns) * frame_width),
				float(int(index / columns) * frame_height),
				float(frame_width),
				float(frame_height)
			)
			frames.add_frame(name, atlas)

	var absolute := ProjectSettings.globalize_path(output_path)
	var before_exists := FileAccess.file_exists(absolute)
	var before_bytes := PackedByteArray()
	if before_exists:
		before_bytes = FileAccess.get_file_as_bytes(absolute)
	var err := ResourceSaver.save(frames, output_path)
	if err == OK and undo_manager != null:
		var after_bytes := FileAccess.get_file_as_bytes(absolute)
		undo_manager.record_binary_file_write(output_path, before_bytes, after_bytes, before_exists)
	return {
		"ok": err == OK,
		"path": output_path,
		"sheet_path": sheet_path,
		"animations": animations.size(),
		"error": err
	}


## 读取任意 Resource（.tres/.res）的可导出/可存档属性。嵌套的 Object/Resource
## 引用不能直接进 JSON.stringify，所以经过 `_json_safe_value` 折算成可序列化的占位结构。
static func read_resource(input: Dictionary) -> Dictionary:
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	if path == "":
		return {"ok": false, "message": "path must be relative or res://"}
	if not PathUtils.is_read_allowed(path):
		return {"ok": false, "message": "reading this path is not allowed: " + path, "error_code": "read_denied"}
	if not FileAccess.file_exists(path):
		return {"ok": false, "message": "resource not found: " + path, "error_code": "resource_not_found"}
	var resource = load(path)
	if not (resource is Resource):
		return {"ok": false, "message": "failed to load resource: " + path, "error_code": "load_failed"}
	var properties := {}
	for prop in resource.get_property_list():
		if int(prop.get("usage", 0)) & PROPERTY_USAGE_STORAGE == 0:
			continue
		var prop_name := str(prop.get("name", ""))
		if prop_name == "" or prop_name in ["resource_local_to_scene", "script"]:
			continue
		properties[prop_name] = _json_safe_value(resource.get(prop_name))
	var script := resource.get_script()
	return {
		"ok": true,
		"path": path,
		"type": resource.get_class(),
		"script_path": str(script.resource_path) if script != null else "",
		"properties": properties
	}


static func set_resource_property(input: Dictionary, undo_manager: Node) -> Dictionary:
	var path := PathUtils.to_res_path(str(input.get("path", "")))
	if path == "":
		return {"ok": false, "message": "path must be relative or res://"}
	if not PathUtils.is_write_allowed(path):
		return {"ok": false, "message": "writing to this path is not allowed: " + path, "error_code": "write_denied"}
	if not FileAccess.file_exists(path):
		return {"ok": false, "message": "resource not found: " + path, "error_code": "resource_not_found"}
	var resource = load(path)
	if not (resource is Resource):
		return {"ok": false, "message": "failed to load resource: " + path, "error_code": "load_failed"}
	var property := str(input.get("property", ""))
	if property == "":
		return {"ok": false, "message": "property is required", "error_code": "property_required"}
	var before := _json_safe_value(resource.get(property))
	resource.set(property, _resolve_resource_reference(input.get("value")))

	var absolute := ProjectSettings.globalize_path(path)
	var before_bytes := FileAccess.get_file_as_bytes(absolute)
	var err := ResourceSaver.save(resource, path)
	if err != OK:
		return {"ok": false, "message": "failed to save resource (error %d)" % err, "error_code": "save_failed"}
	if undo_manager != null:
		var after_bytes := FileAccess.get_file_as_bytes(absolute)
		undo_manager.record_binary_file_write(path, before_bytes, after_bytes, true)
	return {"ok": true, "path": path, "property": property, "before": before}


## JSON 没法直接表达"引用另一个资源"，所以约定：`value` 传 `{"_resource_path": "res://..."}`
## 这种占位结构时，先 load() 出真正的 Resource 对象再赋值（比如把 Shader 挂到
## ShaderMaterial.shader 上）；其余情况原样传给 `resource.set()`。
static func _resolve_resource_reference(value: Variant) -> Variant:
	if value is Dictionary and value.has("_resource_path"):
		var ref_path := PathUtils.to_res_path(str(value.get("_resource_path", "")))
		if ref_path != "" and PathUtils.is_read_allowed(ref_path) and FileAccess.file_exists(ref_path):
			var loaded = load(ref_path)
			if loaded is Resource:
				return loaded
	return value


## 一次性把 .gdshader 文本和引用它的 ShaderMaterial 一起创建好，比分别调用
## propose_content_file + create_resource + set_resource_property 三步省事。
static func create_shader_material(input: Dictionary, undo_manager: Node) -> Dictionary:
	var material_path := PathUtils.to_res_path(str(input.get("material_path", "")))
	var shader_path := PathUtils.to_res_path(str(input.get("shader_path", "")))
	var shader_code := str(input.get("shader_code", ""))
	if material_path == "" or shader_path == "":
		return {"ok": false, "message": "material_path and shader_path are required", "error_code": "path_required"}
	if shader_code.strip_edges() == "":
		return {"ok": false, "message": "shader_code is required", "error_code": "shader_code_required"}
	if not PathUtils.is_write_allowed(material_path) or not PathUtils.is_write_allowed(shader_path):
		return {"ok": false, "message": "writing to one of these paths is not allowed", "error_code": "write_denied"}

	var shader_absolute := ProjectSettings.globalize_path(shader_path)
	var shader_before_exists := FileAccess.file_exists(shader_absolute)
	var shader_before_text := ""
	if shader_before_exists:
		var existing := FileAccess.open(shader_absolute, FileAccess.READ)
		if existing != null:
			shader_before_text = existing.get_as_text()
			existing.close()
	if undo_manager != null:
		var shader_write_error: Error = undo_manager.record_file_write(shader_path, shader_before_text, shader_code, shader_before_exists)
		if shader_write_error != OK:
			return {
				"ok": false,
				"message": "failed to write shader file: %s (%s)" % [shader_path, error_string(shader_write_error)],
				"error_code": "write_failed"
			}
	else:
		DirAccess.make_dir_recursive_absolute(shader_absolute.get_base_dir())
		var file := FileAccess.open(shader_absolute, FileAccess.WRITE)
		if file == null:
			return {"ok": false, "message": "failed to write shader file: " + shader_path, "error_code": "write_failed"}
		file.store_string(shader_code)
		file.close()

	## 必须先确保磁盘上是新内容再加载，避免拿到 ResourceLoader 缓存的旧 Shader。
	var shader = ResourceLoader.load(shader_path, "", ResourceLoader.CACHE_MODE_REPLACE)
	if not (shader is Shader):
		return {"ok": false, "message": "failed to load shader as Shader: " + shader_path, "error_code": "shader_load_failed"}

	var material := ShaderMaterial.new()
	material.shader = shader

	var material_absolute := ProjectSettings.globalize_path(material_path)
	var material_before_exists := FileAccess.file_exists(material_absolute)
	var material_before_bytes := PackedByteArray()
	if material_before_exists:
		material_before_bytes = FileAccess.get_file_as_bytes(material_absolute)
	var err := ResourceSaver.save(material, material_path)
	if err == OK and undo_manager != null:
		var material_after_bytes := FileAccess.get_file_as_bytes(material_absolute)
		undo_manager.record_binary_file_write(material_path, material_before_bytes, material_after_bytes, material_before_exists)
	return {"ok": err == OK, "material_path": material_path, "shader_path": shader_path, "error": err}


## 给场景里一个 AnimationPlayer 的某个动画加/替换一条 VALUE 轨道。只接管这一条轨道
## （按 track_path 匹配），同一动画里其他既有轨道不受影响。Undo 只回滚这一条轨道
## 之前的关键帧快照，而不是整个 Animation 资源，避免影响其他并发编辑。
static func create_animation_track(input: Dictionary, editor_interface: EditorInterface, undo_manager: Node) -> Dictionary:
	if editor_interface == null:
		return {"ok": false, "message": "EditorInterface is not available"}
	var root := editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "message": "No edited scene root"}
	var player_path := str(input.get("player_path", ""))
	var player_node := root.get_node_or_null(NodePath(player_path))
	if not (player_node is AnimationPlayer):
		return {"ok": false, "message": "AnimationPlayer not found: " + player_path, "error_code": "player_not_found"}
	var player: AnimationPlayer = player_node

	var anim_name := str(input.get("animation", "")).strip_edges()
	if anim_name == "":
		return {"ok": false, "message": "animation is required", "error_code": "animation_required"}
	var library_name := str(input.get("library", ""))
	if not player.has_animation_library(library_name):
		player.add_animation_library(library_name, AnimationLibrary.new())
	var library := player.get_animation_library(library_name)
	var animation: Animation
	if library.has_animation(anim_name):
		animation = library.get_animation(anim_name)
	else:
		animation = Animation.new()
		library.add_animation(anim_name, animation)

	var track_path := str(input.get("track_path", ""))
	if track_path == "":
		return {"ok": false, "message": "track_path is required", "error_code": "track_path_required"}
	var keyframes: Array = input.get("keyframes", [])
	if keyframes.is_empty():
		return {"ok": false, "message": "keyframes is required", "error_code": "keyframes_required"}
	var interpolation := int(input.get("interpolation", Animation.INTERPOLATION_LINEAR))

	var existing_index := _find_value_track(animation, track_path)
	var before_snapshot: Variant = _snapshot_value_track(animation, existing_index) if existing_index >= 0 else null
	if existing_index >= 0:
		animation.remove_track(existing_index)
	var new_index := animation.add_track(Animation.TYPE_VALUE)
	animation.track_set_path(new_index, NodePath(track_path))
	animation.track_set_interpolation_type(new_index, interpolation)
	for frame in keyframes:
		if not (frame is Dictionary):
			continue
		var time := float(frame.get("time", 0.0))
		var value = frame.get("value")
		var transition := float(frame.get("transition", 1.0))
		animation.track_insert_key(new_index, time, value, transition)
	var after_snapshot := _snapshot_value_track(animation, new_index)

	if undo_manager != null:
		undo_manager.record_animation_track(animation, track_path, before_snapshot, after_snapshot)

	return {
		"ok": true,
		"player_path": player_path,
		"animation": anim_name,
		"library": library_name,
		"track_path": track_path,
		"keyframes": keyframes.size()
	}


static func _find_value_track(animation: Animation, track_path: String) -> int:
	for i in range(animation.get_track_count()):
		if animation.track_get_type(i) == Animation.TYPE_VALUE and str(animation.track_get_path(i)) == track_path:
			return i
	return -1


static func _snapshot_value_track(animation: Animation, index: int) -> Dictionary:
	var keys: Array = []
	for k in range(animation.track_get_key_count(index)):
		keys.append({
			"time": animation.track_get_key_time(index, k),
			"value": animation.track_get_key_value(index, k),
			"transition": animation.track_get_key_transition(index, k)
		})
	return {
		"path": str(animation.track_get_path(index)),
		"interpolation": int(animation.track_get_interpolation_type(index)),
		"keys": keys
	}


static func _json_safe_value(value: Variant) -> Variant:
	if value is Resource:
		return {"_type": "Resource", "class": value.get_class(), "path": str(value.resource_path)}
	if value is Object:
		return {"_type": "Object", "class": value.get_class()}
	if value is Array:
		var out: Array = []
		for item in value:
			out.append(_json_safe_value(item))
		return out
	if value is Dictionary:
		var out_dict := {}
		for key in value.keys():
			out_dict[str(key)] = _json_safe_value(value[key])
		return out_dict
	return value


static func _color_key(color: Color) -> String:
	return "#%02x%02x%02x" % [
		int(clamp(color.r * 255.0, 0.0, 255.0)),
		int(clamp(color.g * 255.0, 0.0, 255.0)),
		int(clamp(color.b * 255.0, 0.0, 255.0))
	]
