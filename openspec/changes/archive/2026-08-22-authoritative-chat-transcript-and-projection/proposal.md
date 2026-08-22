## Why

聊天面板目前分别从 HTTP 最终响应、实时 WebSocket 事件和重新推断的历史
`blocks` 直接创建 UI 节点。这三条路径没有共享的条目身份和顺序，因此会出现
重复正文、Thought 状态错乱、切换会话后串入旧事件，以及历史加载时遗失工具、审批、
计划、验证或错误块。

需要把“用户应该看见的聊天记录”变成一份在服务端产生时就持久化的权威展示稿；
历史加载和实时投递都只投影这一份展示稿，而不再各自重建或猜测 UI 语义。

## What Changes

- **BREAKING** 以版本化、服务端持久化的聊天展示稿取代由 frame、事件日志和
  文本前缀推断出的历史 `items`/`blocks` 展示语义。
- 在服务端为每个可见条目分配稳定身份、顺序、状态和单调修订版本；正文流和最终
  完成态更新同一条目，工具/审批/进度/验证/错误同样拥有明确条目类型。
- 将用户可见的 Thought 作为独立、可持久化且可展开的展示稿条目；传输重放和内部调度事件仍不进入展示稿。
- 使 Thought 的完成态不可回退；无论思考是否触及 token 预算，只要原始响应流结束就以 `Thought` 结束展示。
- 禁止以空 assistant 正文结束成功轮次：必须先完整消费原始模型流，等待其给出正文或工具调用；仅在流结束后两者皆无时，才进行一次关闭思考的正文补救请求，仍为空则产出明确错误记录。
- 让 HTTP 历史接口返回带事件游标的原子展示稿快照，让 WebSocket 发送可幂等应用
  的展示稿补丁；HTTP 命令响应不再作为第二套正文渲染来源。
- 重建 Godot 端展示稿 Store、投影器与渲染边界；Thought 在思考中显示 token 计数，完成后显示耗时并可展开内容；聊天面板只协调输入、会话和视图，
  不再直接从多种协议消息追加聊天节点。
- 为旧会话建立一次性的、尽力而为的兼容转换；不能可靠恢复的历史内部信息不猜测。

## Capabilities

### New Capabilities

- `authoritative-chat-transcript`: 服务端创建、持久化和提供具有稳定身份的可见聊天展示稿快照与补丁。
- `chat-transcript-projection`: Godot 端对展示稿快照/补丁进行原子水合、去重、更新和按类型渲染。

### Modified Capabilities

- `chat-event-websocket`: WebSocket 事件负载增加展示稿补丁契约，并规定最终正文只能通过该条目更新呈现。
- `chat-event-resume`: 恢复流程在展示稿快照原子替换完成后才订阅/接受实时补丁，并以快照游标恢复。

## Impact

- 后端涉及 `ai_agent_service` 的会话持久化、QueryEngine、历史 API schema 和
  WebSocket 事件发布协议。
- 前端涉及 `ai_agent_frontend/addons/ai_agent/ui/chat_panel.gd`、事件 socket 客户端、
  新的展示稿状态/投影/渲染模块和回归测试。
- 现有 HTTP 命令与 WebSocket 连接方式继续保留；历史响应和实时事件 payload 将改为
  新展示稿契约，旧会话仅通过隔离的兼容转换读取。
