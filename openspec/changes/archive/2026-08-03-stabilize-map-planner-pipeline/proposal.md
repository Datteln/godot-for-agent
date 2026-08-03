## Why

地图规划目前把事实读取、路线设计、写入批次生成和校验修复混在 LLM 上下文中，导致上下文压缩或一次错误推断可以丢失关键数据，并让流程在多次校验失败后既不给最终规划，也永远到不了地图写入。需要把权威事实、主观规划和确定性执行边界拆开，同时保证失败可诊断、规划必交付、写入仍然 fail-closed。

## What Changes

- 在规划前生成绑定 `target_path`、`map_layer`、`map_revision` 和摘要指纹的权威地图快照，覆盖精确 cell/atlas、地图占用、对象占用、碰撞、移动能力、入口与可达 frontier。
- planner 只消费版本化快照，输出路线几何、语义资源引用和设计说明；逐格 `source_id`/`atlas_coords` 由确定性 compiler 根据快照和资源注册表生成，不依赖 LLM 上下文传递。
- 将平台方案的确定性校验尝试上限设为三次。每次失败必须把结构化问题和 repair plan 绑定到下一次尝试，并禁止无变化重试。
- 三次确定性校验均失败后仍发布一个最终规划结果，同时标记 `execution_status=blocked_by_validation`；失败只阻止 approved batch 和地图写入，不阻止规划交付。
- frontier 等可重算路线事实不要求常驻对话上下文；缺失或过期时按同一权威 revision 重算。重算失败允许交付未验证规划，但禁止执行。
- writer 不再接收 planner 手写的裸 atlas 操作，只执行 validator/compiler 生成且与快照 revision、指纹和 approval 绑定的批次。
- 更新地图扩建 skill 和 worker 输入契约，显式区分“快照提供的事实”“planner 负责的设计”和“compiler 负责的写入数据”。

## Capabilities

### New Capabilities

- `authoritative-map-planning`: 定义权威规划快照、三次确定性规划尝试、最终规划交付以及规划结果与执行许可的分离。

### Modified Capabilities

- `platform-traversal-validation`: 校验和编译必须消费与 planner 相同 revision 的权威快照，并在成功时生成不可变批准批次。
- `dependency-aware-map-plans`: 规划尝试耗尽后发布 blocked planning result，而 writer 仅由有效的 compiled approval 解锁。
- `map-workflow-state-and-evidence`: 持久化快照引用、规划尝试、最终规划和独立的 execution status，使其不依赖对话压缩。
- `skill-worker-binding`: 地图规划 skill 必须获得权威快照输入，并将精确 atlas 绑定和可重算 frontier 的职责限定到正确 worker/tool。

## Impact

- 影响 Python 服务端的 map workflow reducer、planner 调度、worker 输入/结果 schema、artifact binding、重试与 completion gate。
- 影响 Godot 侧地图读取、frontier 计算、平台 validator/compiler、资源解析和 approved batch 写入门禁。
- 需要更新 `map-area-expansion` 等规划 skill、相关 agent 定义，以及 planner/validator/writer 的单元、集成与恢复测试。
- 不改变普通 LLM API 的 fallback 重试预算；本 change 的三次上限仅适用于同一地图规划目标的确定性校验尝试。
