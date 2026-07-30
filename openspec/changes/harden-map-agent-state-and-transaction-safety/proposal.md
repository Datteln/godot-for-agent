## Why

当前 map-agent 整改已建立 DAG、Reducer、Completion Gate 和分组事务等正确骨架，但复核确认仍有跨任务状态泄漏、完成门绕过、批准批次提前消费、未捕获调度异常，以及 journal、Undo/Redo revision 和 artifact 发布的数据安全缺口。现有测试基线仍为 177 通过、9 失败，且关键事务与端到端回归尚未落地，因此这些不变量需要在继续扩展地图能力前被硬化。

## What Changes

- 为每个新地图任务建立明确的 task epoch，以可机读的字段生命周期元数据驱动单一 Reducer 原子重置；持久化数据只能经 raw migration、完整校验和一次性构造边界进入 live state，专用 resume 授权只能由下一请求原子消费一次。
- 规范化验证结果的 nullable 字段，要求验证与完成证据精确匹配 target/revision，以作用域 upsert 维护 blocker，并为 running/completed/paused/cancelled/idle 定义完整的 Completion Gate 状态语义。
- 将平台 approved batch 改为“校验时保留、提交成功后消费”，并在 recovery 后、写事务开始前以 Godot 权威 revision 执行 CAS；拒绝、冲突或失败时不丢批次、不提前推进 revision。
- 将地图事务 journal 扩展为持久化状态机，明确 `cleaned` 仅表示终态 journal 已删除而非可序列化状态；对不确定的 `committing` 状态 fail closed，禁止把可能已提交的编辑自动回滚。
- 使 Undo/Redo 将地图内容与 revision 文件作为同一权威历史恢复，避免外部变更扫描器二次 bump。
- 将 map artifact 与 Session locator 纳入可恢复的协调提交，保留并加固现有 turn-id/canonical-fingerprint 幂等语义，禁止提交指向不存在 artifact 的 locator。
- 保证 Session turn 计数器跨回滚与重启单调不减（持久化取 max），并对 staged 与已 committed turn 同 id 异指纹冲突返回 typed 可恢复错误而非卡死会话。
- 把 DAG 输入绑定和 worker stage 转换异常转换为 typed blocked 结果；为重复 `create_plan` 增加 revision-scoped 精确重试熔断与跨 revision 的 task/lineage 收敛预算，并保留旧计划终态。
- 编辑器启动时尽早启动单飞、有界的 journal 恢复；首次地图写入等待同一恢复操作完成，并收紧截图路径和普通路径参数的类型/URI 边界，显式拒绝 `user:/`、`res:/` 等伪 scheme 回退。
- 删除 delegate group 与 DAG scheduler 并存但不可达的 legacy `remaining` 队列，使 scheduler graph 成为后续步骤的唯一来源。
- 在故障测试前建立生产默认关闭、测试可控的文件系统/journal/进程退出故障注入设施，再补齐 Gate、事务、Undo/Redo、artifact、DAG、重试和端到端覆盖。

## Capabilities

### New Capabilities

<!-- None. This change hardens existing map-agent contracts. -->

### Modified Capabilities

- `map-workflow-state-and-evidence`: 增加可机读字段生命周期、封闭 hydration 边界、一次性 resume 授权、任务 epoch 完整重置、Completion Gate 状态矩阵、精确 revision 验证、作用域 blocker 更新、nullable 防御、伪 scheme 拒绝和 Reducer 旁路检测要求。
- `map-edit-transactions`: 增加 durable commit journal、逻辑 cleanup 语义、歧义恢复、有界单飞 recovery、recovery 后权威 revision 重检、首次写入恢复门、可控故障注入以及 Undo/Redo revision 同步要求。
- `dependency-aware-map-plans`: 增加输入绑定/stage 转换的 typed failure、scheduler graph 唯一 pending-step 来源、精确重试熔断和跨 revision 任务收敛预算要求。
- `platform-traversal-validation`: 规定批准批次只能在对应写入成功提交后消费和推进 revision，并在 mutation 前以 Godot 权威 revision 拒绝陈旧批准。
- `atomic-tool-result-submission`: 将 map artifact 和 Session locator 绑定为可恢复的协调提交，并验证/加固已有 turn-id/fingerprint 幂等路径而不重写其身份语义。

## Impact

- 服务端：map workflow/reducer、Completion Gate、plan scheduler、agent worker 创建、平台批准生命周期、QueryEngine Session/artifact 提交与路径安全，以及 turn 计数器单调持久化与 turn-identity 冲突的 typed 恢复路径。
- Godot 前端：UnifiedUndoManager journal/recovery、MapRevisionTracker、ToolExecutor 的 Undo/Redo 与批准写组协作。
- 持久化：扩展 transaction journal 与 artifact publication journal/marker；Session 与现有 `map_artifacts.json` 格式保持兼容。
- 测试：先增加跨 Python/Godot 的确定性故障注入 seam、journal fixture 与独立进程重启驱动，再修正 9 个陈旧断言并新增单元、集成、Godot headless 和恢复回归。
- API：不新增破坏性 HTTP 变更；新增错误使用 typed code/payload 返回而非 500。
