@tool
extends RefCounted

# 可复现的 FastNoiseLite 网格采样器，供程序化生成 Skill 调用。
# 支持 2D 和 3D 网格：按 seed / 频率 / 分形等参数生成确定性噪声值，
# 供地图程序化生成在 reader 之后预演地形或纹理分布，输出可回放、可校验。


static func sample(input: Dictionary, max_cells: int) -> Dictionary:
	var dimension := 3 if str(input.get("dimension", "2d")) == "3d" else 2
	var width := max(1, int(input.get("width", 1)))
	var height := max(1, int(input.get("height", 1)))
	var depth := max(1, int(input.get("depth", 1))) if dimension == 3 else 1
	if width * height * depth > max_cells:
		return {
			"ok": false,
			"message": "noise grid exceeds the %d-sample limit; request a smaller grid" % max_cells,
			"error_code": "noise_grid_too_large",
		}
	var x := int(input.get("x", 0))
	var y := int(input.get("y", 0))
	var z := int(input.get("z", 0))
	var noise := FastNoiseLite.new()
	noise.seed = int(input.get("seed", 0))
	noise.frequency = float(input.get("frequency", 0.05))
	noise.noise_type = _noise_type_from_name(str(input.get("noise_type", "simplex")))
	var octaves := int(input.get("octaves", 0))
	if octaves > 0:
		noise.fractal_octaves = octaves
	var rows: Array = []
	if dimension == 3:
		for dz in range(depth):
			var plane: Array = []
			for dy in range(height):
				var row: Array = []
				for dx in range(width):
					row.append(_normalize(noise.get_noise_3d(x + dx, y + dy, z + dz)))
				plane.append(row)
			rows.append(plane)
	else:
		for dy in range(height):
			var row: Array = []
			for dx in range(width):
				row.append(_normalize(noise.get_noise_2d(x + dx, y + dy)))
			rows.append(row)
	return {
		"ok": true,
		"dimension": dimension,
		"origin": {"x": x, "y": y, "z": z},
		"width": width,
		"height": height,
		"depth": depth,
		"seed": noise.seed,
		"frequency": noise.frequency,
		"noise_type": str(input.get("noise_type", "simplex")),
		"values": rows,
		"note": "values are normalized to 0..1; pick a threshold to convert to placement density",
	}


static func _normalize(value: float) -> float:
	return clampf((value + 1.0) * 0.5, 0.0, 1.0)


static func _noise_type_from_name(noise_name: String) -> int:
	match noise_name.to_lower():
		"perlin":
			return FastNoiseLite.TYPE_PERLIN
		"value":
			return FastNoiseLite.TYPE_VALUE
		"value_cubic":
			return FastNoiseLite.TYPE_VALUE_CUBIC
		"cellular":
			return FastNoiseLite.TYPE_CELLULAR
		"simplex_smooth":
			return FastNoiseLite.TYPE_SIMPLEX_SMOOTH
		_:
			return FastNoiseLite.TYPE_SIMPLEX
