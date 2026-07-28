## Why

map-agent 的核心安全整改已建立 revision、阶段合同和能力清单，但尚未完成的事务原子性、计划依赖、Skill 绑定、动态 Worker 工具可达性、截图视觉回看、结果来源、完成证据、Undo 语义和无进展恢复仍留下可绕过或可崩溃的边界。现在需要把这些分散问题收敛为可验证的运行时合同，避免继续通过 prompt、影子规划器、父 Agent 工具交集和重复白名单维持流程正确性。

## What Changes

- 将前端工具结果提交改为“全批预检、会话副本计算、一次提交”，任何非法后项都不得留下前项副作用。
- 将 `describe_map_region` 等大型地图工具结果收敛到每个 Session 唯一的 `map_artifacts.json`；文件内按 `turn_id + tool_use_id` 保存结果块，不再为每次调用生成 `describe_map_region-*.json`。
- 明确 artifact 的 staged/committed 生命周期：提交前的结果只允许通过事务内暂存读取，Session 提交后才发布正式引用；interrupt、取消或回滚不得留下对应 turn 块。
- 统一地图 artifact 类型、读取工具和提示合同，禁止提示 Agent 调用当前 scope 不可达的 `read_file`，也禁止用只支持 delegate schema 的 reader 读取地图工具原始结果。
- 让 orchestrator 执行不可变 plan step DAG：仅在依赖成功后启动后继，并把前驱 typed result 显式绑定为后继输入。
- 新增 Skill Binding Module，按当前 Agent、stage、worker mode 解析 `resolved/missing/incompatible`，停止信任全局 `effective_tools`。
- 将地图上下文、节点树和精确地图事实读取稳定路由到具备对应能力的 reader；工具搜索不得突破 Agent Interface 或 Capability Contract，地图总控不得用搜索重试替代职责委派。
- 修正动态地图 Worker 的工具派生：Worker 的 Agent Interface 由自身 mode Capability Contract、注册表、Skill binding、stage 和权限共同形成，不再与父 map-agent 的窄工具集求交；否则 read-only/planner/reviewer 会丢失 `describe_map_context`、`describe_map_region`、`read_scene_tree` 和 `read_image_metadata`。
- 建立截图的双方案路径合同：保留通用 `to_res_path` 的项目边界，同时让截图写入和图片回看通过专用 scheme-aware 路径解析安全支持 `res://`、`user://` 与项目相对路径，并继续拒绝绝对路径、未知 scheme 和 `..`。
- 扩展 `read_image_metadata` 为可带 `question` 的视觉回看工具，问题作为独立、限长的 VL prompt 传入 asset-understanding，而不是替换图片 `type_hint`；视觉回答不得作为 tile 坐标、source id 或 atlas 坐标的权威来源。
- 对图片引用误传给 map/delegate artifact reader 的情况返回结构化 `incompatible_artifact_kind` 和正确恢复工具，不再抛裸 `ValueError`/`OSError`；同时修复结构化输出恢复路径对 nullable `structured_issues` 的二次迭代崩溃。
- 让 `describe_map_context` 对缺失的 `resource_registry.json` 和 `spatial_index.json` 执行同调用、确定性的内部支持数据重建：直接扫描真实 TileMap/TileMapLayer/GridMap 并原子落盘，不创建 plan step、不切换 planner/writer、不授予 reader 通用写权限。
- 收紧地图目标参数合同：`target_path="."` 不得被解释为自动选择地图节点；调用方应省略参数触发唯一目标推断，或根据结构化候选补充真实节点路径。
- 强制校验 map worker result 的 stage、target、revision、next_stage 和 Frame contract，防止来源伪造及动态 Worker 名称冲突。
- 建立唯一 Completion Gate，运行时校验 reviewer/validator 的成功截图证据，prompt 不再自行决定 `completion_allowed`。
- 将 Completion Gate 收紧为当前请求作用域：只有用户本轮明确要求创建、修改或修复地图内容时才建立 `map_edit` 意图；普通聊天、地图读取/分析/检查、只规划不执行以及历史地图 `task_id` 均不得触发 Gate。
- 将续作意图与地图编辑授权分离：`继续任务` 等指代表达在当前会话存在唯一、仍聚焦且已授权的可恢复任务时绑定原 `task_id`、checkpoint 和权限范围；无候选、多候选或任务已失焦时不得从任意历史地图状态推断授权。
- 区分模型 attempt 超时与前端 `/chat` 空闲超时：模型连接/请求失败由后端 provider 在同一 LLM attempt 边界切换已配置的 fallback；长事务通过不改变 Session 的进度心跳保持可观测，前端不得通过重放 `/chat` 实现模型切换。
- 将客户端等待超时、用户主动停止、模型耗尽、预算耗尽和真正 no-progress 建模为不同暂停类型；检查点和用户提示必须与实际原因一致，不得把 `client_timeout`/`user_interrupted` 显示为“连续无进展”或输出空 `{}` 报告。
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

- Python：`query/engine.py`、`query/helpers.py`、`orchestrator/agent.py`、`orchestrator/map_workers.py`、`orchestrator/map_request_scope.py`、`llm/provider.py`、`rag/asset_llm_client.py`、artifact readers、plan/delegate、Skill catalog/load、map worker contracts、Session map state、内部缓存 effect，以及 request-scoped map-edit intent/response lineage、续作指代和类型化暂停。
- Artifact 持久化：Session 级 `map_artifacts.json` schema、按 turn/tool entry 定位、事务内暂存读取、原子合并、取消回滚、幂等重试和兼容迁移。
- Godot：PathUtils 的截图专用路径解析、截图输出、图片元数据回看、ToolExecutor、UnifiedUndoManager、地图支持数据确定性重建、地图写入/验证工具、revision tracker，以及 `/chat` 心跳/空闲超时与带 cause 的 interrupt。
- Agent/Skill 定义：map-agent、planner、reader、writer/validator/reviewer 动态模板、地图读取职责路由、scope-aware 工具提示和两个地图 Skill。
- API/持久化：工具结果提交语义、plan step/result schema、Skill binding result、map workflow event/checkpoint。
- 测试：新增原子提交、事务外心跳、provider fallback、类型化暂停、上下文续作解析、DAG 依赖、contract spoof、截图证据、Undo/Redo/重启、平台轨迹和 no-progress 分类回归测试。
