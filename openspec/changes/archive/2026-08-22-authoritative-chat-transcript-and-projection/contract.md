# 权威聊天展示稿契约（Transcript Contract v1）

本文件固定 `authoritative-chat-transcript-and-projection` 变更的线上契约，
同时约束三处消费方：HTTP 历史快照、HTTP 命令确认响应、WebSocket 实时补丁。
契约版本 `version = 1`；任何不兼容调整必须递增版本并更新本文件。

## 1. 条目（TranscriptEntry）

每个用户可见条目是一条带稳定身份的记录：

```json
{
  "entry_id": "e12",
  "ordinal": 11,
  "kind": "assistant",
  "state": "streaming",
  "revision": 4,
  "turn_id": "t3",
  "tool_call_id": null,
  "payload": { "text": "..." }
}
```

| 字段 | 语义 |
|------|------|
| `entry_id` | 会话内唯一的稳定身份（`e<N>`），一旦分配永不改变、永不复用。 |
| `ordinal` | 不可变的展示顺序，等于条目创建顺序；任何更新不得改变它。 |
| `kind` | 条目类型，见下表。渲染器只按 `kind` + typed payload 选择渲染方式。 |
| `state` | 条目状态，随 `kind` 取值，见下表。 |
| `revision` | 单调解码器：条目每次被更新（含创建）递增 1；创建时为 1。客户端必须拒绝 `revision` 不高于已接受值的补丁。 |
| `turn_id` | 产生该条目的轮次 id（可空）。 |
| `tool_call_id` | 关联的工具调用 id（工具/审批类条目必填，其余为 null）。 |
| `payload` | 按 `kind` 区分 schema 的内容载荷；补丁携带该条目的**完整** payload（累计式，不做增量拼接）。 |

### kind 与 state 取值

| kind | 含义 | 合法 state 迁移 | payload 关键字段 |
|------|------|-----------------|------------------|
| `user` | 用户消息 | `complete`（终态） | `text`，`client_message_id`，`has_context` |
| `assistant` | 助手正文（流式与完成态共用同一条目） | `streaming` → `complete` | `text`（累计正文） |
| `thought` | 用户可见 Thought（生产者显式标记可见的推理流） | `thinking` → `complete` | `content`（累计内容），`token_count`，`started_at`，`duration_seconds`（完成后） |
| `tool_activity` | 工具活动/结果（服务端工具、前端静默工具） | `running` → `resolved` \| `failed` | `tool`，`args`，`is_error`，`result_summary`，`result_count`，`render_kind` |
| `approval` | 需确认的前端工具调用及其决定 | `pending` → `approved` \| `rejected` | `tool`，`args`，`decision`，`render_kind` |
| `plan` | 已创建的计划 | `complete`（终态） | `summary`，`steps`（`[{title, status}]`） |
| `progress` | 任务进度（计划步骤迁移） | `running` → `complete` | `step_index`，`total_steps`，`title`，`summary` |
| `verification` | 文件改动校验 | `running` → `passed` \| `failed` | `tool_use_id`，`file_path`，`phase`，`issues_count`，`summary` |
| `error` | 错误记录 | `complete`（终态） | `text` |
| `system` | 系统通知文本（仅旧会话兼容转换产生） | `complete`（终态） | `text` |
| `log` | 通用工具输出文本（仅旧会话兼容转换产生） | `complete`（终态） | `text`，`marker`，`indent` |

`system`/`log` 只允许由旧会话一次性兼容转换产生，新会话的实时写入不得使用。

### 不进入展示稿的内容

未被生产者显式标记为用户可见的推理、缓存/压缩边界、transport 确认、内部
delegate 调度事件一律不得成为展示稿条目。助手正文条目在**产生时**剥离
`Thought:` 前缀行（一次性规则，持久化后不再依赖文本推断）；该前缀不构成
Thought 条目。

### Thought 条目细则

- 身份：与同一条助手响应相同，派生自 `(frame_id, message_index)`；同一次思考
  的内容增量与 token 计数更新同一条目（revision 递增），不新建条目。
- 开始：首个可见推理增量到达时进入 `thinking`，payload 记录 `started_at` 与
  当前 `token_count`。
- 完成：结算推迟到**原始模型响应流被完整消费**时（`assistant_stream_end`），
  不在首个正文增量或工具边界提前结算；完成时写入规范
  `duration_seconds`（= 完成时刻 − `started_at`）。思考预算边界之后继续产出
  的正文/工具调用照常接受，已累计推理内容完整保留。
- **`complete` 是单向的**：完成后的迟到推理增量不得把条目退回 `thinking`；
  仅当迟到增量携带更长内容时，保留更完整的推理（revision 递增、状态不变、
  耗时不变）。
- 渲染头：思考中显示 `Thinking {token_count} Tokens`；完成后显示
  `Thought for {duration_seconds}s`；`content` 可展开，且折叠状态在修订更新间保持。

### 空正文与最终完成边界

- 无工具且正文为空的模型响应**不得**作为成功的最终完成下发：不产生空的
  助手完成条目，也没有空文本的 `final` 确认。
- 原始流结束后执行**恰好一次**挽救调用：关闭 thinking、不提供工具，仅要求
  基于当前上下文给出最终正文。挽救出的正文沿用原响应身份产生助手条目。
- 挽救仍为空时落为 `error` 条目并返回错误响应。
- 客户端收到空文本的 HTTP final 确认时：若已接受有效的助手完成补丁则直接
  终结本轮；否则立即重新水合对账，不使用超时等待。

## 2. 快照（TranscriptSnapshot）

历史接口 `GET /sessions/{id}/history` 在响应中返回原子快照：

```json
{
  "transcript": {
    "version": 1,
    "session_id": "default",
    "upto_event_seq": 42,
    "legacy": false,
    "entries": [ { "entry_id": "e1", "ordinal": 0, "...": "..." } ]
  }
}
```

- 快照在与会话写入相同的临界区内读取：所有 `seq <= upto_event_seq` 的可见
  展示稿状态必须已反映在 `entries` 中。
- `upto_event_seq` 与 WebSocket 订阅的 `after_seq` 使用同一序号空间；客户端
  以 `after_seq = upto_event_seq` 订阅，即可无重复、无遗漏地续收补丁。
- `legacy` 为 true 表示该快照来自旧会话的一次性兼容转换。
- `entries` 按 `ordinal` 升序排列。
- 快照替换语义：客户端收到快照时整体替换当前会话展示态，不做合并。

## 3. 补丁（Transcript Patch）

每一个用户可见变化通过既有 WebSocket 事件包络发布一条
`type = "transcript_patch"` 的不可变事件：

```json
{
  "version": 1,
  "type": "event",
  "event": {
    "event_id": "default:43",
    "session_id": "default",
    "seq": 43,
    "type": "transcript_patch",
    "payload": {
      "entry": { "entry_id": "e12", "ordinal": 11, "kind": "assistant",
                 "state": "complete", "revision": 7, "payload": { "text": "..." } },
      "stream_key": "e12"
    }
  }
}
```

- `payload.entry` 是目标条目的完整最新状态（累计式）；重复投递幂等。
- `payload.stream_key` 供传输层对同一 `entry_id` 的高频补丁做限速合并，
  语义与 `agent_text_delta` 的 `(frame_id, loop)` 分段一致。
- 事件包络的 `event_id`/`seq` 不可变语义、确认（ack）、保留窗口、
  `history_gap`/`resync_required` 行为全部沿用既有传输契约。
- 客户端必须：按 `event_id` 去重；按 `revision` 拒绝过期补丁；最终助手正文
  只能通过该补丁（或快照）呈现，不得从 HTTP 命令响应直接渲染。

## 4. `client_message_id`

- 客户端提交用户消息时在 `POST /chat` 携带 `client_message_id`（会话内唯一的客户端本地 id）。
- 服务端把它写入对应 `user` 条目的 `payload.client_message_id`，作为该用户条目的确认身份。
- 客户端乐观用户条目仅通过 `client_message_id` 与服务端条目对账；快照替换时
  只保留能按 `client_message_id` 匹配到服务端条目的乐观条目（匹配成功即以服务端条目为准）。
- 服务端未收到该字段时为条目生成空值，不影响条目创建。

## 5. 工具完成规则（任务 1.3 决定）

**原地更新（in-place）**：一个工具调用对应一条 `tool_activity` 条目，
开始时 `state = "running"`，完成时同一条目迁移到 `resolved`/`failed`
并携带结果摘要，`revision` 递增；**不**追加独立的工具结果条目。

理由：

- 一次调用与一条可见条目一一对应，顺序与身份都稳定；
- 与既有“单节点工具预览原地更新状态”的 UI 形态一致；
- 避免活动/结果两条记录被快照窗口或重连重放拆散后产生孤儿条目。

需确认的前端工具不使用 `tool_activity`，而由 `approval` 条目独占表达：
`pending`（随 tool_calls 响应创建）→ `approved`/`rejected`（随结果回传更新），
同样是原地更新、同一 `entry_id`。

校验条目同理：`verification` 以 `(tool_use_id, phase)` 为身份原地更新。

> 预览保真度说明：`tool_activity.payload.args` 保存工具入参（含 diff 所需的
> `old_string`/`new_string`），实时轮次在执行前预渲染 diff 预览并随条目复用；
> 历史重载时 `before_text` 可能已被改写，渲染器只依据入参尽力重建预览，
> 无法恢复时仅显示标题与结果状态行，不伪造 diff。

## 6. HTTP 命令响应边界

- `final` 响应只是命令确认：客户端不得用其文本追加或替换助手条目；
  完成态必须经由 `transcript_patch`（或随后的快照）到达。
- 若与某次 `final` 对应的完成补丁始终无法被实时接受（如连接已断），
  客户端必须回退到重新水合历史快照，而不是直接渲染 HTTP 文本。
- `tool_calls` 响应仍驱动确认交互（命令通道），但可见记录由 `approval` 条目表达。
