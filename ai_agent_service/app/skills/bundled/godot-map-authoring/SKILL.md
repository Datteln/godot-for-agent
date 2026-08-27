---
name: godot-map-authoring
description: Bootstrap and maintain code-driven Godot map authoring without guessing serialized map data.
when_to_use: Use when creating or editing TileMapLayer, legacy TileMap, or GridMap content.
allowed-tools: [describe_tilemap_selection, describe_map_region, read_scene_tree, read_file, grep_code, read_class_docs, read_debugger_errors, apply_text_edit, propose_script_edit, propose_content_file, reload_map_targets, rebuild_map_builder, capture_viewport_screenshot]
paths: []
---

为 Godot 地图建立或维护可读、可审核的作者入口。不要根据模型记忆手拼 `.tscn` 中的 TileMap/GridMap 序列化 cell-data。

按下面顺序工作：

1. 确认真实目标、图层、TileSet/GridMap 事实：`describe_tilemap_selection` 仅当编辑器确实选中了 `TileMapLayer` 时可用（不能发现节点、不支持 legacy TileMap/GridMap）；无选择、legacy 目标或收到无选择错误时，用场景事实确认节点路径后改调小范围 `describe_map_region(target_path=...)`，不要重试无参选择调用。大范围结果若 `truncated`，读取 `observed_bounds`、`row_runs` 和 `next_query`，只继续查询所需边界。
2. 用 `read_file` 和 `read_scene_tree` 查找已有的 layout、生成器、配置及其挂载节点；编辑器上下文或检索给出的已知路径直接读取，不要重复搜索。确需 `grep_code` 兜底时，`include` 只用源码/配置 glob（如 `**/*.gd`、`**/*.json`、`**/*.cfg`、`**/*.tscn`），绝不使用 `**/*`。已有入口时只编辑这些可读文件，并保留手工图层。
3. 没有入口时，直接发出 bootstrap 的第一个可审批工具调用；内联审批卡片就是确认，不要先输出“是否继续”或等待额外文字批准。按一次一个工具调用的协议，依次完成下列 bootstrap 批次：
   - 新建人可读的 layout/config（推荐 JSON、CFG 或 GDScript 常量），只表达格子坐标、语义标记和已观察到的 tile 引用；
   - 新建带 `@tool` 的 builder 脚本；它暴露生成目标和 layout 路径，并在编辑器中由明确的重建动作驱动；
   - builder `.gd` 写入结果没有编译错误后，下一步必须在已读取场景上发出普通审批的 `apply_text_edit`：新增或指定一个 generated-only 图层/节点，挂载 builder 脚本，设置 `generated_target_path`、`layout_path`，并明确写入 `generated_target_is_generated_only = true`。该场景编辑获批前，不得 reload 或 rebuild。不要覆盖已有手工 TileMapLayer、TileMap 或 GridMap；迁移旧内容前必须取得用户明确批准。
4. 在填入任何具体 Godot 调用前，必须先调用 `read_class_docs` 查询实际目标类型：先 `overview`，不清楚成员名时先 `search`，然后以 `members` 只请求将调用的精确签名（如 legacy TileMap 的 `set_cell`、`clear_layer`）。随后按文档实现清空、读取 layout、设置 cell/mesh 和编辑器重建；不要凭记忆猜 API，也绝不请求完整 ClassDB。
5. builder 至少使用这种编辑器安全骨架，并根据第 4 步的文档补全具体实现：

```gdscript
@tool
extends Node

@export_node_path("Node") var generated_target_path: NodePath
@export_file("*.json") var layout_path := ""
@export var generated_target_is_generated_only := true
@export var rebuild_now := false:
	set(value):
		rebuild_now = value
		if value:
			call_deferred("rebuild_from_layout")

func rebuild_from_layout() -> Dictionary:
	if not Engine.is_editor_hint():
		return {"ok": false, "error_code": "not_in_editor"}
	var generated_target := get_node_or_null(generated_target_path)
	if generated_target == null:
		push_warning("Choose a generated-only map target before rebuilding.")
		return {"ok": false, "error_code": "generated_target_missing"}
	# Fill in documented APIs and observed tile references here.
	return {"ok": true}
```

6. 获批写入 builder `.gd` 后，首先检查工具结果携带的 `write_applied`、`post_write_validation`、路径观察事实与 `builder_diagnostics`。出现编译错误、空 builder 或空 layout 时，不得 reload、重建或截图；读取指定源文件，必要时调用 `read_debugger_errors`，然后只提出一次普通审批修复。只有 `builder_script_missing` 且 `exists=false` 才说明文件缺失；已存在但无法解析必须按编译/加载问题修复。完成获批编辑后，仅对本批审批返回的项目相对路径调用 `reload_map_targets`；即使模型把 scene 排在前面，前端也会先重载 `.gd`/`.tres` 再重载 `.tscn`。若获批的是已建立 builder 的 layout，先读取场景树确认 builder 节点，再调用 `rebuild_map_builder`；该工具由 Godot 编辑器对已挂载实例调用固定的 `rebuild_from_layout()`，不是 reload `.gd`、不是运行游戏、不是调用 `_ready`/`_process`，也不接受任意方法或脚本路径。`builder_instance_stale` 表示先重载关联场景；`builder_repair_required` 表示相同 source/layout/scene 已失败，必须先有获批修复修改。`blocked`、`failed`、`unavailable` 是需要向 LLM 返回的错误证据，不得盲目重写 builder。截图只能证明 `rebuilt` 后的可见结果，不证明碰撞、可达性或运行时玩法语义。
