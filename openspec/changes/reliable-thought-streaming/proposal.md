## Why

一次地图请求中，服务端持续生成并发布了用户可见的 Thought，但 Godot 前端没有接收或投影该条目；界面仅显示等待状态。用户因此无法判断模型正在工作、卡住，还是已失去事件流。

现有规范覆盖 Thought 持久化、WebSocket 补丁和恢复的各个部分，但缺少从可见推理产生到前端呈现的端到端交付保证与可操作诊断。

## What Changes

- 为模型输出的全部思考内容建立端到端交付保证：前端必须逐 token 实时投影和显示，或在检测到丢失、停滞或无法投影时自动恢复至权威快照。
- 增加 Thought 专用的事件接收、投影、渲染与恢复诊断；诊断保持脱敏且可关联 session、entry、revision 和 event sequence。
- 在活跃请求持续产生用户可见 Thought 而前端未推进该 Thought 时，触发有界恢复，而不是仅因任意事件活动而无限延长等待。
- 不再按“用户可见”标记过滤模型已经返回的思考内容；所有提供方实际输出的思考 token 都需要可靠交付和渲染。
- 不显示“推理内容暂未送达”之类的恢复提示；恢复仅通过后台诊断和自动重放/快照完成。
- 增加断流、订阅积压、补丁被拒绝及恢复后的端到端回归覆盖。

## Capabilities

### New Capabilities

<!-- None. -->

### Modified Capabilities

- `authoritative-chat-transcript`: 明确用户可见 Thought 的可交付性与恢复后的最终可见性。
- `chat-event-websocket`: 为可见 Thought 补丁加入端到端交付、停滞检测和可恢复失败语义。
- `chat-event-resume`: 在活跃 Thought 未推进时定义基于 Thought 游标的恢复与快照回退。
- `chat-transcript-projection`: 要求客户端对已接收或恢复的可见 Thought 最终投影并渲染。
- `chat-transcript-navigation`: 扩充不可见 Thought、被拒绝补丁及恢复结果的红脱敏诊断。

## Impact

- 服务端事件发布、订阅队列、会话/事件游标和 transcript writer。
- Godot 插件 WebSocket 客户端、ChatPanel、TranscriptProjector、批处理器与渲染/诊断路径。
- WebSocket、断线重连、历史水合和长 Thought 的集成测试。
