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


static func _color_key(color: Color) -> String:
	return "#%02x%02x%02x" % [
		int(clamp(color.r * 255.0, 0.0, 255.0)),
		int(clamp(color.g * 255.0, 0.0, 255.0)),
		int(clamp(color.b * 255.0, 0.0, 255.0))
	]
