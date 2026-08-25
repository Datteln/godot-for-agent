---
name: godot-map-authoring
description: Bootstrap and maintain code-driven Godot map authoring without guessing serialized map data.
when_to_use: Use when creating or editing TileMapLayer, legacy TileMap, or GridMap content.
allowed-tools: [describe_tilemap_selection, describe_map_region, read_scene_tree, read_file, grep_code, read_class_docs, apply_text_edit, propose_script_edit, propose_content_file, reload_map_targets, capture_viewport_screenshot]
paths: []
---

为 Godot 地图建立或维护可读、可审核的作者入口。不要根据模型记忆手拼 `.tscn` 中的 TileMap/GridMap 序列化 cell-data。

按下面顺序工作：

1. 用 `describe_tilemap_selection` 或小范围 `describe_map_region` 确认真实目标、图层、TileSet/GridMap 事实。大范围结果若 `truncated`，读取 `observed_bounds`、`row_runs` 和 `next_query`，只继续查询所需边界。
2. 用 `read_file` 和 `read_scene_tree` 查找已有的 layout、生成器、配置及其挂载节点；编辑器上下文或检索给出的已知路径直接读取，不要重复搜索。确需 `grep_code` 兜底时，`include` 只用源码/配置 glob（如 `**/*.gd`、`**/*.json`、`**/*.cfg`、`**/*.tscn`），绝不使用 `**/*`。已有入口时只编辑这些可读文件，并保留手工图层。
3. 没有入口时，直接发出 bootstrap 的第一个可审批工具调用；内联审批卡片就是确认，不要先输出“是否继续”或等待额外文字批准。按一次一个工具调用的协议，依次完成下列 bootstrap 批次：
   - 新建人可读的 layout/config（推荐 JSON、CFG 或 GDScript 常量），只表达格子坐标、语义标记和已观察到的 tile 引用；
   - 新建带 `@tool` 的 builder 脚本；它暴露生成目标和 layout 路径，并在编辑器中由明确的重建动作驱动；
   - 在场景中新增或指定一个 generated-only 图层/节点。不要覆盖已有手工 TileMapLayer、TileMap 或 GridMap；迁移旧内容前必须取得用户明确批准。
4. 在填入任何具体 Godot 调用前，必须先调用 `read_class_docs` 查询实际目标类型：先 `overview`，不清楚成员名时先 `search`，然后以 `members` 只请求将调用的精确签名（如 legacy TileMap 的 `set_cell`、`clear_layer`）。随后按文档实现清空、读取 layout、设置 cell/mesh 和编辑器重建；不要凭记忆猜 API，也绝不请求完整 ClassDB。
5. builder 至少使用这种编辑器安全骨架，并根据第 4 步的文档补全具体实现：

```gdscript
@tool
extends Node

@export_node_path("Node") var generated_target_path: NodePath
@export_file("*.json") var layout_path := ""
@export var rebuild_now := false:
	set(value):
		rebuild_now = value
		if value:
			call_deferred("_rebuild")

func _rebuild() -> void:
	if not Engine.is_editor_hint():
		return
	var generated_target := get_node_or_null(generated_target_path)
	if generated_target == null:
		push_warning("Choose a generated-only map target before rebuilding.")
		return
	# Fill in documented APIs and observed tile references here.
```

6. 完成获批编辑后，仅对本批审批返回的项目相对路径调用 `reload_map_targets`。截图只能证明可见结果，不证明碰撞、可达性或运行时玩法语义。
