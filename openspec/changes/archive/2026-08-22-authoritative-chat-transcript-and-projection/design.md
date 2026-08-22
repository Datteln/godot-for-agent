## Context

当前聊天面板把 HTTP 最终响应、WebSocket 事件和历史 `blocks` 分别渲染为节点。
历史 `blocks` 在读取时由 agent frame、持久化事件和文本前缀推断；实时正文以
`frame_id + loop` 管理流，最终正文又以文本指纹去重。该架构没有一份可作为事实
来源的可见聊天记录，因而无法保证实时内容、重连后的内容和历史加载内容相同。

WebSocket 仍是唯一实时传输，HTTP 仍负责命令和历史读取。此次不改变工具执行、
LLM 推理或权限模型；只重新定义用户可见聊天事实的产生、持久化和投影方式。

## Goals / Non-Goals

**Goals:**

- 每个新会话拥有一份服务端权威、可持久化且有稳定身份的可见展示稿。
- 历史快照与完成态实时会话的条目身份、顺序、类型、状态和内容完全一致。
- 一个助手正文流和它的最终完成态只能更新同一个条目。
- 用户可见的 Thought、工具、审批、计划、验证和错误记录可在重载后保留；Thought 内容在历史和重连后仍可展开。
- 会话切换、重连、重复投递和乱序投递不能生成重复或串会话条目。

**Non-Goals:**

- 展示未被标记为用户可见的内部推理、chain-of-thought 或内部调度事件。
- 保证旧会话能恢复从未持久化过的可见信息。
- 在本变更第一阶段实现列表虚拟化、复杂测量缓存或重新设计视觉主题。
- 更改 WebSocket 的鉴权、心跳、命令传输或工具执行语义。

## Decisions

### 1. 服务端在事实产生时写入展示稿

新增会话拥有的 `TranscriptEntry` 记录，而不是在 `session_history` 中从 frame 和
事件猜测展示结构。每个条目包含 `entry_id`、不可变 `ordinal`、`kind`、`state`、
`revision`、`turn_id`/`tool_call_id` 和按类型区分的 payload。

`TranscriptWriter` 是唯一允许创建或修改可见条目的服务端组件。它在用户消息、
第一个 Thought delta、正文 delta、工具状态、审批、计划、验证、错误和最终完成态发生时写入记录。
选择此方案而不是“统一历史 normalizer”，因为同一转换在不同时间读取不完整事件
日志时仍会产生不同结果。

### 2. 可见条目与内部事件严格分层

可见条目包括用户消息、Thought、助手正文、工具活动/结果、审批、任务进度、验证和错误。
由生产者显式标记为用户可见的 Thought 内容写入 `kind=thought` 条目；未标记为可见的
推理、缓存、压缩边界、transport acknowledgement 与内部 delegate 调度不进入展示稿。
渲染器不得通过文本前缀识别 Thought 或其他语义。

一个 Thought 条目在开始时进入 `thinking` 状态，payload 持有累计 `content`、`token_count`
和开始时间；每次内容或计数更新都递增 revision。服务端完整消费原始模型流后，才以最终累计
reasoning、token 计数和耗时一次性把同一条目结算为 `complete`。思考触及配置 token 预算不是
新的 UI 状态，仍显示为 `Thought`；服务端继续读取同一原始流，等待模型发送正文或工具调用。
`complete` 不可迁移回 `thinking`，任何流结束后迟到的 reasoning delta 必须被 Writer 丢弃且不得
增加 revision。快照和补丁都携带这些字段，因此历史和重连后可重建相同的折叠摘要并展开已持久化内容。

工具活动可以在完成后原地更新为 resolved/failed，或生成明确的工具结果条目；两种
行为都必须由固定的 `entry_id` 和 `revision` 表达，不能由 UI 文字推断。

### 3. 一条正文使用一条有版本的助手条目

客户端在发送请求时携带 `client_message_id`，服务端将它确认为用户条目身份。服务端
在首个正文输出时创建由 turn 和 message index 派生的 assistant entry；每个 delta
发送该 entry 的累计内容或有序 patch 和递增 `revision`，final 只把同一 entry 标记为
`complete`。HTTP final response 仅作为命令确认，绝不直接追加或替换 UI 正文。

模型调用在没有 tool calls 时必须产生非空 assistant 正文才可结束为成功 final。provider 持续
收集 `reasoning_content`，直到原始流结束，再检查是否收到 `content` 或 tool calls；达到 thinking
token 预算时不得提前结算 Thought、发送补救或丢弃该原始流。默认 thinking 请求只发送
`enable_thinking: true`，不把 1024 作为服务端强制上限；如明确配置了预算，也只作为模型参数
而非提前终止条件。仅当完整原始流结束后仍同时没有 `content` 和 tool calls，服务端才进行一次
有界补救调用：不提供工具、关闭 thinking，并要求只输出最终用户答复。补救成功时创建或完成
原助手 entry；补救仍为空或失败时创建 typed error entry，并返回错误而不是空 final。前端收到空
HTTP final 时若已接受 assistant 完成 patch，必须立即结束本轮；不得为确认响应另起 60 秒计时。

这取代 `frame_id + loop` 和文本指纹：相同文本的两轮回答仍是两条不同记录，而重复
网络消息因相同 `event_id` 或较旧 revision 被忽略。

### 4. 快照是带游标的原子切点

历史接口返回 `{version, session_id, upto_event_seq, entries}`。服务端在同一个会话
临界区中取得展示稿和该游标，使得所有 `seq <= upto_event_seq` 的可见状态已反映在
快照内。

Godot 端状态机为 `HYDRATING → REPLACE_SNAPSHOT → READY → SUBSCRIBED`。在
`HYDRATING` 中不渲染任何实时 patch；替换完成后才以 `upto_event_seq` 订阅 WebSocket。
出现 `history_gap` 或 `resync_required` 时回到 `HYDRATING`。会话 ID 与 hydration
generation 不匹配的响应或 patch 一律拒绝。

这取代“先启动 socket 再逐块渲染历史”的竞态。选择快照替换而不是合并，因为历史
接口是权威状态，不应在已有 UI 节点之后追加第二份记录。

### 5. 前端 Store 是唯一展示状态源

新增纯数据的 `TranscriptStore` 和 `TranscriptProjector`。Store 按 entry ID 保存、按
ordinal 排序，并维护已接受 event ID、每条目的最新 revision 和当前 session/generation。
Projector 只验证和应用快照/patch。`ChatPanel` 仅协调输入、会话、滚动和用户操作；
渲染器仅从 Store 的 typed entry 构建或更新控件。

现有 Markdown、日志、Thought、工具预览和审批控件可复用为按 `kind` 选择的 renderer，但不得
读取原始 HTTP/WebSocket payload 或解析 `Thought:` 文本。将渲染与状态分开可避免
“修复历史加载”时再次修改实时分支。

### 6. 旧会话只进行一次尽力转换

旧会话首次读取时可从已持久化的 frame 和 history events 做一次兼容转换，并把结果
标记为 `legacy` 后保存为展示稿；之后所有加载都读取保存结果。转换不能确定的内部
或已丢失信息不生成虚假条目。该方案优于每次加载都重新推断，因为它至少保证同一旧
会话后续读取稳定。

## Risks / Trade-offs

- [展示稿与事件游标未原子保存] → 使用同一会话锁/事务生成快照和 `upto_event_seq`，并以回归测试验证边界事件。
- [HTTP final 与 WebSocket final 双渲染] → HTTP 响应不携带渲染动作；无 WebSocket 完成确认时只触发 hydration。
- [客户端乐观用户消息与服务端确认重复] → 使用 `client_message_id` 关联并将确认更新同一条目。
- [长期会话 payload 变大] → 第一阶段限制历史快照窗口但保持完整 Store 协议；虚拟化作为独立后续变更。
- [旧数据缺块] → 明确标注尽力转换，不伪造不可恢复记录。
- [Thought 内容在刷新后丢失或错配] → 将内容、token 计数、开始时间和完成耗时纳入同一持久化条目及 revision 协议。
- [Thought 完成后被迟到 delta 复活] → Writer 强制终态单向迁移，并在 provider 完整响应结算前保留流更新。
- [模型只返回 reasoning 导致空 final] → 用一次无思考、无工具的正文补救请求；失败时明确显示错误，绝不静默成功。
- [渲染器重构扩大范围] → 保留现有视觉组件，只替换输入和状态边界，先验收正确性。

## Migration Plan

1. 为新会话加入展示稿存储、条目 schema、writer 和带游标快照 API。
2. 发布 WebSocket 展示稿 patch，同时保留 transport 的认证、重放、ack 与 gap 行为。
3. 将 Godot 面板切换到 Store/Projector；HTTP final 不再直接渲染。
4. 对旧会话首次加载执行一次兼容转换并持久化结果。
5. 用实时、Thought 展开/截断、空正文补救、重载、重连、间隙和切会话 fixture 验收后移除旧的直接 UI 追加路径。

回滚时将客户端切回旧历史读取和直接渲染；新展示稿记录保留为附加数据，不修改原有
agent frame 或原始事件记录。

## Open Questions

- 工具“运行中”是否在完成后原地更新，还是保留活动条目并追加结果条目；需在实现前固定 UX 规则。
- 历史快照的默认窗口大小与“加载更早记录”分页协议是否需要随本变更一并引入。
