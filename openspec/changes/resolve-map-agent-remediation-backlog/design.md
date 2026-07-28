## Context

当前 map-agent 流程已经有 Capability Contract、Map Stage Contract、revision guard 和动态 worker，但关键规则仍同时存在于 Python 编排器、QueryEngine、GDScript 工具、Agent prompt 和 Skill 文本中。其结果是：单点校验看似存在，跨边界组合后仍可能半提交、越过依赖、伪造结果来源、无证据完成或在相同错误上耗尽轮次。

本 change 跨 Python 会话事务、Agent/Skill 调度和 Godot 编辑器 Undo 系统，且包含持久化 schema 与行为上的 breaking change，因此需要先确定状态所有权、事务边界和迁移顺序。

## Goals / Non-Goals

**Goals:**

- 让工具结果、计划依赖、Skill 绑定、地图状态、worker 结果和完成证据都具有唯一运行时合同。
- 让大型地图工具结果在每个 Session 内只使用一个可寻址、可原子提交的持久化文档，并支持同轮 staged 读取。
- 使所有状态变化可按 target/revision 审计、恢复和测试。
- 让地图写入组在验证失败时整体回滚，并保持 Undo/Redo/restart 一致。
- 让 Completion Gate 只约束用户明确授权的当前地图编辑请求，不影响普通聊天和非编辑型地图请求。
- 删除影子规划、重复白名单、重复阶段文本和 prompt 内完成判定。
- 将失败恢复变成分类、可熔断、可回补的确定性流程。

**Non-Goals:**

- 不在本 change 内重写整个 `agent.py`、`query/helpers.py` 或 Godot 工具集合。
- 不改变地图生成算法的美术质量目标。
- 不引入分布式事务或外部工作流引擎；事务范围限于当前本地 Session 和 Godot 编辑器。
- 不保证任意第三方 Skill 自动兼容新绑定合同；不兼容项必须明确返回 `incompatible`。

## Decisions

### 1. 工具结果采用“预检 + Session 工作副本 + 原子替换”

`submit_tool_results` 先对整批 tool id、turn id、frame ownership、status、pending metadata 和授权进行纯校验。通过后在深拷贝 Session 上应用所有 reducer；持久化成功后才替换活动 Session 并发事件。

选择该方案而不是逐项补偿，是因为当前副作用主要是 Session 状态与持久化，工作副本更容易证明“后项非法时零提交”。外部 artifact/event 在 commit 后产生，并使用 request/turn 幂等键。

大型地图工具结果不再按调用生成 `describe_map_region-*.json` 等独立文件。每个 Session 只保留一个持久化文档：

```text
.ai_agent_service/artifacts/<session_id>/map_artifacts.json
```

文档 schema 使用 `turns[turn_id].entries[tool_use_id]` 保存每个工具的 `tool`、`input`、`result`、canonical fingerprint、创建时间和 artifact kind。对外定位由 `artifact_ref + artifact_turn_id + artifact_entry_id` 组成；`artifact_ref` 始终指向同一个 Session 文件，后两项定位 JSON 块。省略 turn 或 entry 的读取不得默认选择“最新结果”，避免历史重放读取到错误 revision。

`_SubmissionPublicationBuffer` 按 turn 聚合一个 staged block，而不是积累多个待发布文件。LLM 在同一提交内需要读取时，专用地图 artifact reader 优先从当前事务的 staged block 读取；Session 持久化成功后，运行时读取现有文档、合并该 turn 并原子替换单文件。interrupt、取消、reducer 失败或 Session 保存失败直接丢弃 staged block，不得产生可恢复的正式引用或残留 turn。相同 `turn_id + canonical fingerprint` 的重试为幂等 no-op；相同 turn 的不同 fingerprint 作为冲突拒绝。

选择 Session 单文件而不是每 turn 单文件，是为了避免一次并行地图读取产生多个 `describe_map_region-*` 文件，并让清理、审计和恢复围绕 Session 事务统一进行。底层原子替换可以使用不对 Agent 暴露的瞬时临时文件，但持久化 artifact 只有 `map_artifacts.json`。

### 2. 计划成为不可变 DAG，由专用 Plan Scheduler 消费

`create_plan` 产出带稳定 step id、`depends_on`、输入 schema 和 result schema 的不可变步骤。`delegate_many` 不再重新解释或丢弃依赖，而是接收 scheduler 已解锁的步骤。只有所有依赖处于 `succeeded` 才能启动后继；失败、取消或 blocked 会向下游传播 typed blocked result。

服务层不再从 `edit_map` 反向构造平台参数。writer 仅执行 planner/validator 产出的 approved batch artifact；无批准合同直接返回 planner。

### 3. Skill Binding 是唯一解析层

新增 `SkillBindingResolver`，输入 Skill 标识、Agent role、map stage、worker mode 和 Capability Contract，输出：

- `resolved`: 已启用且需求能力可由当前有效工具满足；
- `missing`: Skill 不存在或未启用；
- `incompatible`: Skill 存在，但角色、阶段、mode 或能力不匹配。

删除动态 worker payload 的 `allowed_tools`。 bundled Skill 从重复的工具名白名单迁移到语义 `required_capabilities`；实际工具集始终由 Agent Interface、stage/mode Capability Contract 和权限求交集得到。`load_skill` 返回当前 binding，而不是全局 registry 视角。

### 4. 地图工作流由事件和单一 reducer 拥有

Agent 与 QueryEngine 只提交 `MapWorkflowEvent`，不得直接写 stage、blocker、checkpoint、batch 或 no-progress 字段。`MapWorkflowReducer` 以 `(target, revision)` 为 scope key 生成新 `MapTaskState`，并拒绝非法转换。

这比继续增加 setter 更安全，因为事件同时提供审计、重放和迁移入口。已有 `transition_stage()` 成为 reducer 内部 Implementation。

### 5. Worker 结果必须通过 Frame Contract Validator

Frame 创建时冻结 contract id、stage、target、revision、allowed next stages、result schema 和 worker instance id。完成时运行时逐字段比对；stage spoof、错误 target/revision、非法 `next_stage` 和 schema 不匹配均返回 typed contract violation。

动态 Worker 使用保留前缀加随机 instance id，不允许覆盖永久 Agent 名。自动子 Frame 的 task 只包含 objective 与输入引用；角色规则、schema 和错误恢复只来自结构化 runtime contract。

### 6. Completion Gate 独占完成决策

reviewer/validator 只返回 observation、issues 和 `evidence_refs`。Evidence Registry 根据 tool_use_id 验证截图工具属于本 Frame、执行成功、目标/revision 一致且 artifact 可读取。Completion Gate 综合验证合同、review issue 和证据得出唯一 `completion_allowed`。

prompt 中的完成条件仅作为行为说明，不再作为运行时真相；legacy `completion_allowed` 输入字段被忽略并最终删除。

### 7. Undo 事务按“approved write group”划分

单个无后续验证要求的写工具仍可形成单工具事务。地图计划写入使用稳定 `map_transaction_id`：首个 approved batch 开启事务，后续写入追加到同一 UnifiedUndoManager action，最终 validator 成功后 commit，失败/取消/contract violation 时 abort 全组。

revision 文件、场景修改和相关索引写入属于同一事务。提交后 Ctrl+Z/Redo 必须同步恢复内容与 revision；重启通过事务 journal 检测未完成组并回滚到 before snapshot。

### 8. 平台验证只信任编辑器侧事实

公共调用不再接收可伪造 `_collision_cells`。validator 从 canonical target/revision 直接读取场景 collision facts，或验证由 reader 生成且绑定 target/revision/digest 的 facts artifact。

leap 对抛物线路径采样 actor footprint，检查中途碰撞、头顶净空、落点宽度与落点净空。每个 segment 的 from/to id 必须存在，并与端点坐标、方向和平台几何一致。

### 9. 重试按语义签名和错误类别管理

重试 key 为 `(stage, target, revision, operation signature, error category)`，而不是统一计数。结构化输出修复返回原始 validation issues、repair actions 和 repair attempt；连续同类问题超过阈值后熔断。

`missing_inputs` 生成 reader step，reader typed result 成为原步骤新 attempt 的显式输入。暂停结果包含最早 root cause、分类计数、最后 attempt 和恢复建议。

### 10. 删除重复描述但保留安全边界

两个地图 Skill 删除阶段交接、revision 恢复和完成条件文本，只保留领域算法。Agent prompt 删除 validator/reviewer 重复完成判定。工具可达性、阶段顺序和 schema 分别由 Capability Contract、Plan Scheduler/Map reducer、Frame Contract 管理。

### 11. Completion Gate 使用请求级地图编辑意图与响应血缘

Completion Gate 分为两个独立判断：

1. **激活 Gate**：当前用户请求必须明确要求创建、修改、扩建、删除、放置、绘制或修复地图内容，运行时才建立 request-scoped `map_edit` intent。打开/选中地图、历史 `task_id`、普通聊天、地图读取/分析/检查、只规划不执行、修改地图相关脚本、保存场景以及 Undo/Redo 均不能单独激活 Gate。
2. **执行 Gate**：当前 final response 必须属于该 `map_edit` intent 的任务血缘，并已成为地图完成候选。仍在询问 target、layer、resource、revision 等 `missing_inputs`，或尚未进入完成候选阶段时，回复必须原样返回，不能被 `completion_target_missing` 等 Gate 文本替换。

用户发起新的普通请求时创建新的 request scope；旧地图任务可继续以 completed/paused/checkpoint 状态持久化，但处于休眠状态，不参与新回复。工具结果回填通过 pending turn/frame metadata 继承原 `map_edit` lineage。自然语言“继续”不自动继承授权；只有明确表达“继续刚才的地图编辑”，或使用专用 `resume_map_task` 命令，才能把新请求重新绑定到旧地图编辑任务。

运行时条件应等价于：

```text
gate_active =
    current_request.intent == map_edit
    AND current_response.map_task_id == current_request.map_task_id
    AND current_response.kind == map_completion_candidate
```

不得使用 `session.map_task_state.task_id != ""`、当前选中地图节点、历史 blocker 或曾经出现过地图工具调用作为 Gate 的充分条件。地图读取、规划和验证仍可返回结构化观察，但非编辑请求的最终文本不经过 Completion Gate。

### 12. 地图读取职责、artifact reader 与目标参数保持同一能力合同

地图总控需要完整上下文、节点树或精确地图事实时，必须通过 scheduler/委派进入具备 `context_read` 能力的 reader；总控自身不得反复调用 `search_tools` 试图突破 Agent Interface 或 Capability Contract。`search_tools` 可以报告 `unavailable_in_agent_scope`，但不能激活被当前角色或阶段裁掉的工具。一个 LLM 批次混合 server tool 与 front tool 时，server tool 可在服务端先完成、front calls 再返回 Godot 前端；这属于正常分侧调度，不视为工具丢失。

地图原始结果使用专用地图 artifact reader，按 `artifact_turn_id + artifact_entry_id` 读取 Session 单文件中的 committed 或当前事务 staged entry。`read_delegate_artifact` 继续只接受 delegate schema，不得读取地图工具原始结果。工具摘要中的读取提示必须由 `artifact_kind` 与当前 effective tools 动态生成，不得硬编码当前 Agent 不可达的 `read_file`。

`target_path` 省略时表示使用当前选中的兼容地图节点或场景中唯一兼容节点；字符串 `"."` 仍表示真实场景根节点，不能被重解释为自动选择标记。若 `"."` 或其他路径解析到非地图节点，工具返回结构化 `unsupported_map_type`，并在可安全确定时提供兼容地图候选或“省略 target_path”的修复提示。

## Risks / Trade-offs

- [长事务持有编辑器对象过久] → 限制 approved write group 的最大工具数、时长和 snapshot 体积；超过上限先安全 commit 并要求新事务。
- [Session 深拷贝成本增加] → 仅在 pending tool result 批次提交时复制，并对大型 artifact 保存引用而非内联内容。
- [Session 单文件随历史 turn 增长] → 按仍被 Session history、计划输入、证据或 checkpoint 引用的 turn 保留；压缩时只清理无引用块，并设置可观测的大小上限。
- [单文件损坏扩大影响面] → 使用 schema/version、canonical fingerprint 和原子替换；校验失败时停止消费并保留诊断，不以部分 JSON 猜测结果。
- [staged 引用被误当作 committed] → reader 显式解析当前事务 staged block，正式历史只记录提交成功的 turn；取消和回滚必须使 provisional 定位失效。
- [同一 Session 并发合并丢失 turn] → 复用 Session 级提交锁，并以 turn fingerprint 做 compare-and-swap/幂等检查。
- [旧 Skill 全部变为 incompatible] → 提供迁移诊断和 bundled Skill 自动迁移；第三方 Skill 明确列出缺失 capability。
- [事件 reducer 与旧字段双写造成分叉] → 迁移期只允许 reducer 写新状态，旧字段只读；校验一致后删除旧路径。
- [Undo journal 损坏] → journal 使用原子写与 checksum；无法验证时停止自动恢复并向用户报告，不猜测提交状态。
- [严格来源校验暴露旧 prompt 依赖] → 先以观测模式记录 violation，再切换强制拒绝。
- [自然语言意图误判导致普通回复再次被 Gate 吞掉] → 默认分类为非编辑；只有当前请求的显式地图变更语义或专用恢复命令才能创建/继承 `map_edit` lineage，且 Gate 只处理完成候选。

## Migration Plan

1. 增加新 schema、binding/result 类型、事件 reducer 和兼容读取，不改变现有执行路径。
2. 落地全批预检与 Session 工作副本，启用原子提交回归测试。
3. 启用 Plan Scheduler 和 Skill Binding；迁移 bundled Agent/Skill，删除动态 `allowed_tools`。
4. 将地图状态写入逐路由切换为事件 reducer，并启用 Frame Contract 强制校验。
5. 引入 Evidence Registry 与 Completion Gate，先记录 legacy completion 差异，再拒绝无证据完成。
6. 引入 map transaction journal，完成 edit/validate/Undo/Redo/restart 测试后切换地图写入。
7. 启用平台可信 facts 与分类重试，删除影子规划器及重复 prompt/Skill 文本。
8. 删除兼容字段和观测模式。回滚时可按阶段关闭新 scheduler/gate/transaction feature flag，但不得恢复半提交路径。
9. 增加 request-scoped `map_edit` intent 与 response lineage，停止在新用户消息上无条件启动地图任务，并移除基于历史 `task_id` 的全局 Gate。
10. 增加 `map_artifacts.json` schema、专用 reader 与旧逐文件 artifact 的兼容读取；先让新写入按 Session/turn 聚合，再迁移仍被引用的旧结果并删除逐调用文件生成和清理路径。
11. 切换 scope-aware artifact 提示和地图 reader 路由，加入 staged read、interrupt、幂等重试、错误 `target_path` 与混合 server/front 调度回归验证。

## Open Questions

- approved write group 的默认最大工具数、时长和 snapshot 上限需要通过现有大型地图场景基准确定。
- 第三方 Skill 的 `required_capabilities` 是否需要独立 schema version，实施时应与 Skill catalog 迁移一起决定。
