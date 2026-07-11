---
name: map-area-expansion
description: 扩建已有地图的可达性增长流程、横版平台关卡专用规划（critical route/jump graph/coin arcs/enemy slots）、移动模型能力校准与导航烘焙。
when_to_use: 扩展已有地图（而不是从空白区生成）、横版平台跳跃关卡设计、需要校准 leap/free 移动能力参数、或需要导航网格烘焙时加载。
allowed-tools: [plan_reachable_map_growth, compute_reachable_frontier, plan_platform_level, validate_map_region, validate_layer_coverage, repair_map_region, bake_navigation_mesh, describe_map_region, query_spatial_index, read_scene_tree, read_file, read_class_docs, edit_map, capture_viewport_screenshot]
paths: []
---

加载前提：已完成认知阶段，已确定 target_path/map_layer，并已读过 [[map-agent]] 核心规则里关于 `movement_model`（grid/leap/free）的选型说明。

## 可达性增长优先于空白生成

扩展已有地图时优先考虑 `plan_reachable_map_growth`，而不是从空白区凭空生成。它把地图增长抽象成 `frontier → candidates → accepted_motifs → edit_map_batches → validation → repair_strategies`，支持 `profile="platformer"`、`topdown`、`dungeon`、`3d_grid`。选择 profile：横版跳跃用 platformer；俯视 RPG/道路用 topdown；房间走廊用 dungeon；3D GridMap 地板/室内用 3d_grid。执行前必须确认 frontier 来自真实已可达区域；没有 frontier 时先用 `describe_map_region`/`query_spatial_index` 定位可达边界。

如果用户给了玩家/单位真实起点，或项目里能从场景/脚本定位起点，扩展地图前必须先用 `compute_reachable_frontier` 在真实地图 cell 上计算可达集合。`plan_reachable_map_growth` 应使用返回的 `rightmost_frontier` 作为 `frontier`；不得把"左侧边界看起来有平台"当成已可达。`compute_reachable_frontier` 的 `movement_model` 和能力参数必须与后续 `validate_map_region` 一致。

扩建已有地形/背景前必须先用 `describe_map_region` 读边界附近真实 `source_id`/`atlas_coords`（或 3D 的 `item`/`orientation`），用 `copy` 原样延伸；只有新绘制区域才用上下文里的 tile_catalog 或 MeshLibrary item。目标是多图层 legacy TileMap 时，必须先确认正确 `map_layer`，不要默认第 0 层。

大范围地形按「读边界 → 写块计划 → 小批 `edit_map` → 核对结果 → 必要时重读」执行。单次 `edit_map` 单轴范围不超过 5 格；动手前说明区域、动作、来源边界、预期 cells；调用时传 `expected_cells`。闭区间格数按 `B-A+1` 算：`x=85..87` 是 3 列，不是 2 列。每铺完一段独立地形（平台/阶梯/悬浮台）就地 `validate_map_region`，不要全部铺完再一次性校验。

`edit_map` 的 `expected_cells` 必须等于所有 operations 实际写入单元数之和（fill/erase 为 `width * height * depth`，2D depth=1）；总写入不得超过 2000 cells。遇到 `cell_count_mismatch`、`map_edit_batch_too_large` 或 `region_too_large` 时按返回的实际数量、`max_cells` 或 `suggested_regions` 重拆，不要原样重试。

`describe_map_region` 的读取频率：处理一段新地形前第一批必须读边界；后续只在衔接边界没读过、上一批返回的 `cells`/`operations` 数量不符、或发现边界/空洞/已有节点推翻当前假设时补读。`region_too_large` 时照返回的 `suggested_regions` 读，不要自己再拆。

## 横版平台关卡专用规划

横版平台跳跃关卡不要只用通用 zone/Poisson 算法。用户目标是平台游戏、Mario/Celeste 类跳跃、Brackeys 平台地图、关卡主路径、跳跃/落点/金币弧线/敌人槽位时，优先调用 `plan_platform_level`。它会先生成 critical route、platform motifs、jump_graph、`edit_map_batches`、`coin_arcs`、`enemy_slots` 和 `movement_model="leap"` 校验计划；再把批次转成小批 `edit_map`，把奖励弧线/敌人槽位转成真实资源放置。不要先随便铺瓦片再事后 A* 校验。

`plan_platform_level` 不是只管能不能跳到，它也会执行平台关卡形态语法：平台默认 1-2 格厚、非休息段限制最大宽度、终点前必须有安全平地、重复挑战形状会被扣分/拒绝。返回 `score.passed=false` 或 `blocked_reason="score_issues"` 时，不要执行 `edit_map_batches`，先调小 `max_platform_width`/`max_platform_thickness`、调大 `min_finish_buffer_width` 或换 seed 重新规划。目标是先生成一串可读的落点/表面，再用少量支撑和装饰表达形状；禁止把新区铺成连续厚墙、密集竖柱阵列或大块实心矩形。

扩展已有横版地图时，`plan_platform_level`/`plan_reachable_map_growth` 必须默认 `connect_from_existing=true`，并传 `target_path`/`map_layer` 让工具扫描左侧边界已有表面，返回 `entry_anchor`。返回的 `blocked_reason` 非空（`entry_anchor_not_found`/`jump_graph_failed`/`score_issues`）时，`edit_map_batches` 已经被工具结构性清空，不需要你自己再判断要不要执行——但仍要按 `blocked_reason` 处理：`entry_anchor_not_found` 时扩大/移动 `entry_sample_*` 重新找边界落脚点；`jump_graph_failed`/`score_issues` 时降低新平台起点高度、缩短 gap 或增大 landing_width 后重新规划。右侧新区内部可达不算完成，必须从左侧初始地图的真实落脚点一路可达。

`plan_platform_level` 返回的 `ability_used_defaults` 非空时，说明你没传 `max_horizontal_gap`/`max_rise`/`max_fall`/`min_landing_width` 中的某些字段，工具用了写死的默认值（4/2/6/3），这条规划**不能**当作"已验证可玩"——先按下面"能力校准"读真实角色脚本和 `tile_size` 补全这些参数再重新调用，不要因为它返回了 `ok:true` 就直接执行。

调用 `plan_platform_level` 前也遵守同一条：先读取角色脚本和 tile_size，把 `max_horizontal_gap`、`max_rise`、`max_fall`、`min_landing_width` 传进去。`plan_platform_level` 返回的 `validation.validate_map_region.start` 必须是左侧已有 `entry_anchor`，不是新区第一块平台；返回的 `jump_graph.passed=false` 或 `score.issues` 不为空时，不要执行它的 `edit_map_batches`，先缩小 gap、增加 landing_width 或降低 vertical_delta 后重新规划。

执行后用 `validate_map_region(movement_model="leap", check_platform_design=true)` 校验同一片区域。`platform_design.passed=false` 与可达性失败同级：长实心行、过高竖柱、大块实心体量或终点缓冲不足都必须通过重规划/拆薄/删柱修复，不能只靠 `repair_map_region` 补一条桥。

## leap/free 能力校准（通用铁律）

`leap`/`free` 的能力参数（`max_horizontal_gap`/`max_rise`/`max_fall`/`max_step`）**必须按角色控制器里的真实移动能力换算成格数**，不准凭感觉编。校验前先 `read_file` 读真实的角色脚本（移动速度、跳跃速度/初速度、重力、是否能飞/游泳/二段跳等）和项目设置，结合 `describe_map_region` 读到的真实 `tile_size`/`cell_size` 把"能跳多远/多高"换算成格数再传进去。读不到真实数值就向用户说明缺少哪个参数，不要用假设值去"证明"可玩性。

`leap` 校验的区域要把落脚平台正下方那一行/层地面也包含进 `width`/`height`/`depth` 内，否则支撑判定会因为区域裁剪而失真。非标准重力方向（横向重力、3D 里地面不在 -y 等）用 `gravity_axis`/`gravity_sign` 覆盖。

`validate_map_region` 返回起点/终点不是合法落脚点时，优先使用结果里的 `suggested_foothold` 作为新的 start/goal 重试。地表瓦片本身是实心格，玩家落脚点通常是它正上方的空格，不要从原始瓦片逐格反推。

如果返回 `repair_plan`，优先用 `repair_map_region` 应用修复，并传与校验完全相同的 `movement_model` 和能力参数。连通性修复传 start/goal；重叠修复传 `repair_overlaps=true`；对象压水/障碍修复传 `repair_blocked_objects=true`。修完必须重跑校验。

`repair_map_region` 返回 `ok=true` 或 `changed=true` 不代表修好；只有 `repaired=true` 才算完成。`repaired=false` 时按 `validation_after.reason`/`repair_plan` 重新定位，必要时 `describe_map_region` 重读。同一段连续修复失败 2 次以上，停止硬试坐标，先重读真实数据。

`validate_map_region.passed=true` 只证明在当前移动假设下可达，不等于设计合理或任务完成。通过后仍要复核关键缺口、落点和终点；可用 `capture_viewport_screenshot` 传 `focus_region` + `target_path`，但截图只做视觉复核，真实数据仍以读取工具为准。

## 导航网格烘焙

如果存在 `NavigationRegion2D` 或 `NavigationRegion3D`，地图生成或结构性改动后可调用 `bake_navigation_mesh`；如果返回空导航 warning 或 `fallback`，立即用 `validate_map_region(path_algorithm="astar")` 对入口/出口/waypoints 做结构化降级校验，并在最终说明里说清楚。没有导航节点时不要临时硬造一套复杂导航，只提示未做导航烘焙并给出 A* 结构校验结果。
