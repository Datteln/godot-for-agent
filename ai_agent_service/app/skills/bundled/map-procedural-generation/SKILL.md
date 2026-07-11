---
name: map-procedural-generation
description: 从零批量生成/装饰地图的算法栈（zone planning、Poisson/noise 采样、grammar/blueprint 模板、草图转地图）。
when_to_use: 用户要求大范围生成、装饰、村庄/地牢/房间/道路/资源分布、"自然分布"、模板复用、或草图/参考图转地图时加载。
allowed-tools: [plan_map_layout, plan_map_algorithms, sample_poisson_points, sample_noise_grid, compose_map_blueprint_grammar, save_map_blueprint, apply_map_blueprint, ensure_standard_map_layers, read_scene_tree, read_image_metadata, query_spatial_index, find_placement_anchors, validate_object_placements, repair_placements, paint_from_image_grid, edit_map, fill_rect, paint_terrain_connect, place_map_objects, validate_layer_coverage, validate_map_region, repair_map_region, compact_spatial_index]
paths: []
---

加载前提：已完成认知阶段（`read_scene_tree`/`describe_map_context` 确认地图节点、资源语义表、空间索引状态），并已用 [[map-agent]] 核心规则确定目标节点/图层。

## 复杂生成/装饰任务

复杂生成/装饰/替换任务先调用 `plan_map_layout`，让工具把自然语言压成结构化 `MapIntent`，并输出布局 zone、anchor、资源缺口、标准层需求、`edit_map` 操作草案和校验计划。只有资源缺口为 0、目标路径和图层确认后，才进入小批 `edit_map` 执行。

通用地图生成/重构默认采用这套算法栈：`Zone Planning → Poisson Disk Sampling → Grammar/Blueprint Composer → A*/NavMesh Validation → Constraint Validator/Repair`。大范围生成、装饰、村庄/地牢/房间/道路/资源分布等任务，先用 `plan_map_layout`；如果需要更通用的结构化算法输出，调用 `plan_map_algorithms`。执行时不要直接照脑内随机坐标落子，而是把返回的 `zones` 转成基础地形/区域批次，把 `poisson_points` 转成 `place_map_objects` 或装饰 `edit_map`，把 `grammar.stamps` 转成 `apply_map_blueprint`，最后按 `constraints.validate_map_region` 校验并按 `repair_map_region` 修复。

如果用户要求搭建新地图骨架，或当前场景缺少清晰地图分层，先调用 `ensure_standard_map_layers`。2D 复用/补齐 `GroundLayer`、`WaterLayer`、`RoadLayer`、`ObstacleLayer`、`DecorLayer`、`ObjectLayer`；3D 复用/补齐 `GridMap`、`PropsRoot`、`LightsRoot`、`InteractRoot`。不要重复创建同名层。

大型生成必须先规划主路径、入口、出口和可通行区域，再填建筑、障碍和装饰。障碍、墙体、树木、房屋等不得阻断主路径；2D 核对道路/平台/河岸连通，3D 核对地板连续、墙体闭合、门格可通行、重要 Node3D 落在地板上。

## 自然分布与密度采样

需要"自然分布"（树木/岩石/草地深浅按密度散布，而不是整齐平铺）时，用 `sample_noise_grid` 采样一块归一化噪声网格，对返回的 0..1 值设阈值决定每格放不放、放什么；固定 `seed` 保证可复现。不要在 reasoning 里手编随机分布。

对象、资源点、敌人、宝箱、装饰物这类离散点位，优先用 `sample_poisson_points` 而不是手写随机坐标；根据 `min_distance` 控制稀疏度，根据 `max_points` 控制数量，必要时传入 `zones` 和 `zone` 只在指定语义区采样。

Poisson 点用于"放在哪里"，noise 用于"这一片密度/变化有多强"，两者不要混淆。

离散 PackedScene 对象必须先用 `find_placement_anchors` 或 `validate_object_placements` 拿到合法坐标，再用 `place_map_objects` 放置。树木、建筑、NPC、敌人、宝箱、陷阱/killzone 等都服从同一套 placement profile：anchor、footprint、支撑层、禁放层、clearance、同类最小距离、surface_type、保护路径/房间中心/支线末端/路径边缘等。需要玩家可拾取、交互或到达时传 `requires_reachable=true`、`start` 和正确 `movement_model`。

`surface_type=room_center`/`branch_end`/`path_edge` 时分别传 `room_centers`/`branch_ends`/`path_cells`（或 `route_cells`/`protected_cells`），让工具从集合提取候选。批量校验里任何候选失败，替补坐标也必须重新走 `find_placement_anchors`/`validate_object_placements`；不要凭手感挑临近格。

放树木、岩石、灌木等装饰对象前，先用 `query_spatial_index` 或 `read_scene_tree` 查 `ObjectLayer`/`PropsRoot` 下是否已有同语义实例；找到了就复用它的 `scene_path`/属性。修复已有对象摆放错误优先用 `repair_placements`，不要手动猜新坐标重复放置。

资源语义表条目有 `terrain_set`/`terrain` 时，水域、道路、草地等连续地形优先 `paint_terrain_connect`；条目有 `scene_path` 时必须 `place_map_objects`，不要把对象伪装成瓦片。遇到 `resource_requires_object_placement` 立即改用 `place_map_objects`。`plan_map_layout` 给出 `fallback_resources` 时，把 `fallback_resource` 传给写入工具。

资源只使用本轮真实读取或已核实的 registry 项；registry entry 至少包含 `kind` 和真实 2D `source_id`+`atlas_coords` 或 3D `item`/`mesh_library_item` 或 `scene_path`。禁止发明 key、atlas 或 item；索引与真实区域冲突时以区域读取为准。用瓦片表达可见物体时，所有 operations 共享 `visual_group_id`/`instance_id`、`instance_kind`、`required_cells`，写入后按实例复核。

需要后续局部删除、替换或模板复用的内容，写入时补 `resource`/`resource_key`、`semantic_layer`、`tags`、`cost` 并传 `update_spatial_index=true`。索引接近上限、重生成大片区域、或刚删除/替换大块内容后，调用 `compact_spatial_index` 清掉旧索引。

## 模板复用

需要模块化复用时优先 `compose_map_blueprint_grammar`：有已保存模板时让它产出 `apply_map_blueprint` stamping 计划；没有模板时使用返回的 fallback drafts，再用 `edit_map`/`paint_terrain_connect` 落地。不要把"再来一个这样的塔/房间/桥"重新逐格发明一遍。

用户说"把这块存成模板""再来一个这样的 X"时：先 `save_map_blueprint` 把选定区域的真实瓦片/网格存成模板，之后用 `apply_map_blueprint` 平移到新原点复用。模板复用优先于重新逐格拼，能最大程度保持和原作一致。

局部替换不能全量重绘。先用 `query_spatial_index` 按 tags、semantic_layer、resource 或坐标范围定位，再 `describe_map_region` 核实小块区域，生成最小 `erase`/`fill`/`copy` 操作；空间索引为空时退回只读必要区域。

`edit_map` 或 `validate_map_region` 返回 `layer_coverage_gaps` 非空时，任务未完成。背景、天空、水面等毯式图层缺口优先用 `validate_layer_coverage`/`repair_layer_coverage`，直到 `layer_coverage_gaps=[]`；不要只因写入工具 `ok:true` 就结束。

关键连通性用 `validate_map_region` 校验 `start`/`goal`、`entrances`/`exits` 或有序 `waypoints`。复杂绕障碍用 `path_algorithm="astar"`；对象密集区传 `check_overlaps=true`，对象压水/障碍检查传 `check_blocked_objects=true`，有明确范围时传 `allowed_bounds`。

`repair_map_region` 后只有 `repaired=true` 才算修好；`repaired=false` 时按 `validation_after`/`repair_plan` 重新定位。连续修复失败 2 次以上，先重读真实区域，不要继续硬试坐标。

## 草图/参考图转地图

草图/参考图转地图先用 `read_image_metadata` 理解尺寸和颜色，再用 `paint_from_image_grid` 生成可撤销 TileMap 改动。

草图落地后仍要按真实 `describe_map_region` 数据校验资源、图层覆盖和连通性；截图只做视觉复核，不能替代读取工具。
