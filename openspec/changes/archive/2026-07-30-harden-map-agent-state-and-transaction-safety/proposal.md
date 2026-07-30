## Why

当前 map-agent 整改已建立 DAG、Reducer、Completion Gate 和分组事务等正确骨架，但复核确认仍有跨任务状态泄漏、完成门绕过、批准批次提前消费、未捕获调度异常，以及 journal、Undo/Redo revision 和 artifact 发布的数据安全缺口。用户重置会话目前也只删除部分 Session 持久化：同一 `session_id` 下的旧 turn/artifact、事件、文件读取授权和前端缓存仍可能存活，导致重置后的请求复用旧 turn identity、读取旧会话数据或与旧提交冲突。错误生命周期同样过粗：工具、模型、调度、传输或提交 attempt 的失败最终都可能压成同一个 `ChatErrorResponse`，前端统一清空 pending 状态并回到 `IDLE`，使本应恢复的 attempt 错误表现为整个 task 终止。现有测试基线仍为 177 通过、9 失败，且关键事务、完整会话重置、持久化任务恢复与端到端回归尚未落地，因此这些不变量需要在继续扩展地图能力前被硬化。

## What Changes

- 为每个新地图任务建立明确的 task epoch，以可机读的字段生命周期元数据驱动单一 Reducer 原子重置；持久化数据只能经 raw migration、完整校验和一次性构造边界进入 live state，专用 resume 授权只能由下一请求原子消费一次。
- 规范化验证结果的 nullable 字段，要求验证与完成证据精确匹配 target/revision，以作用域 upsert 维护 blocker，并为 running/completed/paused/cancelled/idle 定义完整的 Completion Gate 状态语义。
- 将平台 approved batch 改为“校验时保留、提交成功后消费”，并在 recovery 后、写事务开始前以 Godot 权威 revision 执行 CAS；拒绝、冲突或失败时不丢批次、不提前推进 revision。
- 将地图事务 journal 扩展为持久化状态机，明确 `cleaned` 仅表示终态 journal 已删除而非可序列化状态；对不确定的 `committing` 状态 fail closed，禁止把可能已提交的编辑自动回滚。
- 使 Undo/Redo 将地图内容与 revision 文件作为同一权威历史恢复，避免外部变更扫描器二次 bump。
- 将 map artifact 与 Session locator 纳入可恢复的协调提交，保留并加固现有 turn-id/canonical-fingerprint 幂等语义，禁止提交指向不存在 artifact 的 locator。
- 保证 Session turn 计数器跨回滚与重启单调不减（持久化取 max），并对 staged 与已 committed turn 同 id 异指纹冲突返回 typed 可恢复错误而非卡死会话。
- 将用户重置定义为持久化的 session-epoch 切换：旧 epoch 的 Session、turn/幂等缓存、map/delegate artifact、事件/history、文件读取授权、recovery pointer 和前端会话缓存必须不可达并最终清理；Godot 项目内容、权威 revision、崩溃恢复 journal、资源索引与全局配置必须保留。
- 让 reset 在服务端建立完整逻辑隔离边界后才返回成功，并由响应提供新 epoch 与事件高水位；前端在确认前保持 `resetting`、阻止发送并取消旧事件轮询，失败时不得伪装成已重置。
- 将 durable task 与一次 `/chat`/LLM/tool/submission attempt 分离；任何非终态错误只能结束当前 attempt，必须保留 task checkpoint、已知副作用状态和下一恢复动作，不能隐式把 task 置为 `idle`、`cancelled` 或永久失败。
- 为错误响应增加稳定的 recovery disposition，并由后端 Recovery Supervisor 负责安全重试、fresh-turn 恢复、权威信息刷新、replan、等待前端或 typed pause；前端按 disposition 保留 pending/Undo/approval 状态，不再把所有 `type=error` 统一清理成任务结束。
- 把 DAG 输入绑定和 worker stage 转换异常转换为 typed blocked 结果；为重复 `create_plan` 增加 revision-scoped 精确重试熔断与跨 revision 的 task/lineage 收敛预算，并保留旧计划终态。
- 编辑器启动时尽早启动单飞、有界的 journal 恢复；首次地图写入等待同一恢复操作完成，并收紧截图路径和普通路径参数的类型/URI 边界，显式拒绝 `user:/`、`res:/` 等伪 scheme 回退。
- 删除 delegate group 与 DAG scheduler 并存但不可达的 legacy `remaining` 队列，使 scheduler graph 成为后续步骤的唯一来源。
- 在故障测试前建立生产默认关闭、测试可控的文件系统/journal/进程退出故障注入设施，再补齐 Gate、事务、Undo/Redo、artifact、DAG、重试和端到端覆盖。

## Capabilities

### New Capabilities

<!-- None. This change hardens existing map-agent contracts. -->

### Modified Capabilities

- `map-workflow-state-and-evidence`: 增加可机读字段生命周期、封闭 hydration 边界、一次性 resume 授权、任务 epoch 完整重置、task/attempt 生命周期分离、Completion Gate 状态矩阵、精确 revision 验证、作用域 blocker 更新、nullable 防御、伪 scheme 拒绝和 Reducer 旁路检测要求。
- `map-edit-transactions`: 增加 durable commit journal、逻辑 cleanup 语义、歧义恢复、有界单飞 recovery、recovery 后权威 revision 重检、首次写入恢复门、可控故障注入以及 Undo/Redo revision 同步要求。
- `dependency-aware-map-plans`: 增加输入绑定/stage 转换的 typed failure、可恢复 attempt 与终态 step failure 的区分、scheduler graph 唯一 pending-step 来源、精确重试熔断和跨 revision 任务收敛预算要求。
- `platform-traversal-validation`: 规定批准批次只能在对应写入成功提交后消费和推进 revision，并在 mutation 前以 Godot 权威 revision 拒绝陈旧批准。
- `atomic-tool-result-submission`: 将 map artifact 和 Session locator 绑定为可恢复的协调提交，验证/加固已有 turn-id/fingerprint 幂等路径而不重写其身份语义，并增加完整 session-epoch 重置、旧状态隔离、结构化 recovery disposition、durable attempt 恢复及 reset/event 前后端确认契约。

## Impact

- 服务端：map workflow/reducer、Completion Gate、durable TaskRun/Attempt、Recovery Supervisor、plan scheduler、agent worker 创建、平台批准生命周期、QueryEngine Session/artifact 提交、EventStore、FileStateCache、history/recovery 缓存与路径安全，以及 turn 计数器单调持久化、session epoch 和 turn-identity 冲突的 typed 恢复路径。
- Godot 前端：UnifiedUndoManager journal/recovery、MapRevisionTracker、ToolExecutor 的 Undo/Redo 与批准写组协作，以及 disposition-aware task 状态、pending/Undo 保留、reset 确认、事件轮询切换和每会话安全缓存清理。
- 持久化：扩展 transaction journal 与 artifact publication journal/marker，并增加持久化 reset epoch/manifest；Session 与现有 `map_artifacts.json` 格式通过兼容读取保持可迁移。
- 测试：先增加跨 Python/Godot 的确定性故障注入 seam、journal fixture 与独立进程重启驱动，再修正 9 个陈旧断言并新增单元、集成、Godot headless、reset 隔离和恢复回归。
- API：不新增破坏性 HTTP 变更；reset 响应向后兼容地增加 `session_epoch` 与 `last_event_seq`，问题响应增加 task/attempt/checkpoint identity、recovery disposition、side-effect state、retry token 和 next action，新增错误使用 typed payload 返回而非 500。
