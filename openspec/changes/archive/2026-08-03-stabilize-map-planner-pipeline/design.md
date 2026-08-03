## Context

现有地图扩建流程允许 planner 临时调用区域读取工具、声明 traversal occupancy、直接形成带 atlas 的 `proposed_batches`，再调用平台 validator。planner、reader、validator 和 writer 之间通过对话内容、瘦身结果或 artifact 引用交接，关键事实虽然部分落盘，但没有一份统一、不可变、按 revision 绑定的规划输入。结果是上下文压缩、遗漏 artifact 回读、入口坐标误判或校验失败都可能触发重复规划；当重试耗尽时，依赖图把 writer 连同用户可见的规划交付一起阻塞。

现有系统已经具备 map revision、map artifacts、resource registry、reachable-frontier 计算、平台校验/批次编译、approved write batch 和 reducer-owned workflow state。本设计复用这些组件，增加明确的数据边界和状态机，不引入新的外部依赖。

## Goals / Non-Goals

**Goals:**

- 让 planner 的每次尝试都基于同一份完整、可追溯且 revision 一致的权威事实。
- 把路线设计与逐格 atlas 写入编译分开，避免 LLM 输出成为写入事实来源。
- 将同一快照上的 planner 确定性校验尝试严格限制为三次，并保证每次修复有结构化差异。
- 即使三次均失败，也向用户交付最后一次候选规划、校验历史和未解决问题。
- 只有通过确定性校验和编译的计划才能生成 approved batch 并解锁 writer。
- 让快照、repair plan、规划尝试和最终结果独立于对话压缩持久存在。

**Non-Goals:**

- 不改变通用 LLM 请求的 fallback/retry 策略。
- 不让 validator 自动设计、重写或主观优化平台路线。
- 不允许校验失败后的最终规划绕过写入门禁。
- 不以截图、空间索引摘要或 LLM 推断替代 canonical TileMap/TileMapLayer/GridMap 事实。
- 不在本 change 中重新定义 Godot Undo、事务 journal 或地图 revision 算法。

## Decisions

### 1. 引入不可变 `authoritative_map_snapshot_v1`

reader/snapshot builder 在 planner 启动前生成 artifact。核心内容包括：

- identity：`snapshot_id`、schema version、`target_path`、`map_layer`、dimension、`map_revision`、内容 digest；
- coverage：请求区域、逐区域 completeness、截断/遗漏计数和 used bounds；
- canonical cells：坐标、filled/empty、2D 的 `source_id/atlas_coords/alternative_tile` 或 3D 的 item/orientation；
- environment：tile/cell size、node transform、collision/support facts、对象 occupancy 及其 freshness；
- traversal profile：显式的 movement model、actor footprint、能力参数、`cell_occupancy`、`requires_support`、`support_occupancy` 及事实来源；
- route facts：entry anchor、地图边界和按同一 traversal profile 计算的 reachable frontier；
- resource bindings：语义资源键到经过验证的 TileSet/GridMap 数据或 reference-cell 规则。

每个字段组携带 evidence reference 和 completeness。缺少执行关键事实时快照仍可供 planner 形成草案，但标记 `execution_eligible=false`。

选择 artifact 而不是继续依赖消息历史，是因为 artifact 可绑定 revision、做摘要校验并在 compaction 后重新注入。替代方案是要求 planner 每轮自行重读；该方案会重复消耗工具调用，且无法保证三次尝试使用同一事实基线。

### 2. 给 planner 和 compiler 不同的快照投影

planner 输入投影包含边界、占用/碰撞几何、traversal profile、entry/frontier、语义资源键和 reference-cell 坐标，但不包含用于生成每个写操作的裸 `source_id/atlas_coords` 数组。planner 输出 `platforms`、`segments`、装饰意图、语义资源引用和设计说明。

validator/compiler 使用完整快照，把语义资源引用或 reference-cell 规则解析为精确 operations，并生成 batch fingerprint。writer 仅消费该编译结果。

选择语义引用而不是让 planner 手写 atlas，是为了把“设计错误”和“资源绑定错误”分成可测试的失败类型。代价是必须为资源注册表、reference cell 和编译器定义严格契约。

### 3. occupancy 分为事实与解释两层

快照中的 cell/object occupancy 来自 canonical map 和空间索引核验；traversal occupancy 是由真实玩法和角色控制器确定的解释规则。snapshot builder 必须显式记录两者及来源，不能把 `non_empty_count` 当作完整 occupancy，也不能用默认 `empty/filled` 授权执行。

planner 消费 traversal profile，不得自行覆盖。若用户明确改变玩法语义，则使旧快照失效并生成新快照，而不是在当前尝试中修改 occupancy 参数。

### 4. 三次尝试由 reducer/scheduler 计数

尝试键为稳定任务 lineage、目标、图层、snapshot id 和规划 operation。一次尝试定义为“planner 产生候选，随后 validator 返回确定性结果”。最大次数固定为 3：

1. 第一次生成完整候选；
2. 第二次必须绑定第一次的结构化 issues/repair plan；
3. 第三次必须绑定前一次仍未解决的问题并形成最后候选。

候选 fingerprint 未变化或没有消费要求的 repair 字段时，不再浪费一次 LLM 调用，直接返回 typed `unchanged_plan_attempt`，并作为该次失败记录。地图 revision 或权威输入真实变化时生成新 snapshot；精确尝试预算可以重建，但既有 task-level convergence 计数继续保留，避免跨 revision 无限循环。

尝试次数由 reducer 持有而不是 prompt 计数，避免对话压缩后重新从第一次开始。

### 5. 规划交付与执行许可是正交状态

最终结果至少包含：

- `planning_status=delivered`；
- `execution_status=approved | blocked_by_validation | blocked_by_missing_facts`；
- 最后一次候选规划；
- snapshot identity；
- 三次以内的 validation summaries、完整 repair artifact 引用和 unresolved issues；
- approved batch refs（仅成功时存在）。

第三次失败后 scheduler 运行 plan-publication step，但不运行 writer step。整体地图编辑任务不得声称写入完成；用户看到的是“规划已交付、执行被阻止”。这比把 planner step 标成普通 terminal failure 更准确，也不会放松写入安全。

### 6. frontier 是快照事实，可重算但仍是写入门禁

planner 仍接收边界、entry 和 reachable frontier 作为路线设计事实。它们不要求常驻自然语言上下文，而是每轮从 snapshot artifact 注入。frontier 缺失、截断或 stale 时，scheduler 用同 snapshot revision 的 canonical cells 和 traversal profile 触发确定性重算并派生新 snapshot。

重算失败不阻止发布一个明确标记的规划草案，但任何依赖 frontier 连通性的验证、编译和写入均保持阻塞。替代方案是把 frontier 当作便宜的非关键提示；这可能生成与旧地图不连通却仍被写入的新区域，因此拒绝。

### 7. skill 与 worker binding 显式声明数据职责

`map-area-expansion` 和相关 planner skill 更新为：

- 要求绑定兼容的 authoritative snapshot artifact；
- 使用注入的 traversal profile、entry 和 frontier，不从摘要猜测；
- 只输出设计 schema 和语义资源引用；
- 通过 typed refresh 请求补充或重算事实，不直接绕过 snapshot 形成另一套事实；
- 不生成或修改 approved batch。

reader 负责 exact facts，validator/compiler 负责 atlas resolution 和批准，writer 负责事务执行。运行时 binding 检查 worker 的 artifact kind、snapshot schema、target、layer 和 revision。

## Risks / Trade-offs

- [快照过大] → 使用完整 artifact 加角色专用投影；对 coverage 分块并记录 completeness，不把逐格数组复制进每轮消息。
- [resource registry 过期导致错误 atlas] → compiler 同时校验 registry 条目和 snapshot/reference cell；不匹配时返回 typed refresh，不生成 approval。
- [地图在三次尝试中被外部修改] → revision 变化立即废弃候选与 approval，刷新 snapshot；task-level convergence 继续计数。
- [规划已交付被误解为编辑完成] → UI/事件同时展示 planning 和 execution 两个状态，并明确 map revision 未变化、没有 committed writes。
- [第三次候选质量比第二次差] → 最终交付仍采用最后一次候选以保持确定性，同时保留所有尝试供用户比较；未来可另加确定性评分选择，但不属于本 change。
- [skill 文档与 runtime schema 漂移] → 增加 contract tests，验证 skill 所述输入/输出字段和实际 worker binding 一致。

## Migration Plan

1. 新增 snapshot schema/builder、artifact 持久化和只读解析，先以 shadow mode 与现有读取结果比对。
2. 新增 planner projection 和设计输出 schema，更新 planner skill/agent；在迁移期拒绝同一计划混用 legacy raw batches 与新 semantic plan。
3. 让平台 validator/compiler 接受 snapshot id，生成 revision/digest 绑定的 approved batches。
4. 在 reducer/scheduler 中加入三次尝试、repair binding、planning publication 和独立 execution status。
5. 将 writer 切换为只接受新 approval contract；保留对旧持久化任务的 typed migration block，不自动猜测转换。
6. 增加 compaction、restart、revision drift、三次失败和成功写入的端到端测试后移除 legacy planner-produced raw batch 路径。

回滚时可以关闭新 planner pipeline 并恢复旧任务入口，但已经生成的新 approval 不得降级转换为旧批次；应废弃并重新规划。

## Open Questions

- snapshot coverage 的默认分块大小与 artifact 总大小上限需要通过真实大地图基准确定。
- UI 最终展示完整三次候选还是只展示最后候选加历史摘要，可在不改变状态契约的前提下决定。
