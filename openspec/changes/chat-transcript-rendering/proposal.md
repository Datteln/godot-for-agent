## Why

权威展示稿解决“显示哪一条”的正确性，但没有定义“如何稳定地显示每一种条目”。
当前 Markdown、复制、工具预览、确认和错误 UI 仍散落在直接追加消息节点的旧面板中；
如果不把它们改为以 transcript entry 为输入，重构后会丢失交互、重复渲染或重新依赖
文本前缀猜测语义。

## What Changes

- **BREAKING** 用按 transcript entry `kind` 选择的渲染器取代直接 role/text 追加和
  `Thought:`、文本指纹等语义推断；`kind=thought` 是显式的可展开条目，而非从文本推断。
- 定义助手和用户 Markdown 的安全可读渲染、流式/完成更新一致性和复制文本语义。
- 定义工具、审批、进度、验证和错误条目的紧凑展示、原地更新和历史只读状态。
- 定义 Thought 条目的实时 token 摘要、完成耗时摘要、持久化内容的按需展开和规范内容复制。
- 规定同一 entry ID 的 revision 更新必须更新现有控件，不得额外插入聊天块或丢失
  用户的复制/展开上下文。

## Capabilities

### New Capabilities

- `chat-transcript-rendering`: 基于权威聊天展示稿的类型化、可复制、可更新且安全的消息渲染。

### Modified Capabilities

<!-- None. -->

## Impact

- 依赖 `authoritative-chat-transcript-and-projection` 提供的 Store 和 entry contract。
- 影响 Godot 聊天面板、Markdown/Thought renderer、日志/工具预览、内联确认和错误展示组件。
- 不改变后端聊天、WebSocket 或历史 API 协议；本 change 只消费既有展示稿条目。
