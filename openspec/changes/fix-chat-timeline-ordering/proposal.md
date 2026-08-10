## Why

聊天时间线存在两个用户可见缺陷，均源于 canonical Timeline 管线：发送者自己的消息在按下发送时不显示（只有等服务器 `user_submitted` WebSocket 回声到达才出现，而日志证明该回声可能晚于同一 turn 的第一批模型流式事件）；同时 store 的全局 order_key 排序混合了三种互不兼容的键空间（事件类整数 seq、消息/工具类字符串 frame_id、本地类固定 1e9 整数偏移），导致事件块与本地通知永远排在用户消息和助手内容之上，与时间顺序无关。

## What Changes

- `_on_send` 在发送瞬间渲染乐观本地用户气泡；`user_submitted` 回声到达时一次性对账（替换或丢弃本地临时条目），保证时间线中用户消息恰好一条。
- 统一 Timeline order_key 为单一可比较空间：投影器不再为消息/工具条目覆盖字符串 frame_id 键；本地条目基于已接受 seq 高水位取键，废弃 `1_000_000_000` 固定偏移。
- 服务端将 `user_submitted` 事件的发布点提前到该 turn 第一个模型展示事件之前，使回声本身按时间有序。
- `ChatTimelineStore._compare_order` 对混合类型 order_key 失败闭环（拒绝 mutation），不再静默退化为字符串比较。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `chat-event-streaming`: canonical 投影必须使用单一 order_key 空间；展示权威性增加一个窄例外——用户提交乐观本地渲染 + 回声一次性对账。

## Impact

- 前端：`ui/chat_panel.gd`、`controllers/chat_timeline_controller.gd`、`timeline/chat_timeline_projector.gd`、`timeline/chat_timeline_store.gd`。
- 服务端：`app/application/submission/turn_service.py` 等事件发布处（`user_submitted` 发布点）。
- 历史回放与实时渲染共用同一投影器，两者同时继承修复后的排序。
