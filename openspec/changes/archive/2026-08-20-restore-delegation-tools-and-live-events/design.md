# Design: Restore Delegation Tools And Live Events

## Context

事故链（实测于 brackeys-platformer-assets 会话 `session_1785151076`，2026-08-19 16:39–16:40）：

1. CodeAct 改造后 `app/main.py` 硬编码 `register_front_tools(enabled=False)`，front 工具一键全禁——但 `create_plan`/`delegate`/`delegate_many` 虽注册在 `front_tools/core_tools.py`，`side="server"` 且无 handler，执行由 orchestrator 的 frame 级路由（`response_routing._handle_create_plan`、`delegation*.py`）按工具名拦截，本不该被禁用。
2. coordinator 提示词（`coordinator.md`）仍强制"复杂地图任务必须先 create_plan 再 delegate"；工具缺失后 LLM 用 `tool.search` 换词检索 32 次（query 全部指向 create_plan/delegate），`_score` 词法打分在 6 个可见工具里恒 0 分，12 轮空转 → `agent_turn_budget_exhausted`。
3. 事件通道语义：`_emit_scoped` 只把 `agent_reasoning_delta`/`agent_text_delta` 作为 provisional preview 实时发布；其余事件全部 `transactional` 缓冲到 `flush()` 单次倾泻；出错时 `resolve_previews(committed=False)` 把思考 preview 整体删除。→ 用户看到"只有 Thought 流 → 结尾洪水 → Thought 消失"。

约束：前端 timeline（`chat_timeline_projector.gd`/`chat_timeline_store.gd`，godot-master 与游戏工程同源）已支持 provisional/committed/finalize/discard 语义与 tool 块投影；事件 store 按 seq 顺序消费；`_delivery_id` 已有幂等去重。

## Goals / Non-Goals

**Goals:**

- 恢复 `create_plan`/`delegate`/`delegate_many` 的服务端可用性（effective tools 恢复完整），不影响 CodeAct 对旧 front 工具的剪枝。
- `tool.search` 空转不可再发生：失败反馈契约 + 提示词护栏双保险。
- 失败轮次的思考/文本 preview 保留并带失败标记；工具事件运行期节流实时可见；成功与失败都能看到完整过程。

**Non-Goals:**

- 不改事件 store/背压协议、不改 WebSocket 批次机制（复用既有通道）。
- 不恢复旧 front 工具（scene edit / program tools 等维持禁用）。
- 不引入定时器/后台协程节流（见决策 5）。
- 不做 `tool.search` 语义搜索/中英匹配增强（此案例根因是工具缺失而非打分算法）。

## Decisions

### D1. 编排工具迁往 server 侧注册

新建 `app/tools/server_tools/orchestration_tools.py`，把 `delegate`/`delegate_many`/`create_plan` 三个 ToolDef 原样迁入（`side="server"` 已就位，无 handler——执行不经 `tool_execution`，frame 级路由已存在，仅缺注册）。`_object_schema`/`_worker_spec_schema` 内联复制而非从 `front_tools/_shared` 反向 import。`register_server_tools()` 追加 `register_orchestration_tools()`；删除 `front_tools/core_tools.py` 及 `front_tools/__init__.py` 中的 core 注册。

- 备选 a：`register_front_tools(enabled=True)` 放开——会把 93 个旧 front 工具全部带回，违背 CodeAct 剪枝意图。否决。
- 备选 b：留在 front_tools 但只放开 core——模块归属与"front=froce 剪枝"语义冲突，且未来再次被一刀切的风险仍在。否决。

### D2. 提示词护栏条款

所有依赖 `tool.search` 激活 deferred 工具且含委派职责的 agent 定义（coordinator、advisor、map-agent、programming-agent、scene-agent、resource-agent、map-planner-agent、map-reader-agent、map-reviewer-agent、map-validator-agent）追加统一条款（中文，一句规则 + 可执行阈值）：`tool.search` 连续 2 次空匹配必须停止换词重试；向用户说明缺失工具；改用现有工具或委派完成。放在"工具使用"段落，措辞与现有条目一致。

### D3. tool.search 护栏契约（服务端）

`search_tools_handler` 返回结构扩展（向后兼容，仅新增字段）：

- `visible_tools`: 可见工具名列表（`sorted(visible)`），LLM 一眼看清搜索边界；
- `advisory`: 当 visible+registered 全域匹配数为 0 时返回硬提示——"未找到任何工具，继续搜索不会得到新结果；请改用可见工具或向用户说明"；
- 进程内护栏：模块级 `_empty_match_streak: dict[(session_id, agent), int]`，`matches==0` 时 +1 且写入 `search_stop: true`；`>=3` 时 advisory 升级为强制指令（"search_stop：禁止继续搜索"）；任何非空匹配清零。

内存计数仅在服务进程生命周期有效，服务重启自然清零——可接受，护栏只防一次连续空转。

### D4. 失败 preview 保留语义（chat-event-streaming 修改）

`resolve_previews` 的 `committed` 取值按错误类别划分，替代现在的 `not isinstance(response, ChatErrorResponse)` 一刀切：

- **agent 层错误**（`ChatErrorResponse`，如 `agent_turn_budget_exhausted`、模型错误、对话被拒）：`committed=True, reason=error_code`。此时 `save_task_run` 已成功、会话状态已推进，preview（思考/文本/工具事件）全量保留并 finalize，前端标记失败原因；
- **基础设施失败**（`session_persistence_failed`、`turn_identity_recovery_failed` 冲突恢复失败）：维持 `committed=False`——工具结果未持久化，保留会误导用户。

`submission_preview_committed` 事件携带 `reason`（现有字段）与错误 problem fields（commit_service error 分支已有的 update 逻辑顺带覆盖 preview 条目）。前端 `_preview_mutations(payload, "finalize")` 增加 status 透传（失败标记）。

### D5. 运行期事件节流实时推送（chat-event-streaming 修改）

分类实时化，复用既有 preview 边界机制，不新增通道：

- **实时化集合**：前端会展示的事件——`agent_tool_calls`、`server_tool_start`、`server_tool_result`、`agent_step`。`context_usage`/`cache_hit`/`agent_model_selected` 属 `NON_PRESENTATION_EVENTS`，维持 transactional 缓冲（到达时间不影响 UI）；
- **preview_id 复用**：这些事件沿 `_publish_preview` 的 preview 登记路径走——`_emit_scoped` 对实时化集合打 `delivery=transactional, provisional=True`，`preview_id` 按同构规则生成（`request_id:turn_id:<type>:<message_id/tool_use_id>`），并 `scope.preview.add(...)`。于是 `submission_preview_*` 边界的 `preview_ids` 自动涵盖工具条目，前端 projector 的 `_tool_call_mutations`/`_insert_event_item` 已带 `source.preview_id`，committed/discard 边界按 preview_id 统一 finalize/remove——**前端投影与 store 无需新增逻辑**；
- **触发式节流（无定时器）**：`SubmissionScope` 增加 `live_buffer: list[BufferedEvent]` 与 `last_live_flush: float`。每次 live 事件入队后同步检查：`len(live_buffer) >= 4 或 now - last_live_flush >= 0.25s` 即批量 append 到 event store 并清空。提交时 `flush()` 先清空剩余 live_buffer。事件在 provider 调用间隙（~2.5s）自然聚簇，形成小批量实时流；
- **无重复提交**：`scope.live_preview_ids: set[str]` 记录已实时发布的 preview_id（事件以最终 payload 发布，无中间形态），`flush()` 发布 transactional 批时**跳过**已 live 发布的条目（`_delivery_id` 幂等兜底），确保提交后不重复渲染。

备选：委托 `asyncio.create_task` 定时 flush——引入竞态与事务边界复杂度，否决。备选：live 发布后提交时照发权威副本并依赖前端去重——前端无此去重逻辑，兼容负担大，否决。

### D6. 前端打字机平滑渲染（展示层，仅前端）

模型推理/正文 chunk 粒度大（40~110 字/块、0.5~1s/块），而 `chat_virtual_scroller._on_store_mutation` 对每个 patch 都整节点重建（`_refresh_index` → `create_item_node` 全量 set text），视觉上"跳一大段"。改动：**patch 不再直接整块 set text，而是把增量文本交给一个逐帧 reveal 队列**（每帧 ≤2 字符），在下一个 delta 到达前均匀吐出；渲染仍以 item 最新 `text` 为权威目标。

- 落点：`timeline/chat_item_renderer_registry.gd`（markdown/reasoning 块渲染路径）+ `ui/chat_virtual_scroller.gd`（patch 刷新入口），新增一个 `StreamRevealQueue`（每 item 一个，存"已显示长度"与队列）。
- 边界：`finalize` 立即排空剩余字符；`discard`/`remove`/`reset_epoch` 丢弃队列；`MAX_MESSAGE_RENDER_CHARS`/`MAX_REASONING_RENDER_CHARS` 限长语义不变（截断发生在目标文本上，不平滑段）；滚动跟随依赖 height 变化，打字机期间高度不变，不进 scroll 扰动。
- 纯展示层：不动服务端、事件协议、store；与既有 frame-budget 渲染（`chat-event-streaming` 需求）并行不冲突。

备选 B（服务端把 chunk 切成 ~15 字符小片）：事件量 ×3~5，需复核 store coalesce 与 WS 背压参数，且块间隔不变、视觉改善有限——否决。

## Risks / Trade-offs

- [失败保留使 chat 历史包含失败轮内容] → 语义明确化：`submission_preview_committed` 的 `reason` 必填（错误码），前端渲染失败标记；中断/回滚路径仍 discard，规则可预测。
- [live 实时发布后进程崩溃，事件已入 store 但会话未提交] → 既有 `_delivery_id` 幂等 + `record_recovery` 指针语义不变；重启后按 recovery 指针续跑，重复 append 被幂等吸收。与既有 preview 同级别风险，无新增暴露。
- [节流窗口 0.25s/4 条启发式不合特定负载] → 常量集中在 `publication.py` 顶部，配置化成本低；对 12 轮/70 条的事件量，实际批大小 < 5。
- [编排工具迁移可能影响依赖 front_tools 注册的测试] → `test_extended_godot_tools.py` 等显式 `register_front_tools()` 的用例改调 `register_server_tools()` 断言；跑全量 tests。
- [游戏工程 addon 与 godot-master 前端不同步] → 边界 payload 字段均向后兼容（新增字段，前端旧版本忽略）；`provisional=True` 的工具事件旧前端也能投影（已有 tool 块渲染路径）。前后端仍建议同版本部署。

## Migration Plan

1. 合并 D1（最小可修复集，副作用为零）→ 跑 test_public_route 端到端。
2. 合并 D3 + D2（护栏）。
3. 合并 D5 → 对照运行日志验证实时流与提交去重。
4. 合并 D4（失败保留语义）→ 验证错误轮显示思考轨迹。
5. 游戏工程 addon 同步前端文件（如存在差异）。
6. 回滚：各项独立可 revert；D5/D4 依赖同批次前端（向后兼容字段，旧前端回退安全）。

## Open Questions

- 失败轮保留的 preview 是否计入会话上下文（下一轮 prompt 会看到失败内容）？当前设计不计入（preview 不进 history，`record_history_event` 对 preview 类型不记录）——保留为现状，不做上下文注入。