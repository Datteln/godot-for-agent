---
name: map-procedural-generation
description: 从零批量生成/装饰地图的算法栈（zone planning、Poisson/noise 采样、grammar/blueprint 模板、草图转地图）。
when_to_use: 用户要求大范围生成、装饰、村庄/地牢/房间/道路/资源分布、"自然分布"、模板复用、或草图/参考图转地图时加载。
allowed-tools: [plan_map_layout, plan_map_algorithms, sample_poisson_points, sample_noise_grid, compose_map_blueprint_grammar, save_map_blueprint, apply_map_blueprint, read_image_metadata, paint_from_image_grid, edit_map, paint_terrain_connect, place_map_objects, validate_map_region, repair_map_region]
paths: []
---

加载前提：已完成认知阶段（`read_scene_tree`/`describe_map_context` 确认地图节点、资源语义表、空间索引状态），并已用 [[map-agent]] 核心规则确定目标节点/图层。

## 复杂生成/装饰任务

复杂生成/装饰/替换任务先调用 `plan_map_layout`，让工具把自然语言压成结构化 `MapIntent`，并输出布局 zone、anchor、资源缺口、标准层需求、`edit_map` 操作草案和校验计划。只有资源缺口为 0、目标路径和图层确认后，才进入小批 `edit_map` 执行。

通用地图生成/重构默认采用这套算法栈：`Zone Planning → Poisson Disk Sampling → Grammar/Blueprint Composer → A*/NavMesh Validation → Constraint Validator/Repair`。大范围生成、装饰、村庄/地牢/房间/道路/资源分布等任务，先用 `plan_map_layout`；如果需要更通用的结构化算法输出，调用 `plan_map_algorithms`。执行时不要直接照脑内随机坐标落子，而是把返回的 `zones` 转成基础地形/区域批次，把 `poisson_points` 转成 `place_map_objects` 或装饰 `edit_map`，把 `grammar.stamps` 转成 `apply_map_blueprint`，最后按 `constraints.validate_map_region` 校验并按 `repair_map_region` 修复。

## 自然分布与密度采样

需要"自然分布"（树木/岩石/草地深浅按密度散布，而不是整齐平铺）时，用 `sample_noise_grid` 采样一块归一化噪声网格，对返回的 0..1 值设阈值决定每格放不放、放什么；固定 `seed` 保证可复现。不要在 reasoning 里手编随机分布。

对象、资源点、敌人、宝箱、装饰物这类离散点位，优先用 `sample_poisson_points` 而不是手写随机坐标；根据 `min_distance` 控制稀疏度，根据 `max_points` 控制数量，必要时传入 `zones` 和 `zone` 只在指定语义区采样。

Poisson 点用于"放在哪里"，noise 用于"这一片密度/变化有多强"，两者不要混淆。

## 模板复用

需要模块化复用时优先 `compose_map_blueprint_grammar`：有已保存模板时让它产出 `apply_map_blueprint` stamping 计划；没有模板时使用返回的 fallback drafts，再用 `edit_map`/`paint_terrain_connect` 落地。不要把"再来一个这样的塔/房间/桥"重新逐格发明一遍。

用户说"把这块存成模板""再来一个这样的 X"时：先 `save_map_blueprint` 把选定区域的真实瓦片/网格存成模板，之后用 `apply_map_blueprint` 平移到新原点复用。模板复用优先于重新逐格拼，能最大程度保持和原作一致。

## 草图/参考图转地图

草图/参考图转地图先用 `read_image_metadata` 理解尺寸和颜色，再用 `paint_from_image_grid` 生成可撤销 TileMap 改动。
