## Why

map-agent 的核心安全整改已建立 revision、阶段合同和能力清单，但剩余 17 项仍在事务原子性、计划依赖、Skill 绑定、结果来源、完成证据、Undo 语义和无进展恢复之间留下可绕过的边界。现在需要把这些分散问题收敛为可验证的运行时合同，避免继续通过 prompt、影子规划器和重复白名单维持流程正确性。

## What Changes

- 将前端工具结果提交改为“全批预检、会话副本计算、一次提交”，任何非法后项都不得留下前项副作用。
- 将 `describe_map_region` 等大型地图工具结果收敛到每个 Session 唯一的 `map_artifacts.json`；文件内按 `turn_id + tool_use_id` 保存结果块，不再为每次调用生成 `describe_map_region-*.json`。
- 明确 artifact 的 staged/committed 生命周期：提交前的结果只允许通过事务内暂存读取，Session 提交后才发布正式引用；interrupt、取消或回滚不得留下对应 turn 块。
- 统一地图 artifact 类型、读取工具和提示合同，禁止提示 Agent 调用当前 scope 不可达的 `read_file`，也禁止用只支持 delegate schema 的 reader 读取地图工具原始结果。
- 让 orchestrator 执行不可变 plan step DAG：仅在依赖成功后启动后继，并把前驱 typed result 显式绑定为后继输入。
- 新增 Skill Binding Module，按当前 Agent、stage、worker mode 解析 `resolved/missing/incompatible`，停止信任全局 `effective_tools`。
- 将地图上下文、节点树和精确地图事实读取稳定路由到具备对应能力的 reader；工具搜索不得突破 Agent Interface 或 Capability Contract，地图总控不得用搜索重试替代职责委派。
- 收紧地图目标参数合同：`target_path="."` 不得被解释为自动选择地图节点；调用方应省略参数触发唯一目标推断，或根据结构化候选补充真实节点路径。
- 强制校验 map worker result 的 stage、target、revision、next_stage 和 Frame contract，防止来源伪造及动态 Worker 名称冲突。
- 建立唯一 Completion Gate，运行时校验 reviewer/validator 的成功截图证据，prompt 不再自行决定 `completion_allowed`。
- 将 Completion Gate 收紧为当前请求作用域：只有用户本轮明确要求创建、修改或修复地图内容时才建立 `map_edit` 意图；普通聊天、地图读取/分析/检查、只规划不执行以及历史地图 `task_id` 均不得触发 Gate。
- 统一地图状态变更为按 target/revision 作用域的事件与 reducer，禁止 Agent/QueryEngine 直接写流程状态。
- 明确地图 Undo 事务边界并支持批次失败整体回滚、Ctrl+Z/Redo 和重启恢复。
- 加强平台规划验证：轨迹采样、碰撞与头顶净空、segment 几何一致性和可信 collision facts。
- 删除服务层影子规划器、重复工具白名单、重复阶段说明和自动子 Frame 中重复的系统规则。
- 为结构化输出修复与 no-progress 暂停增加分类、首个根因、同类错误熔断和 reader 回补链路。
- **BREAKING**：动态 Worker 的 Skill、工具和结果合同将严格校验；旧的无依赖 `delegate_many`、未绑定 Skill、无证据完成结果和 legacy completion 字段将被拒绝。

## Capabilities

### New Capabilities

- `atomic-tool-result-submission`: 工具结果批次在产生副作用前完成全量校验，并以会话事务一次提交。
- `dependency-aware-map-plans`: 不可变计划步骤、依赖门、typed result 传递和失败短路。
- `skill-worker-binding`: Skill 与当前 Agent、阶段、worker mode、有效工具的可信绑定解析。
- `map-workflow-state-and-evidence`: 按 target/revision 的事件状态机、worker 结果来源校验和唯一完成证据门。
- `map-edit-transactions`: 地图写入批次的统一 Undo/Redo、失败回滚和重启一致性。
- `platform-traversal-validation`: 基于可信场景事实的轨迹、碰撞、净空和 segment 几何验证。
- `map-progress-recovery`: 分类重试、结构化修复问题、missing-input 回补和带首因的无进展暂停。

### Modified Capabilities

无。当前 `openspec/specs/` 尚无既有 capability，本 change 将建立首批行为规格。

## Impact

- Python：`query/engine.py`、`query/helpers.py`、`orchestrator/agent.py`、plan/delegate、Skill catalog/load、map worker contracts、Session map state，以及 request-scoped map-edit intent/response lineage。
- Artifact 持久化：Session 级 `map_artifacts.json` schema、按 turn/tool entry 定位、事务内暂存读取、原子合并、取消回滚、幂等重试和兼容迁移。
- Godot：ToolExecutor、UnifiedUndoManager、地图写入/验证工具及 revision tracker。
- Agent/Skill 定义：map-agent、planner、reader、writer/validator/reviewer 动态模板、地图读取职责路由、scope-aware 工具提示和两个地图 Skill。
- API/持久化：工具结果提交语义、plan step/result schema、Skill binding result、map workflow event/checkpoint。
- 测试：新增原子提交、DAG 依赖、contract spoof、截图证据、Undo/Redo/重启、平台轨迹和 no-progress 分类回归测试。
