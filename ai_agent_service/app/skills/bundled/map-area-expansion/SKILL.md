---
name: map-area-expansion
schema-version: 2
description: 扩建已有地图的可达性增长流程、横版平台关卡专用规划（critical route/jump graph/coin arcs/enemy slots）、移动模型能力校准与导航烘焙。
when_to_use: 扩展已有地图（而不是从空白区生成）、横版平台跳跃关卡设计、需要校准 leap/free 移动能力参数、或需要导航网格烘焙时加载。
required-capabilities: [category:context_read, category:plan, category:platform_plan]
compatible-roles: [map_planner, map_worker]
compatible-stages: [plan]
compatible-modes: [propose_only, repair_propose]
paths: []
---

## 可达性增长优先于空白生成

现有地图规划必须先绑定运行时提供的 `authoritative_map_snapshot_v1`。planner 只消费其中的受限投影：target/layer/revision、coverage/occupancy、已确认 traversal profile、entry/boundary/reachable frontier、语义资源键和 reference-cell 坐标。逐格 `source_id`/`atlas_coords`/`alternative_tile` 或 GridMap item/orientation 属于 validator/compiler 的写入编译事实，不得由 planner 输出或从摘要猜测。

snapshot 缺失、coverage 不完整、revision/digest stale 或 traversal/frontier 不完整时，返回明确的 typed `missing_inputs`/refresh/recompute 请求，由 reader 或确定性 frontier 工具派生新 snapshot；不得临时调用区域读取工具建立第二套事实基线。对话压缩后使用注入的 snapshot locator 和 `read_planning_snapshot` 恢复投影。

扩展已有地图时优先使用 `plan_reachable_map_growth`，从真实可达 frontier 生成候选、批次和修复策略。profile 选择：横版跳跃用 `platformer`，俯视道路用 `topdown`，房间走廊用 `dungeon`，3D GridMap 用 `3d_grid`。

存在真实玩家/单位起点时，先用 `compute_reachable_frontier` 按最终校验相同的移动事实计算可达集合，再把 `reachable_frontier` 交给增长规划；不得把视觉边界当成已可达边界。移动事实必须分别声明 `cell_occupancy`、`requires_support`、`support_occupancy`，不得用一个布尔值混合“角色占用格”和“支撑格”。起点/终点/前沿用 `role="actor_cell"` 或 `role="support_cell"` 明确坐标含义。

2D 区域必须显式提供 `x/y/width/height`；3D GridMap 还必须提供 `z/depth`。3D 或非向右增长用 `frontier_axis`/`frontier_sign` 指定增长方向。

扩建既有地形或背景时，planner 只引用 snapshot 中的 semantic resource 或 canonical reference cell；validator/compiler 负责把引用解析为真实 2D source/atlas 或 3D item/orientation。只有新区域才使用已核实的 catalog/registry 资源。多层 TileMap 必须沿用 snapshot 的真实 `map_layer`。

## 横版平台关卡专用规划

平台路线不能用通用 zone/Poisson 代替。planner 必须根据真实边界与能力显式设计有序 `platforms`/`segments`，按需附带 `coin_arcs`/`enemy_slots`，再调用 `validate_platform_level_plan`。该工具只校验和编译，不生成或自动修补路线。

不可执行的方案按字段级 `issues`/`repair_plan` 修改；禁止原样重试、只改 seed/区域宽度，或手写 ground fill。可玩路线、背景和装饰保持独立语义。

同一 task/target/layer/snapshot/operation 最多提交三次确定性平台校验。第二、三次必须消费上一轮字段级 repair plan。第三次仍失败时，仍以最后候选输出 `planning_status=delivered`、`execution_status=blocked_by_validation` 和未解决问题；不得生成 approved batch，也不得进入写入。

扩展已有平台地图时使用 `connect_from_existing=true`，并以真实 `entry_anchor` 作为路线起点。jump graph、score 或终点缓冲对平台间距、高度、落脚宽度、挑战段和终点平台构成约束。新区内部自洽不等于与旧地图连通。

`leap`/`free` 能力参数必须由角色控制器、项目设置和真实 tile/cell size 换算，覆盖水平距离、上升、下落、步高和最小落脚宽度；读取不到就返回 `missing_inputs`。非标准重力使用 `gravity_axis`/`gravity_sign`。校验区域必须包含角色格和对应支撑格；`support_outside_region` 不能按“区域外地面默认延续”处理。

`suggested_foothold` 是规划参考信息，不是已验证的落脚点。

`repair_map_region` 只适用于非设计类连通性、overlap 或 blocked-object 问题；它不适合修补平台形态、路线质量或终点缓冲等设计问题。

## 导航网格烘焙

存在 NavigationRegion 时，结构性修改后可调用 `bake_navigation_mesh`；空导航或 fallback 时改用 `validate_map_region(path_algorithm="astar")` 校验真实入口、出口或 waypoints。没有导航节点时不临时创建复杂导航。
