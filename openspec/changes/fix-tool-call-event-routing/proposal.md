## Why

`chat_panel.gd _handle_event`（1520-1521 行）把 `tool_calls` 与纯展示性的 `agent_tool_calls` 事件一并送进 `_handle_tool_calls` 执行路径。服务端 `agent_tool_calls` 只携带 `{frame_id, agent, tools:[名字]}`（response_routing.py 142-150 行，服务端自行执行），没有 `calls` 键，于是每个该类事件触发一轮空转：`Handling tool calls count 0` → `submit_tool_results([])` → HTTP 请求被抑制并本地 emit 70 字符伪 error 响应 → `Error command acknowledged…`。日志中单批 46 个事件内出现 7 轮。不致命但产生日志噪音、状态抖动，且伪 error 携带 `retryable:true`/`next_action`，将来若被消费可能酿成真实死循环。

## What Changes

- `_handle_event` 只把 `tool_calls` 路由到 `_handle_tool_calls`；`agent_tool_calls` 仅保留 timeline 投影（展示块不变）。
- `_handle_tool_calls` 在 `calls` 为空时早退：不切状态、不提交空结果。
- `agent_http_client.send_tool_results` 对空批次保持抑制 HTTP 请求，但不再 emit 伪 error 响应（伪 error 仅保留给"非空但非法"批次）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `chat-event-streaming`: 增加要求——展示性编排事件永不触发执行（agent_tool_calls 仅投影；仅非空 calls 的 tool_calls 派发执行）。
- `atomic-tool-result-submission`: 增加要求——空工具结果批次为静默 no-op，不 emit 合成 error 响应。

## Impact

- 前端：`ui/chat_panel.gd`（_handle_event / _handle_tool_calls）、`service/agent_http_client.gd`（send_tool_results）。
- 服务端无改动（事件语义已正确，问题纯在前端路由）。
- 消除每 turn N 轮的 suppressed/伪 error 日志噪音与 WAITING_LLM 状态抖动。
