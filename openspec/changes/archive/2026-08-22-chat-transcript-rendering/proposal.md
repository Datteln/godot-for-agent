## Why

权威展示稿解决“显示哪一条”的正确性，但没有定义“如何稳定地显示每一种条目”。
当前 Markdown、复制、工具预览、确认和错误 UI 仍散落在直接追加消息节点的旧面板中；
如果不把它们改为以 transcript entry 为输入，重构后会丢失交互、重复渲染或重新依赖
文本前缀猜测语义。

## What Changes

- **BREAKING** 用按 transcript entry `kind` 选择的渲染器取代直接 role/text 追加和
  `Thought:`、文本指纹等语义推断；`kind=thought` 是显式的可展开条目，而非从文本推断。
- 定义助手和用户 Markdown 的安全可读渲染、流式/完成更新一致性和复制文本语义。
- 定义每条可视内容的最小初始展示预算：任何超长条目完整保留在 Store，默认只创建预览；用户明确点击后才渲染该条完整内容，复制仍取得完整规范内容。
- 定义工具、审批、进度、验证和错误条目的紧凑展示、原地更新和历史只读状态；审批被确认、拒绝或提交结果后降级为一行权限结果文本，用户补充输入仍是独立用户消息块。
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
- 要求 approval 的 typed payload 以向后兼容方式持久化可读操作摘要、受影响路径和权限结果，使其可在历史和重连后显示；用户补充输入通过独立 `kind=user` 条目持久化，本 change 不改变其他聊天或 WebSocket 传输语义。
- 临时“等待/命令执行中”等提示不写入展示稿，也不参与历史重建；其已创建节点可直接丢弃。
- 临时系统提示必须显示在触发它的聊天上下文位置，不能因为独立提示容器固定排在 transcript 之后而一律落到历史记录末尾；提示仍不写入展示稿，也不参与历史重建。
- 历史工具结果的紧凑摘要必须有明确体积上限；不得把日志命中的完整模型请求、源文件或其他巨大原始结果同步交给 `RichTextLabel.fit_content`，以免阻塞编辑器主线程。
