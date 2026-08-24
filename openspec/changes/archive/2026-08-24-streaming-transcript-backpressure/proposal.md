## Why

长 Thought 或助手回答以累计全文形式高频发送时，服务端、WebSocket 和 Godot 渲染器会重复处理不断增长的载荷。一次本应继续执行的任务可能因此停止向前端交付事件，最终被“无事件”看门狗错误地 `/chat/interrupt`。现在需要让实时展示在慢客户端和长流下仍保持有界、可恢复，并且不误杀健康任务。

## What Changes

- 为流式 Thought 与助手正文引入有界的实时补丁表示：实时传输只发送追加增量或受限的最新预览，完整条目继续由权威 transcript 与历史快照保存。
- 按 `entry_id` 合并尚未发送的流式补丁，并对慢订阅者采用显式重同步，避免无界堆积旧的累计快照。
- 前端把 WebSocket 接收、Store 投影和视图渲染解耦；同一条目在一个渲染窗口内仅应用最新修订，避免每个包都同步重排并滚动。
- 将活跃 turn 的轻量进展信号与 WebSocket 传输心跳区分；请求空闲超时先尝试重连/快照恢复，只有恢复失败或达到硬上限才取消后端 turn。
- 将空正文模型恢复视为同一逻辑 Thought 的连续尝试：中间尝试不得提前固化 Thought 时长，恢复流也不得向已完成的 Thought 追加内容并保留过期耗时。
- 增加红脱敏的端到端诊断指标：补丁字节数、合并/重同步、队列深度、socket 交付与渲染延迟、超时的恢复结果。
- 为 `describe_map_region` 暴露 400-cell 范围约束或安全分块，减少可恢复的前端工具参数失败。

## Capabilities

### New Capabilities

- `streaming-transcript-backpressure`: 有界流式补丁、慢订阅者合并与重同步、turn 进展恢复和端到端背压诊断。

### Modified Capabilities

- `chat-event-websocket`: 实时 transcript 更新的有效载荷和发布策略改为有界且可合并。
- `chat-event-resume`: 慢客户端及请求空闲时的重连、重放/快照恢复语义扩展为不误取消健康 turn。
- `chat-transcript-projection`: 客户端接收流式修订后按条目合并并异步投影到 Store/视图。
- `chat-transcript-navigation`: 流式更新的渲染频率和自动滚动须保持有界并保留最终状态。

## Impact

- 后端：`app/orchestrator/agent.py`、`app/transcript/writer.py`、`app/events/store.py`、`app/events/websocket.py`、`app/query/engine.py`、工具 schema/说明和相关测试。
- Godot 前端：`agent_http_client.gd`、`chat_event_socket.gd`、`chat_panel.gd`、transcript projector/renderer/viewport 及测试。
- 协议：WebSocket 流式正文补丁扩展为增量或受限预览；历史快照保持完整条目，现有 resume 游标和单调 revision 不变。
