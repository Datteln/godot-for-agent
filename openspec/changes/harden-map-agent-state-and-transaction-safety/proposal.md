## Why

当前 map-agent 整改已建立 DAG、Reducer、Completion Gate 和分组事务等正确骨架，但复核确认仍有跨任务状态泄漏、完成门绕过、批准批次提前消费、未捕获调度异常，以及 journal、Undo/Redo revision 和 artifact 发布的数据安全缺口。现有测试基线仍为 177 通过、9 失败，且关键事务与端到端回归尚未落地，因此这些不变量需要在继续扩展地图能力前被硬化。

## What Changes

- 为每个新地图任务建立明确的 task epoch，通过单一 Reducer 原子重置所有任务级状态，并把直接字段/容器写入扫描接入测试。
- 规范化验证结果的 nullable 字段，要求验证与完成证据精确匹配 target/revision，并以作用域 upsert 维护 blocker。
- 将平台 approved batch 改为“校验时保留、提交成功后消费”，拒绝或失败时不丢批次、不提前推进 revision。
- 将地图事务 journal 扩展为持久化状态机；对不确定的 `committing` 状态 fail closed，禁止把可能已提交的编辑自动回滚。
- 使 Undo/Redo 将地图内容与 revision 文件作为同一权威历史恢复，避免外部变更扫描器二次 bump。
- 将 map artifact 与 Session locator 纳入可恢复的协调提交，禁止提交指向不存在 artifact 的 locator。
- 把 DAG 输入绑定和 worker stage 转换异常转换为 typed blocked 结果；为重复 `create_plan` 增加语义熔断并保留旧计划终态。
- 首次地图写入前同步完成 journal 恢复检查；收紧截图路径和普通路径参数的类型/URI 边界。
- 更新陈旧测试并补齐 Gate、事务、Undo/Redo、artifact、DAG、重试和端到端故障注入覆盖。

## Capabilities

### New Capabilities

<!-- None. This change hardens existing map-agent contracts. -->

### Modified Capabilities

- `map-workflow-state-and-evidence`: 增加任务 epoch 完整重置、精确 revision 验证、作用域 blocker 更新、nullable 防御和 Reducer 旁路检测要求。
- `map-edit-transactions`: 增加 durable commit journal、歧义恢复、首次写入恢复门以及 Undo/Redo revision 同步要求。
- `dependency-aware-map-plans`: 增加输入绑定/stage 转换的 typed failure 和重复计划熔断要求。
- `platform-traversal-validation`: 规定批准批次只能在对应写入成功提交后消费和推进 revision。
- `atomic-tool-result-submission`: 将 map artifact 和 Session locator 绑定为可恢复的协调提交，消除悬空 locator。

## Impact

- 服务端：map workflow/reducer、Completion Gate、plan scheduler、agent worker 创建、平台批准生命周期、QueryEngine Session/artifact 提交与路径安全。
- Godot 前端：UnifiedUndoManager journal/recovery、MapRevisionTracker、ToolExecutor 的 Undo/Redo 与批准写组协作。
- 持久化：扩展 transaction journal 与 artifact publication journal/marker；Session 与现有 `map_artifacts.json` 格式保持兼容。
- 测试：修正 9 个陈旧断言，新增 Python 单元/集成测试和 Godot headless 事务、revision、恢复回归。
- API：不新增破坏性 HTTP 变更；新增错误使用 typed code/payload 返回而非 500。
