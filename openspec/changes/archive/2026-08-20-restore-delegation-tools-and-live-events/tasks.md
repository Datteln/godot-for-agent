# Tasks: Restore Delegation Tools And Live Events

## 1. Restore orchestration tool registration (D1)

- [x] 1.1 新建 `ai_agent_service/app/tools/server_tools/orchestration_tools.py`：内联 `_object_schema`/`_worker_spec_schema` helper，从 `front_tools/core_tools.py` 迁入 `delegate`/`delegate_many`/`create_plan` 三个 ToolDef（原样，含 `side="server"`），定义 `register_orchestration_tools()`
- [x] 1.2 `app/tools/server_tools/__init__.py`：`register_server_tools()` 追加 `register_orchestration_tools()` 调用
- [x] 1.3 删除 `app/tools/front_tools/core_tools.py`；`front_tools/__init__.py` 移除 `register_core_tools` import 与调用
- [x] 1.4 更新测试：`tests/test_extended_godot_tools.py` 及其余显式 `register_front_tools()` 后用 `delegate`/`create_plan`/`delegate_many` 断言的用例改为 `register_server_tools()` 路径
- [x] 1.5 验证：`pytest tests/ -k "public_route or extended_godot or scheduler"` 通过；`register_all()` 后 REGISTRY 含三个编排工具

## 2. tool.search failure feedback and guardrail (D3)

- [x] 2.1 `app/tools/server_tools/search_tools.py` handler：返回增加 `visible_tools`（排序的可见工具名列表）与 `advisory`（visible 与 hidden 全域均 0 匹配时的硬提示文案）
- [x] 2.2 同一模块：模块级 `_EMPTY_MATCH_STREAK: dict[tuple[str, str], int]`（key=session_id+agent/probe 标识），`matches==0` 递增并返回 `search_stop: bool`，≥3 时 advisory 升级为禁止继续搜索指令；非空匹配清零
- [x] 2.3 测试：新增 `tests/test_search_tools_guardrails.py`——空结果含 visible_tools/advisory、三次空匹配触发 search_stop、成功匹配重置计数
- [x] 2.4 验证：`pytest tests/test_search_tools_guardrails.py` 通过

## 3. Agent prompt anti-spin clauses (D2)

- [x] 3.1 `coordinator.md`：工具使用段落追加统一条款（tool.search 连续 2 次空匹配必须停止换词重试、说明缺失工具、改用现有工具或委派）
- [x] 3.2 `advisor.md` / `map-agent.md` / `programming-agent.md` / `scene-agent.md` / `resource-agent.md` / `map-planner-agent.md` / `map-reader-agent.md` / `map-reviewer-agent.md` / `map-validator-agent.md`：追加同款条款
- [x] 3.3 验证：`tests/test_agent_loader.py`（或对应加载测试）断言各 agent 定义 prompt 含护栏条款；确认这些 .md 的未提交改动不被覆盖

## 4. Run-phase throttled live events (D5)

- [x] 4.1 `app/application/publication.py`：`_submission_event_delivery` 增加实时化集合（`agent_tool_calls`/`server_tool_start`/`server_tool_result`/`agent_step`）→ throttled-live 交付分支
- [x] 4.2 `SubmissionScope`：新增 `live_buffer`、`last_live_flush`、`live_preview_ids` 字段；`_publish_preview` 路径推广到实时化事件（打 `provisional=True` + 同构 preview_id 并 `scope.preview.add`），非预览事件复用现有 append-to-store 逻辑
- [x] 4.3 `SubmissionPublisher.flush()`：提交前先清空剩余 live_buffer；发布 transactional 批时跳过已在 `live_preview_ids` 的条目（忽略重复）
- [x] 4.4 节流常量（批大小 4 / 窗口 0.25s）置于模块顶部；触发式 flush 检查点实现
- [x] 4.5 测试：扩展 `tests/test_chat_event_streaming.py`——运行期工具事件在提交前已可达、提交后无重复事件、边界 preview_ids 涵盖工具条目
- [x] 4.6 验证：`pytest tests/ -k "chat_event_streaming or publication"` 通过

## 5. Failure-boundary preview retention (D4)

- [x] 5.1 `commit_service.py`：主成功/错误路径 `resolve_previews(committed=...)` 改为——agent 层错误（ChatErrorResponse 已提交）`committed=True, reason=error_code`；基础设施失败分支（`session_persistence_failed`/`turn_identity_recovery_failed`）保持 `committed=False`
- [x] 5.2 error 分支的 problem fields update 逻辑确保 `submission_preview_committed` 携带 reason 与必要错误标识
- [x] 5.3 前端 `chat_timeline_projector.gd`（godot-master 与游戏工程副本同步）：`_preview_mutations(payload, "finalize")` 透传 `reason` 作为 status（失败标记渲染，如"失败：agent_turn_budget_exhausted"）
- [x] 5.4 测试：`tests/test_chat_event_streaming.py` 增补失败提交保留 preview + reason 场景；检查既有 rollback 场景仍 discard
- [x] 5.5 验证：`pytest tests/ -k "chat_event_streaming or commit or public_route"` 通过

## 6. End-to-end verification

- [x] 6.1 全量 `pytest tests/` 通过（688 passed + 43 subtests）
- [ ] 6.2 冒烟：起服务 + 模拟生产场景（地图任务），确认 coordinator 第一轮直接 `create_plan`（`tools=9`），无 tool.search 死循环（需真实 LLM + Godot 环境）
- [ ] 6.3 事件时序验证：运行期日志显示 agent_tool_calls/server_tool_start/server_tool_result 分批实时到达前端；失败轮（budget 耗尽）chat 面板保留 Thought 轨迹并显示失败标记（需真实 LLM + Godot 环境）
- [x] 6.4 游戏工程 `D:\godot\brackeys-platformer-assets\addons\ai_agent\` 前端文件与 godot-master 同步（inode 相同，硬链接同一份文件，改动自动生效）

## 7. Frontend smooth streaming reveal (D6)

- [x] 7.1 新增 `timeline/stream_reveal_queue.gd`（或等价）：每个流式 item 一个队列状态（目标文本 + 已显示长度），`advance(max_chars)` 按帧推进
- [x] 7.2 `ui/chat_virtual_scroller.gd` patch 刷新路径接入队列：流式 patch 不重建节点（重填内层文本 + 推进队列），非 streaming（committed/static）item 保持整块重建；**用户展开的 Thought 折叠块在刷新中保持展开**
- [x] 7.3 `timeline/chat_item_renderer_registry.gd` + `ui/log_entry_renderer.gd`：markdown/reasoning 块渲染支持 visible_characters 两段式呈现（完整 bbcode 一次写入 + 渐进显示）；`write_rich_text` 拆出供增量重填；限长常量语义不变
- [x] 7.4 生命周期边界：finalize 走重建路径自然排空；discard/remove/reset_epoch 丢弃队列；`chat_panel._process` 逐帧驱动推进并贴底跟随
- [ ] 7.5 验证：Godot 运行观察打字机平滑效果与 Thought 折叠保持；事件接受顺序/计数不变（无服务端改动，跑既有前端行为一致）