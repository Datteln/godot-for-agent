---
name: map-procedural-generation
description: 从零批量生成/装饰地图的算法栈（zone planning、Poisson/noise 采样、grammar/blueprint 模板、草图转地图）。
when_to_use: 用户要求大范围生成、装饰、村庄/地牢/房间/道路/资源分布、"自然分布"、模板复用、或草图/参考图转地图时加载。
allowed-tools: [plan_map_layout, plan_map_algorithms, sample_poisson_points, sample_noise_grid, compose_map_blueprint_grammar, save_map_blueprint, apply_map_blueprint, ensure_standard_map_layers, read_scene_tree, read_image_metadata, query_spatial_index, find_placement_anchors, validate_object_placements, repair_placements, paint_from_image_grid, edit_map, fill_rect, paint_terrain_connect, place_map_objects, validate_layer_coverage, validate_map_region, repair_map_region, compact_spatial_index]
paths: []
---

加载前提：reader 已确认 `target_path`、`map_layer`、revision、资源语义表、空间索引状态和任务区域。缺少真实资源或目标时返回 `missing_inputs`。

## 核心流程

复杂生成、装饰或替换先调用 `plan_map_layout`，获得结构化 `MapIntent`、zones、anchors、资源缺口、候选批次和校验计划。只有目标、图层和资源均确认后才能交给 writer。

算法按意图选择：

- 语义区域、主路径和整体结构：`plan_map_layout`；需要更通用算法输出时用 `plan_map_algorithms`。
- 连续密度或材质变化：`sample_noise_grid`。
- 自然间距的离散对象：`sample_poisson_points`。
- 模块化房间、桥、塔或重复结构：`compose_map_blueprint_grammar`。
- 草图或色块参考：`read_image_metadata` 后使用 `paint_from_image_grid`。

需要新地图骨架或缺少清晰分层时才调用 `ensure_standard_map_layers`，复用已有标准层。大型生成先规划入口、出口、主路径和可通行区域，再规划建筑、障碍和装饰；2D 保持道路/平台/河岸连通，3D 保持地板、墙体、门格和 Node3D 支撑关系。

## 对象与资源

noise 表示区域密度或变化强度，Poisson 表示离散点位；固定 seed 保证可复现，不手编随机坐标。

PackedScene 对象先用 `find_placement_anchors`/`validate_object_placements` 确认 anchor、footprint、支撑、禁放层、clearance、同类距离和可达性，再用 `place_map_objects`。room center、branch end、path edge 等语义候选必须来自对应事实集合；失败候选的替补位置也要重新校验。

优先复用空间索引或场景树中已有同语义实例的真实 `scene_path` 和属性；修复已有对象用 `repair_placements`，不重复放置。

资源按 registry 类型选择工具：terrain 使用 `paint_terrain_connect`，PackedScene 使用 `place_map_objects`，普通瓦片/网格使用 `edit_map`。只使用已核实的 source/atlas、item 或 scene path；真实区域与索引冲突时以真实区域为准。

可见瓦片对象使用稳定的 `visual_group_id`/`instance_id`、`instance_kind` 和 `required_cells`。需要后续定位或复用的内容补充 resource、semantic layer、tags、cost 并更新空间索引；大范围替换后按需 compact。

## 模板复用

“存成模板”先用 `save_map_blueprint` 保存真实区域；“再来一个这样的结构”优先用 `compose_map_blueprint_grammar` 和 `apply_map_blueprint` 平移复用，不重新逐格发明。

局部替换先用空间索引定位，再由 reader 核实必要小区域，只生成最小 `erase`/`fill`/`copy` 批次，不全量重绘。

## 草图/参考图转地图

读取图像尺寸和颜色后，用 `paint_from_image_grid` 生成有边界、可撤销的 TileMap 候选批次。

## 输出与验收交接

planner 输出有序候选批次、`expected_cells`、postconditions 和所需 validator constraints。主路径、背景、覆盖层和对象分别规划；对象不得阻断受保护路线。writer 执行后交给 validator 检查连通性、覆盖率、overlap/blocked 和实例完整性，再由 reviewer 做视觉复核。
