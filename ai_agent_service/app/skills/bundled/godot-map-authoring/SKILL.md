---
name: godot-map-authoring
description: Bootstrap and maintain code-driven Godot map authoring without guessing serialized map data.
when_to_use: Use when creating or editing TileMapLayer, legacy TileMap, or GridMap content.
allowed-tools: [describe_tilemap_selection, describe_map_region, read_scene_tree, read_file, grep_code, read_class_docs, read_debugger_errors, apply_text_edit, propose_script_edit, propose_content_file, reload_map_targets, rebuild_map_builder, capture_viewport_screenshot]
paths: []
---

为 Godot 地图建立或维护可读、可审核的作者入口。不要根据模型记忆手拼 `.tscn` 中的 TileMap/GridMap 序列化 cell-data。

## 先理解要保留什么

先把当前场景里观察到的地图当作用户的既有作品，而不是等待导出的数据。选择作者方式前，判断请求的编辑尺度：

- 局部增量：增加、延伸、修复、移动少量已命名的格子/结构。先观察目标邻域，明确新增差量与需要保留的结构；不要因为缺少 builder 就把人工图层重建成 layout。
- 生成或迁移：用户明确要求生成、重建、迁移或参数化布局，或者已确认目标本来就是专用 generated-only 区域。此时 layout/builder 是合适的表达。

这不是关键词开关：结合用户意图和已观察事实判断。一个有用的自检是：**若我的方案需要重新写回用户没有要求改动的既有格子，我是在重建而不是局部编辑。** 重新选择局部方案、提出只承载新增格子的独立增量层，或在地图含义会实质改变时解释歧义。

对比示例：

- “从现有草地右端延伸 10 格”，且右侧有塔：读取塔和地板附近的事实；塔仍是要保留的事实。提出接到塔前、接为台阶或询问用户的局部选择，不能删除塔来得到一条直线。
- “根据宽度、难度和种子重新生成这一区域”：这是生成任务，可以为专用 generated-only 目标建立 layout/builder，并说明生成边界。
- 只读取过几段人工 TileMap 后，把它们抄进 JSON 再 `clear_layer` 回填：这是不完整的迁移，不是扩建，也不能宣称保留原图。

按下面顺序工作：

1. 确认真实目标、图层、TileSet/GridMap 事实：`describe_tilemap_selection` 仅当编辑器确实选中了 `TileMapLayer` 时可用（不能发现节点、不支持 legacy TileMap/GridMap）；无选择、legacy 目标或收到无选择错误时，用场景事实确认节点路径后改调小范围 `describe_map_region(target_path=...)`，不要重试无参选择调用。大范围结果若 `truncated`，读取 `observed_bounds`、`row_runs` 和 `next_query`，只继续查询所需边界。
2. 用 `read_file` 和 `read_scene_tree` 查找已有的 layout、生成器、配置及其挂载节点；编辑器上下文或检索给出的已知路径直接读取，不要重复搜索。确需 `grep_code` 兜底时，`include` 只用源码/配置 glob（如 `**/*.gd`、`**/*.json`、`**/*.cfg`、`**/*.tscn`），绝不使用 `**/*`。已有入口时只编辑这些可读文件，并保留手工图层。
3. 根据编辑尺度选择作者入口。局部请求先保留人工地图，并优先使用现有的增量入口；没有入口时，可提出只写入新增格子的独立增量层，但不能把“缺少入口”当作复写原图的理由。明确生成/迁移任务时，直接发出 bootstrap 的第一个可审批工具调用；内联审批卡片就是确认，不要先输出“是否继续”或等待额外文字批准。按一次一个工具调用的协议，依次完成下列 bootstrap 批次：
   - 新建人可读的 layout/config（推荐 JSON、CFG 或 GDScript 常量），只表达格子坐标、语义标记和已观察到的 tile 引用；layout 与 builder 文件**一律写入固定目录 `res://map_layouts/`**（前端校验强制，其它目录会被拒绝）；
   - 在 `res://map_layouts/` 下新建带 `@tool` 的 builder 脚本；它暴露生成目标和 layout 路径（`layout_path` 指向 `res://map_layouts/` 下的布局文件），并在编辑器中由明确的重建动作驱动；
   - builder `.gd` 写入结果没有编译错误后，下一步必须在已读取场景上发出普通审批的 `apply_text_edit`：新增或指定一个 generated-only 图层/节点，挂载 builder 脚本，设置 `generated_target_path`、`layout_path`，并明确写入 `generated_target_is_generated_only = true`。该场景编辑获批前，不得 reload 或 rebuild。不要覆盖已有手工 TileMapLayer、TileMap 或 GridMap；迁移旧内容前必须取得用户明确批准。
4. 在填入任何具体 Godot 调用前，必须先调用 `read_class_docs` 查询实际目标类型：先 `overview`，不清楚成员名时先 `search`，然后以 `members` 只请求将调用的精确签名（如 legacy TileMap 的 `set_cell`、`clear_layer`）。仅在已确认的专用 generated-only 目标中，才按文档实现清空、读取 layout、设置 cell/mesh 和编辑器重建；局部人工地图编辑应保持为新增差量，而不是重现未变内容。不要凭记忆猜 API，也绝不请求完整 ClassDB。
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

6. 获批写入 builder `.gd` 后，首先检查工具结果携带的 `write_applied`、`post_write_validation`、路径观察事实与 `builder_diagnostics`。出现编译错误、空 builder 或空 layout 时，不得 reload、重建或截图；读取指定源文件，必要时调用 `read_debugger_errors`，然后只提出一次普通审批修复。只有 `builder_script_missing` 且 `exists=false` 才说明文件缺失；已存在但无法解析必须按编译/加载问题修复。完成获批编辑后，仅对本批审批返回的项目相对路径调用 `reload_map_targets`；即使模型把 scene 排在前面，前端也会先重载 `.gd`/`.tres` 再重载 `.tscn`。若获批的是已建立 builder 的 layout，先读取场景树确认 builder 节点，再调用 `rebuild_map_builder`；该工具由 Godot 编辑器对已挂载实例调用固定的 `rebuild_from_layout()`，不是 reload `.gd`、不是运行游戏、不是调用 `_ready`/`_process`，也不接受任意方法或脚本路径。`builder_instance_stale` 表示先重载关联场景；`builder_repair_required` 表示相同 source/layout/scene 已失败，必须先有获批修复修改。`blocked`、`failed`、`unavailable` 是需要向 LLM 返回的错误证据，不得盲目重写 builder。截图只能证明 `rebuilt` 后的可见结果，不证明碰撞、可达性或运行时玩法语义。地图视觉截图只能以显式 `map_region`（路径、`map_layer`、有限 `cell_bounds`）作补充证据；自动整编辑器截图只是诊断，必须另以匹配的 `describe_map_region` 作为确定性地图验证。局部编辑完成后，复查变更区与其紧邻的原有结构，分别报告新增差量、已观察到的保留事实和任何意外变化；不要把截图或写入成功表述为完整地图未受影响的证明。
