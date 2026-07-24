---
name: map-area-expansion
description: 扩建已有地图的可达性增长流程、横版平台关卡专用规划（critical route/jump graph/coin arcs/enemy slots）、移动模型能力校准与导航烘焙。
when_to_use: 扩展已有地图（而不是从空白区生成）、横版平台跳跃关卡设计、需要校准 leap/free 移动能力参数、或需要导航网格烘焙时加载。
allowed-tools: [plan_reachable_map_growth, compute_reachable_frontier, validate_platform_level_plan, validate_map_region, validate_layer_coverage, repair_map_region, bake_navigation_mesh, describe_map_region, query_spatial_index, read_scene_tree, read_file, read_class_docs, edit_map, capture_viewport_screenshot]
paths: []
---

加载前提：reader 已确认 `target_path`、revision、真实边界、资源和任务所需移动能力。legacy TileMap 还必须明确 `map_layer`；TileMapLayer/GridMap 不传 `map_layer`。事实不足时返回 reader，不得用默认值证明可玩性。

## 可达性增长优先于空白生成

扩展已有地图时优先使用 `plan_reachable_map_growth`，从真实可达 frontier 生成候选、批次和修复策略。profile 选择：横版跳跃用 `platformer`，俯视道路用 `topdown`，房间走廊用 `dungeon`，3D GridMap 用 `3d_grid`。

存在真实玩家/单位起点时，先用 `compute_reachable_frontier` 按最终校验相同的移动事实计算可达集合，再把 `reachable_frontier` 交给增长规划；不得把视觉边界当成已可达边界。移动事实必须分别声明 `cell_occupancy`、`requires_support`、`support_occupancy`，不得用一个布尔值混合“角色占用格”和“支撑格”。起点/终点/前沿用 `role="actor_cell"` 或 `role="support_cell"` 明确坐标含义。

2D 区域必须显式提供 `x/y/width/height`；3D GridMap 还必须提供 `z/depth`。单次区域最多 1600 cells，超限时使用工具返回的 `suggested_regions`。3D 或非向右增长用 `frontier_axis`/`frontier_sign` 指定增长方向。后续规划与校验原样传递 `planning_contract`；目标、区域、锚点、移动事实或地图 revision 冲突时重新读取，不得改参数碰运气。

扩建既有地形或背景时，从 reader 的边界事实复制真实 2D source/atlas 或 3D item/orientation；只有新区域才使用已核实的 catalog/registry 资源。多层 TileMap 必须沿用真实 `map_layer`。

planner 输出遵守工具限制的有序小批次、`expected_cells` 和 postconditions；writer 只执行这些批次，不在执行中重新规划。

## 横版平台关卡专用规划

平台路线不能用通用 zone/Poisson 代替。planner 必须根据真实边界与能力显式设计有序 `platforms`/`segments`，按需附带 `coin_arcs`/`enemy_slots`，再调用 `validate_platform_level_plan`。该工具只校验和编译，不生成或自动修补路线。

只有 `jump_graph.passed=true`、`score.passed=true`、`blocked_reason` 为空且 `ability_used_defaults` 为空时，writer 才能执行原样返回的 `edit_map_batches`。否则按字段级 `issues`/`repair_plan` 修改显式计划；禁止原样重试、只改 seed/区域宽度，或手写 ground fill。可玩路线、背景和装饰保持独立语义。

扩展已有平台地图时使用 `connect_from_existing=true`，并以真实 `entry_anchor` 作为路线起点。`entry_anchor_not_found` 时调整采样区域重新读边界；jump graph、score 或终点缓冲失败时修改平台间距、高度、落脚宽度、挑战段或终点平台。新区内部自洽不等于与旧地图连通。

`leap`/`free` 能力参数必须由角色控制器、项目设置和真实 tile/cell size 换算，覆盖水平距离、上升、下落、步高和最小落脚宽度；读取不到就返回 `missing_inputs`。非标准重力使用 `gravity_axis`/`gravity_sign`。校验区域必须包含角色格和对应支撑格；`support_outside_region` 不能按“区域外地面默认延续”处理。

执行后由 validator 在同一区域使用 `movement_model="leap"`、真实能力参数和 `check_platform_design=true`。`suggested_foothold` 只用于 diagnostic 或下一版规划，不能修改已冻结的 completion 端点。

`repair_map_region` 只适用于非设计类连通性、overlap 或 blocked-object 问题，并沿用原验证参数；平台形态、路线质量或终点缓冲失败必须回 planner。修复只有 `repaired=true` 才算成功。

## 导航网格烘焙

存在 NavigationRegion 时，结构性修改后可调用 `bake_navigation_mesh`；空导航或 fallback 时改用 `validate_map_region(path_algorithm="astar")` 校验真实入口、出口或 waypoints。没有导航节点时不临时创建复杂导航。
