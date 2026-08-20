# Restore Delegation Tools And Live Events

## Why

CodeAct 改造将全部 front 工具注册硬编码为禁用（`register_front_tools(enabled=False)`），连带禁用了本应服务端执行的编排工具 `create_plan`/`delegate`/`delegate_many`；而 coordinator 提示词仍强制要求先 `create_plan` 再 `delegate`。LLM 于是通过 `tool.search` 反复检索不存在的工具，连续 12 轮空转后触发 `agent_turn_budget_exhausted`（实测会话 service.log 16:39:30–16:40:20，32 次搜索全部 `matches=0`）。同时事件通道只在运行期推送文本/思考预览，工具事件全部缓冲到提交时单次倾泻；失败轮次的思考预览又被整体丢弃，用户既看不到过程也看不到失败原因。

## What Changes

- 恢复服务端注册 `create_plan` / `delegate` / `delegate_many`：把三个 schema 从 `front_tools/core_tools.py` 迁至 server 侧注册（`register_server_tools()`），coordinator 的 effective tools 从 6 恢复为完整列表。
- coordinator 与其依赖 `tool.search` 激活 deferred 工具的 agent 提示词增加防空转条款：连续搜索匹配不到即停止换词重试，说明缺失工具并改用现有工具。
- 强化 `tool.search` 失败反馈：返回可见工具白名单与明确 advisory；进程内护栏统计连续空匹配，超过阈值返回 hard-stop 指令。
- **BREAKING（事件语义）**：失败轮次的 provisional preview（Thinking/文本增量）不再 discard，改为保留并标记失败原因——用户可见 agent 失败前的完整思考轨迹。
- **BREAKING（事件推送时序）**：`agent_tool_calls` / `server_tool_start` / `server_tool_result` / `agent_step` 等运行期事件改为按节流窗口批量实时推送，替代仅在提交时一次性倾泻；事务语义通过既有 `_delivery_id` 幂等提交保持。

## Capabilities

### New Capabilities

- `tool-search-guardrails`: tool.search 的失败反馈契约（visible_tools 白名单、advisory、连续空匹配 hard-stop 阈值）以及 agent 提示词不得无限重试的护栏约束。

### Modified Capabilities

- `chat-event-streaming`: 预览生命周期需求扩展——失败提交保留并标记 preview 而非 discarding；新增运行期事件节流实时推送需求（工具/步骤事件不等待提交）。
- `codeact-execution-gateway`: 工具可用性需求修正——`create_plan`/`delegate`/`delegate_many` 必须在服务端始终注册，不受旧 front 工具禁用开关影响；角色 effective tool set 依此解析。

## Impact

- `ai_agent_service/app/main.py`：无改动（沿用 `register_server_tools()`）；front 工具禁用开关维持，仅编排工具迁出。
- `ai_agent_service/app/tools/`：新增 `server_tools/orchestration_tools.py`（迁入三个 schema）；`front_tools/core_tools.py` 与 `front_tools/__init__.py` 移除 core 注册；强化 `server_tools/search_tools.py`。
- `ai_agent_service/app/agents/agent_defs/*.md`：6+ 个 agent 提示词同步防空转条款。
- `ai_agent_service/app/application/publication.py`：preview 解析语义（失败保留+标记）与运行期节流推送通道。
- `ai_agent_service/app/application/submission/commit_service.py`：失败路径 `resolve_previews` 调用参数变化；刷新点与节流刷新器接线。
- 前端 `ai_agent_frontend/addons/ai_agent/`（及游戏工程副本）：投影器/时间线处理"保留但标记失败"边界与运行期工具事件批次；渲染端无需改动。
- 测试：`tests/test_public_route.py`、`tests/test_chat_event_streaming.py`、`tests/test_extended_godot_tools.py` 等受影响用例更新。