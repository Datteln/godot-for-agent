## Why

地图代理在 `ClassInfo TileMap · map-agent` 或 `Grep · map-agent …` 后仍持续产出 Thought、工具调用和审批，但 Godot 前端没有再显示任何后续条目。根因是某些只读工具把不受限的原始结果同时送入 LLM 上下文与终态实时展示稿：除完整 ClassDB 外，`grep_code` 还能以 `**/*` 命中 `logs/service.log` 的巨型单行，并将该行递归地带回事件载荷。这既制造毒帧，也浪费模型上下文。

需要让任何已持久化的用户可见转录条目，在实时补丁丢失、被拒绝或投影失败后自动追赶到权威会话快照，而不是静默停在最后一个已显示条目。

## What Changes

- 为客户端已接收、已投影与已渲染的转录事件建立可比较的连续游标和断档诊断。
- 在活跃请求中检测可见转录停滞、事件序号缺口、补丁解析/投影失败和订阅重置，并自动执行有界恢复：先从连续游标恢复事件，无法闭合时原子水合权威快照。
- 将停滞判定从“是否收到 WebSocket 事件”提升为“是否真正投影并渲染”：已接收但卡在流式补丁批处理、Projector 或视口的条目同样必须自动恢复；批处理积压和失活连接不得无限等待。
- 将实时传输改为“提交后确认”：事件只能在其对应 revision 已提交到权威 Store 并获视口接受后 ACK；接收游标不得被用作 ACK 或重连游标，避免未显示的事件被服务端当作已消费。
- 将 Reset 定义为中断屏障：先取消在途及排队的聊天/工具结果并请求服务端中断，再重置会话和水合新快照，禁止旧轮次在 Reset 后继续污染前端状态。
- 确保恢复路径覆盖 Thought、assistant、工具活动、审批和错误等所有用户可见 entry，而不仅是最后的 HTTP 响应或工具调用。
- 修复虚拟转录视口的布局测量与瞬时提示挂载：异常 Thought 高度不得污染 spacer/滚动位置，错误与报告提示必须紧随最后一个实际条目而可见。
- 将恢复保持为静默行为；保留脱敏、可关联 session/entry/revision/event sequence 的诊断，以便定位发布、传输、投影或渲染环节。
- 增加长 Thought、大地图工具结果、ClassInfo 后续工具链、丢失补丁及 projector 拒绝后的端到端回归覆盖。
- 将 `read_class_docs` 改为按需检索：LLM 只能请求有限数量的指定成员、指定常量或搜索候选，不能取得整类 ClassDB。
- 禁止完整 ClassDB/API 文本进入 WebSocket、可见 transcript 或持久化会话；工具卡片仅显示 `ClassInfo <class_name>`，不显示成员数量或原始 API 内容。
- 将检索工具的运行时目录排除、逐匹配摘录与模型结果表示定义为工具本身的契约：`grep_code` 不得扫描日志/服务状态目录，且不得返回未受限的整行文本。该规则不采用所有工具结果统一 4 KiB 上限；每类工具按其语义保留所需的有界事实。
- 对所有终态工具展示稿补丁执行入站字节预算校验：超预算时不得把原始载荷交给 Godot 的主线程解析，而是发送可恢复的受限摘要或显式重同步信号。

## Capabilities

### New Capabilities

<!-- None. -->

### Modified Capabilities

- `authoritative-chat-transcript`: 明确完整 ClassDB 与未受限检索原文不属于持久化可见 transcript，快照只保留受限展示元数据。
- `chat-event-websocket`: 定义可见转录补丁的序列连续性、订阅重置、终态工具补丁入站预算和脱敏断档诊断。
- `chat-event-resume`: 定义活跃请求的断档恢复、重放失败时的快照回退及有界重试。
- `chat-transcript-projection`: 要求客户端在补丁被拒绝或投影停滞后恢复权威状态，稳定测量虚拟条目高度、可见地呈现瞬时提示，并以不含成员数或原文的 ClassInfo 标题呈现文档查询。

## Impact

- Python AI 服务的 server 检索工具、工具结果净化、transcript 发布、事件保留/重放与会话历史接口。
- Godot 插件的 ClassDB 查询器、工具执行器、工具卡片渲染、TranscriptProjector、TranscriptViewport、瞬时提示宿主与 ChatPanel。
- 按需 API 查询、超大 ClassInfo 结果不外泄及地图作者流程的服务端与 Godot 集成测试。
