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
- 让“继续任务”等续作指代在当前唯一、仍聚焦且已授权的任务上下文内恢复原任务，而不是依赖地图关键词或任意历史状态。
- 让长时间 `/chat` 在保持 Session 原子提交的同时持续提供事务外存活信号，并由后端在真正的模型 attempt 失败时切换 fallback。
- 让暂停原因、检查点和面向用户的恢复提示保持同一语义，避免客户端超时被误报为连续无进展。
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

原子提交缓冲只约束会改变 Session、历史、artifact 或可重放输出的事件，不应隐藏请求存活状态。服务端在长工具结果提交和 LLM 等待期间通过独立、非持久化的 `turn_progress` 心跳发布 `request_id`、`turn_id`、Frame、phase 和单调序号；心跳不得携带 assistant 正文、工具结果、授权变化或任何可被恢复重放为业务状态的内容。前端只用它刷新空闲 watchdog，Session commit/rollback 后再发布正式事件。

### 2. 计划成为不可变 DAG，由专用 Plan Scheduler 消费

`create_plan` 产出带稳定 step id、`depends_on`、输入 schema 和 result schema 的不可变步骤。`delegate_many` 不再重新解释或丢弃依赖，而是接收 scheduler 已解锁的步骤。只有所有依赖处于 `succeeded` 才能启动后继；失败、取消或 blocked 会向下游传播 typed blocked result。

服务层不再从 `edit_map` 反向构造平台参数。writer 仅执行 planner/validator 产出的 approved batch artifact；无批准合同直接返回 planner。

### 3. Skill Binding 是唯一解析层

新增 `SkillBindingResolver`，输入 Skill 标识、Agent role、map stage、worker mode 和 Capability Contract，输出：

- `resolved`: 已启用且需求能力可由当前有效工具满足；
- `missing`: Skill 不存在或未启用；
- `incompatible`: Skill 存在，但角色、阶段、mode 或能力不匹配。

删除动态 worker payload 的 `allowed_tools`。 bundled Skill 从重复的工具名白名单迁移到语义 `required_capabilities`；实际工具集始终由 Agent Interface、stage/mode Capability Contract 和权限求交集得到。`load_skill` 返回当前 binding，而不是全局 registry 视角。

动态 Worker 的 Agent Interface 不是父 map-agent 工具列表的子集。父 map-agent 为了保持总控职责会故意不持有 `context_read` 工具；若非写入 Worker 再执行 `mode_tools & parent_tools`，read-only、propose-only 和 review-only Worker 会系统性失去各自 mode 合法的读取能力。所有动态 Worker 均以 `mode_tools & registered_tools` 建立初始接口，再剥离 `delegate`、`delegate_many`、`create_plan`，最后继续经过 Skill binding、stage 和权限裁剪。mode Capability Contract 才是 Worker 的授权上界，父 Agent 的窄接口不是安全边界。

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

用户发起新的普通请求时创建新的 request scope；旧地图任务可继续以 completed/paused/checkpoint 状态持久化，但处于休眠状态，不参与新回复。工具结果回填通过 pending turn/frame metadata 继承原 `map_edit` lineage。

续作解析拆为两个彼此独立的步骤：

1. `ContinuationIntentClassifier` 只判断“继续”“继续任务”“接着做”“恢复任务”等文本是否表达续作，不授予地图权限；
2. `TaskReferenceResolver` 只在当前会话存在唯一可恢复任务、该任务仍是最近对话焦点、checkpoint 有效且原始 request lineage 已明确获得 `map_edit` 授权时，把“任务”解析为原 `task_id`。

解析成功表示用户明确要求恢复已知任务，只继承原 target、权限上界、checkpoint 和授权 lineage，不创建更宽的新地图编辑权限。不存在候选、存在多个候选、任务已完成/放弃、对话焦点已经切换或 checkpoint 无效时，不得扫描任意历史地图状态猜测；运行时返回任务消歧或按普通请求处理。专用 `resume_map_task` 仍是无歧义的显式恢复入口。

运行时条件应等价于：

```text
gate_active =
    current_request.intent == map_edit
    AND current_response.map_task_id == current_request.map_task_id
    AND current_response.kind == map_completion_candidate
```

不得使用 `session.map_task_state.task_id != ""`、当前选中地图节点、历史 blocker 或曾经出现过地图工具调用作为 Gate 的充分条件。地图读取、规划和验证仍可返回结构化观察，但非编辑请求的最终文本不经过 Completion Gate。

### 15. 模型 fallback、请求存活与暂停语义分层

超时按所有权分成三层：

1. **LLM attempt timeout**：由后端 provider 拥有。主模型连接失败、请求超时或返回受支持的失败状态时，在丢弃该 attempt 的 provisional delta 后，使用相同 messages/tools 在 fallback 模型重试一次，并发布 `agent_model_fallback`。fallback 未配置、与主模型相同或也失败时才返回 provider-exhausted。
2. **`/chat` idle watchdog**：由前端拥有，但只判断多久没有后端存活信号。任何 `turn_progress` 或正式事件都刷新空闲计时；它不得选择模型、重放 `/chat` 或重新提交 tool results。
3. **`/chat` hard cap / liveness loss**：超过硬上限或服务端心跳消失后，前端才调用 interrupt，并显式传递 `cause=client_timeout`。用户点击停止传递 `cause=user_interrupted`。

配置必须满足 provider attempt 有机会先完成或切换 fallback，不能让较短的前端空闲超时抢先取消仍有心跳的请求。fallback 只能发生在单个 LLM attempt 边界；已经提交工具副作用的整个 `/chat` 不得被前端重放。

`MapTaskState`/checkpoint 使用类型化 `pause_kind`，至少区分 `no_progress_exhausted`、`client_timeout`、`user_interrupted`、`provider_exhausted` 和 `budget_exhausted`。pause formatter 按类型输出原因和恢复动作；只有 `no_progress_exhausted` 可以使用“连续无进展”措辞。所有暂停都必须生成最小结构化报告；缺少专用 report 时从 pause kind、stage、checkpoint 和 unresolved issues 合成，禁止向用户输出裸 `{}`。

暂停状态只在当前请求明确恢复或操作该任务时参与响应。新的普通请求以及无法解析到该任务的文本不会经过旧地图帧的 paused guard。

### 12. 地图读取职责、artifact reader 与目标参数保持同一能力合同

地图总控需要完整上下文、节点树或精确地图事实时，必须通过 scheduler/委派进入具备 `context_read` 能力的 reader；总控自身不得反复调用 `search_tools` 试图突破 Agent Interface 或 Capability Contract。`search_tools` 可以报告 `unavailable_in_agent_scope`，但不能激活被当前角色或阶段裁掉的工具。一个 LLM 批次混合 server tool 与 front tool 时，server tool 可在服务端先完成、front calls 再返回 Godot 前端；这属于正常分侧调度，不视为工具丢失。

地图原始结果使用专用地图 artifact reader，按 `artifact_turn_id + artifact_entry_id` 读取 Session 单文件中的 committed 或当前事务 staged entry。`read_delegate_artifact` 继续只接受 delegate schema，不得读取地图工具原始结果。工具摘要中的读取提示必须由 `artifact_kind` 与当前 effective tools 动态生成，不得硬编码当前 Agent 不可达的 `read_file`。

`target_path` 省略时表示使用当前选中的兼容地图节点或场景中唯一兼容节点；字符串 `"."` 仍表示真实场景根节点，不能被重解释为自动选择标记。若 `"."` 或其他路径解析到非地图节点，工具返回结构化 `unsupported_map_type`，并在可安全确定时提供兼容地图候选或“省略 target_path”的修复提示。

### 13. 截图路径、带问题视觉回看与 artifact 类型恢复

通用 `to_res_path` 继续只接受 `res://` 和项目相对路径，避免 `export_project`、资源写入等工具意外获得 `user://` 能力。截图输出和图片回看改用专用 capture path 合同，只接受：

- `res://...`；
- `user://...`；
- 可规范化为 `res://...` 的项目相对路径。

三类输入都拒绝绝对路径、未知 scheme 和任意 `..` 路径段。`res://` 继续接受项目读写 deny/allow 规则；`user://` 作为 Godot scratch 空间，只能由显式声明 scratch path 参数的截图/图片工具访问，不能在服务端 `path_ok` 中伪装成项目内 `user:/...`。服务端权限元数据必须区分 project path 与 scratch path，不能通过删除路径守卫来放行。

`read_image_metadata` 增加可选、限长的 `question`，用于视觉确认。QueryEngine 从原始可信 tool args 读取 question，并调用形如 `describe(image_path, "image", question=question)` 的接口；`"image"` 始终保留为媒体类型，问题不得占用 `type_hint` 参数。未提供 question 时保持现有通用视觉描述。返回值记录实际问题和回答，便于历史审计。

视觉侧道的职责仅包括构图、遮挡、外观、可见性和整体观感等模糊判断。tile 列号、cell 坐标、`source_id`、`atlas_coords` 和 revision 等精确事实必须来自 `describe_map_context` / `describe_map_region`，不得从截图像素或 VL 文本反推。

当 `read_map_artifact` 或 `read_delegate_artifact` 收到截图/图片引用时，工具返回结构化 `incompatible_artifact_kind`，包括 `actual_kind=image`、期望 artifact kind、`recommended_tool=read_image_metadata` 和上述精确事实边界。其他非法或缺失 artifact 引用返回对应的结构化 `invalid_artifact_ref` / `missing_artifact`，不得把 Windows 路径异常或裸异常字符串交给 Worker。

结构化输出修复必须始终消费已经规整过的集合。`validation.structured_issues` 为 `null`、非数组或缺失时先规范为 `[]`；后续 original issue category 聚合只能迭代该规范值，不能重新读取 nullable 原值。该防线独立于 Worker 工具可达性修复：正确工具会减少修复触发，但不能替代修复函数自身的 total/None-safe 性质。

### 14. 缺失的地图支持数据由上下文工具同调用直接重建

`resource_registry.json` 和 `spatial_index.json` 位于固定的 `.ai_agent_service/map_agent/` 内部目录，属于地图工具的本地支持数据。`describe_map_context` 读取真实场景后调用内部 `_ensure_map_support_data`：当其中一个文件不存在且场景存在兼容地图节点时，在同一次前端工具调用内直接从 canonical TileMapLayer/TileMap/GridMap、TileSet 和 MeshLibrary 事实构建缺失文件，随后复读并返回最终状态。

该路径不得创建 plan step、动态 Worker 或额外 LLM 回合，也不得切换 planner/writer。重建算法是确定性的，不使用 LLM 推断自然语言语义：

- 空间索引扫描真实已使用 cell，按维度、目标路径和稳定坐标签名生成条目；
- 资源 registry 优先使用可验证的资源/item 名称生成稳定 key，无名称时使用 source/item/atlas 签名生成技术 key；
- 已删除的人工别名（例如 `grass`）无法从场景事实精确恢复，重建结果必须标记 `semantic_aliases_recovered=false`，不得伪造原别名；
- 已存在且结构有效的文件保持不动，不能因一次上下文读取覆盖人工维护内容；本决策只规定“文件缺失”的直接重建，损坏文件继续返回结构化诊断，避免静默覆盖可能仍可恢复的数据。

内部重建使用新的 `writes_internal_cache` effect，而不是 `writes_project` 或地图内容写权限。该 effect 只能写两个编译期固定路径，不接受调用方输出路径，不触发地图 Completion Gate、approved write group 或 Undo。`describe_map_context` 对用户地图仍是语义只读工具；reader 获得的是封装在该工具内部的缓存自愈能力，而不是任意写工具。

两个文件均通过同目录临时文件、完整 JSON/合同校验和原子替换落盘。首次重建必须串行化并在锁内再次检查文件是否仍缺失；`describe_map_context` 在具备该 effect 后不得继续被声明为无条件并发安全。空间索引达到上限时允许写入明确的部分索引，但必须返回 `complete=false`、收录/跳过数量和范围，且后续查询不得把“部分索引无匹配”解释为 canonical 地图中不存在。

若场景没有兼容地图节点，工具不创建误导性的空支持文件，而是返回 `rebuild_skipped=no_compatible_map`。兼容地图为空但绑定了 TileSet/MeshLibrary 时，可以生成资源基线和合法空空间索引。

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
- [自然语言意图误判导致普通回复再次被 Gate 吞掉] → 默认分类为非编辑；续作文本必须由当前焦点和唯一可恢复任务共同解析，且只能继承该任务原授权上界；Gate 只处理绑定 lineage 的完成候选。
- [“继续任务”错误恢复久远地图任务] → resolver 不扫描任意历史任务，只接受当前唯一、最近聚焦、checkpoint 有效且原始授权明确的任务；多候选或失焦时消歧。
- [事务事件缓冲导致前端假超时] → 只把非持久化 `turn_progress` 放在提交缓冲之外，既维持存活可见性又不提前发布业务状态。
- [前端超时重放造成重复工具副作用] → 前端不得切模型或重发 `/chat`；fallback 仅由 provider 在 LLM attempt 边界执行，外层最终超时只发送带 cause 的 interrupt。
- [暂停原因与提示错配] → checkpoint 保存类型化 pause kind，formatter 穷举映射；无专用 report 时合成最小报告，只有 no-progress 类型显示连续无进展。
- [动态 Worker 通过全局注册表扩权] → 仅从 mode Capability Contract 中选取已注册工具，并继续应用 Skill、stage、permission 裁剪及编排工具剥离；不把完整注册表直接暴露给 Worker。
- [`user://` 绕过项目路径策略] → 仅对显式 scratch path 参数启用 scheme-aware 校验；项目路径仍走原有 deny/allow/symlink 边界，未知 scheme 和 `..` 一律拒绝。
- [VL 被用于精确地图事实] → schema、prompt 和结构化错误都明确视觉/数据边界；精确字段只能由地图数据工具提供。
- [上下文读取产生隐藏项目写入] → 使用独立 `writes_internal_cache` effect、固定内部路径和原子写入；不把它计为地图内容修改，也不向 reader 暴露通用写工具。
- [并发首次读取重复重建] → 进程内串行化并在锁内二次检查缺失状态；重建中的临时文件不作为有效支持数据发布。
- [资源 registry 人工语义无法恢复] → 只在文件缺失时生成确定性技术基线，明确返回人工别名未恢复；有效现存 registry 永不被上下文读取覆盖。
- [大地图索引超过容量] → 返回显式不完整状态、跳过数量和覆盖范围；查询结果保留不完整警告，不把零匹配升级为权威不存在。

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
12. 对齐所有动态 Worker 的 mode-derived 工具接口，加入截图 scratch path 合同、带 question 的视觉回看、artifact kind 恢复和 nullable structured repair 回归验证。
13. 为地图上下文工具增加固定路径的内部缓存 effect 和直接重建模块；先验证确定性输出、原子失败与并发首次读取，再启用缺失文件自愈。
14. 增加事务外 `turn_progress`、后端 attempt fallback 超时合同和带 cause 的 interrupt；先验证心跳不进入 Session/历史，再调整前端 watchdog。
15. 增加续作意图与当前任务指代解析、类型化 pause kind 和按原因格式化；移除旧 paused 地图帧对不相关请求的无条件错误响应。

## Open Questions

- approved write group 的默认最大工具数、时长和 snapshot 上限需要通过现有大型地图场景基准确定。
- 第三方 Skill 的 `required_capabilities` 是否需要独立 schema version，实施时应与 Skill catalog 迁移一起决定。
