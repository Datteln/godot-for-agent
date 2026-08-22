## Context

`authoritative-chat-transcript-and-projection` 提供稳定 entry ID、kind、state、revision 和 payload，但不应把视觉和交互细节放进 Store。现有 Godot 组件已经能渲染 Markdown、日志、工具预览和内联确认，却仍以直接追加节点和文本推断为入口。

本 change 将这些组件收敛为只消费 `TranscriptEntry` 的 renderer。它必须在 revision 更新、虚拟列表复用和历史只读场景下保持同一语义。

## Goals / Non-Goals

**Goals:**

- 每个 entry kind 有明确、可测试的 renderer 和复制表示。
- 同一 entry ID 的更新原地修改控件，不增加第二个聊天块。
- Markdown 对实时、完成态和历史快照显示一致，且异常输入保持可读安全。
- Thought、工具、审批、进度、验证和错误具有能重载的明确状态与交互限制。

**Non-Goals:**

- 定义条目身份、patch 或历史水合；这些属于权威展示稿 change。
- 实现虚拟列表、测量、锚点或分页；这些属于 navigation change。
- 从文本或原始传输消息推断 Thought；只有权威展示稿明确标记为 `kind=thought` 的用户可见内容可以渲染。

## Decisions

### 1. Renderer registry 只按 kind 分发

registry 接收 `TranscriptEntry` 和只读渲染 context，选择 user、assistant、thought、tool、approval、progress、verification 或 error renderer。任何 renderer 都不得读取 WebSocket 原包、HTTP 响应或检查正文前缀。重复判定、状态和顺序永远归 Store，而非 UI。

### 2. 控件以 entry ID 身份化并原地更新

宿主以 `entry_id → Control` 索引已挂载根控件。revision 较新的 entry 调用该控件的 `update(entry)`；只有初次进入可见窗口时创建。更新前记录并恢复文本选择、展开状态和焦点；无法安全保留的状态必须清除而不能指向旧文本。

### 3. Markdown 与复制使用同一规范文本

Markdown renderer 只使用受支持的 BBCode 子集并转义不支持或畸形语法，不执行链接外的脚本行为。复制操作来自 entry 的规范文本 payload，而不是 RichTextLabel 的 BBCode、节点名称或内部 metadata；这让历史、实时和虚拟化后重新挂载得到相同复制结果。

### 4. Thought 是持久化的可展开状态卡片

`kind=thought` renderer 在 `thinking` 状态显示 `Thinking {token_count} Tokens >`，在
`complete` 状态显示 `Thought for {duration_seconds}s >`。摘要始终可点击；点击后才显示该条目
持久化的累计 Thought 内容。思考 token 计数到达配置预算不是新的 UI 状态，也不改变完成后的
摘要文案。内容更新与终态都通过同一 entry ID 的 revision 原地更新；终态到 `thinking` 的 patch
即使 revision 更高也必须被拒绝。展开状态是本地视图状态：同一已挂载条目的有效 revision
更新必须保留它；历史加载或重连后的新挂载从持久化 payload 重建内容，但默认折叠。renderer
不得从正文前缀或未声明的推理事件创建 Thought 卡片。Thought 卡片无论折叠或展开均提供复制
操作；复制值必须是该条目持久化的规范 Thought 内容，不包含 `Thinking`/`Thought for` 摘要、
token/耗时、折叠符号或 UI metadata。

### 5. 工具与审批是状态机卡片

工具卡片默认显示简短动作、目标、状态和摘要，大 payload 折叠。审批按钮只有 `actionable` entry 可用；一旦 accepted/rejected/resolved，原根控件变为只读历史记录。错误卡片必须展示操作/任务上下文、用户可读原因和已知修改状态；重试只在 payload 明确声明可重试时出现。

### 6. 实时 Markdown 与 Thought 更新节流但不丢 revision

Store 立即接受 patch；renderer 可在一帧内合并多次文本重绘以减少 RichTextLabel 布局，但最终必须呈现最新 revision，且不得跳过 complete state。该节流只影响绘制，不影响 Store 状态或 WebSocket acknowledgement。

## Risks / Trade-offs

- [renderer 更新重置用户选择] → 用 entry ID 保存选择/展开状态，并为无法恢复的选区显式清除。
- [Markdown 解析耗时] → 合并同帧更新、限制单次可渲染 payload，并保留纯文本降级。
- [Thought 内容在重连后不可展开或在预算边界丢失] → 只从持久化 `kind=thought` payload 重建，并用历史/重连/预算边界 fixture 验证。
- [工具 payload 泄漏敏感/冗长参数] → 白名单摘要字段，原始 payload 默认不展示。
- [复用旧组件保留隐式 role/text 依赖] → 为每个 renderer 添加 typed adapter，拒绝缺少 kind 的输入。

## Migration Plan

1. 定义 renderer entry payload 与纯文本复制规则。
2. 为现有 Markdown、Thought、日志、工具预览、确认和错误组件创建 typed adapter。
3. 让 transcript 宿主按 entry ID 管理控件并进行 revision 更新。
4. 移除 ChatPanel 的直接 append、基于文本的 Thought 推断和文本指纹渲染路径。
5. 在 navigation change 接入前后验证 renderer 重挂载和复制保持一致。
