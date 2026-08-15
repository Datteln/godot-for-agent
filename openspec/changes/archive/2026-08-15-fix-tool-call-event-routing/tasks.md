## 1. 路由与早退

- [x] 1.1 `chat_panel.gd _handle_event`：`agent_tool_calls` 不再路由进 `_handle_tool_calls`，仅保留投影
- [x] 1.2 `chat_panel.gd _handle_tool_calls`：`calls.is_empty()` 时早退（不切状态、不提交）

## 2. 空批次静默化

- [x] 2.1 `agent_http_client.gd send_tool_results`：空批次仅 debug 日志，移除伪 error emit；非空非法批次行为不变

## 3. 验证

- [x] 3.1 回归测试：注入 agent_tool_calls 事件，断言无执行、无提交、无状态变化、时间线展示块照常
- [x] 3.2 回归测试：空 calls 的 tool_calls 事件为 no-op；非空 calls 正常执行回传
- [ ] 3.3 日志验证：同一 turn 不再出现 "No valid tool results to send; request suppressed" 与 70 字符伪 error 成对空转
