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

`TranscriptViewport` 是唯一可决定根控件 mount/unmount 的宿主；renderer registry 只提供 `create(entry)`、`update(root, entry)` 和 `reset(root)`。宿主以 `entry_id → Control` 索引当前已挂载根控件。revision 较新的 entry 调用该控件的 `update`；只有初次进入可见窗口时创建。更新前记录并恢复文本选择、展开状态和焦点；无法安全保留的状态必须清除而不能指向旧文本。`reset` 必须断开回调、清空选择、展开状态、可操作审批动作和 entry ID，避免被虚拟列表复用到另一条记录。

### 3. Markdown 与复制使用同一规范文本

Markdown renderer 只使用受支持的 BBCode 子集并转义不支持或畸形语法，不执行链接外的脚本行为。复制操作来自 entry 的规范文本 payload，而不是 RichTextLabel 的 BBCode、节点名称或内部 metadata；这让历史、实时和虚拟化后重新挂载得到相同复制结果。用户/助手 Markdown 的规范复制值是持久化 `payload.text` 的可读文本表示；Thought 则是持久化 `payload.content`，均不受展示截断影响。

### 4. Thought 是持久化的可展开状态卡片

`kind=thought` renderer 在 `thinking` 状态显示 `Thinking {token_count} Tokens >`，在
`complete` 状态显示 `Thought for {duration_seconds}s >`。摘要始终可点击；点击后才显示该条目
持久化的累计 Thought 内容。思考 token 计数到达配置预算不是新的 UI 状态，也不改变完成后的
摘要文案。内容更新与终态都通过同一 entry ID 的 revision 原地更新；终态到 `thinking` 的 patch
即使 revision 更高也必须被拒绝。展开状态是本地视图状态：同一已挂载条目的有效 revision
更新必须保留它；历史加载或重连后的新挂载从持久化 payload 重建内容，但默认折叠。renderer
不得从正文前缀或未声明的推理事件创建 Thought 卡片。Thought 展开内容与普通正文同样复制
（选中文本或右键“复制全文”），不设独立的折叠态复制按钮；复制值必须是该条目持久化的规范
Thought 内容，不包含 `Thinking`/`Thought for` 摘要、token/耗时、折叠符号或 UI metadata。

### 5. 最小单条展示预算

每个 renderer 必须接受统一的可配置初始展示字符预算。任何 kind 的超长持久化内容都完整保留在 Store；Markdown、Thought 展开内容和工具原始详情超过预算时，初次只创建带“内容过长，点击显示完整内容；可复制完整内容”说明的预览。用户明确点击该条目的“显示完整内容”后，renderer 才为该单条目创建完整富文本；这不是截断或额外内容接口。完整节点离开虚拟窗口、会话切换或被 reset 后必须释放，重新挂载回到预览状态。工具摘要保持紧凑，原始详情同样采用此延迟完整渲染规则。

### 6. 工具与审批是状态机卡片

工具卡片默认显示简短动作、目标、状态和摘要，大 payload 折叠。审批控件只在 `pending`（展示稿契约唯一的可操作态）期间由确认宿主提供，其余状态一律不得出现确认控件；用户确认、拒绝或提交结果后，原审批卡片必须被同一 entry ID 的一行普通文本节点替换，不保留卡片边框、按钮或可展开详情。例如：`已确认：修改 res://player.gd`、`已拒绝：删除 res://legacy.gd`。该文本只表达权限操作及其结果：approval payload 必须持久化 `operation_summary`、`affected_paths` 和 `resolution_summary`，并由 renderer 生成单行文本；路径过多时使用稳定的简短列表或数量摘要。用户补充输入不是审批结果，必须作为独立 `kind=user` 条目在其实际提交顺序显示、持久化和重载，绝不写入 approval payload 或合并进权限结果文本。字段客观不存在时显示“未提供”，不得从 UI 或原始传输猜测。错误卡片必须展示操作/任务上下文、用户可读原因和已知修改状态；重试只在 payload 明确声明可重试时出现。

### 7. 临时提示不是展示稿条目

“正在等待模型”“命令执行中”等仅反映本地瞬时状态的提示可以沿用当前视觉样式，但由独立 transient host 创建，不进入 Store、entry ID、ordinal、测量或分页。请求完成、失败、会话切换、水合替换或该提示被覆盖时，host 直接 `queue_free` 该节点；绝不从快照、WebSocket 或 Viewport remount 中重新渲染。服务端持久化的 `kind=error` 与 `kind=progress` 不属于 transient，仍按 typed entry 渲染。

### 8. 实时 Markdown 与 Thought 更新节流但不丢 revision

Store 立即接受 patch；renderer 可在一帧内合并多次文本重绘以减少 RichTextLabel 布局，但最终必须呈现最新 revision，且不得跳过 complete state。该节流只影响绘制，不影响 Store 状态或 WebSocket acknowledgement。

完成修订不改变内容时不得重建富文本控件（避免输出完成瞬间的可见闪烁）：流式更新走增量追加，完整重建只发生在这几种情况——挂载（打开会话、历史水合、滚动加载重挂载）、展示模式切换、内容被替换、或完成时比对发现分块转换与整体转换不一致（分块边界切断了 Markdown 语法）而做的一次性自愈。流式光标必须独立于富文本节点，摘除光标不得触碰正文。

### 9. 瞬时提示保持触发位置

每个 transient notice 都必须锚定到产生它的聊天上下文：例如“无历史记录”位于该会话的空历史位置，“等待模型响应”位于对应乐观用户消息之后。实现不得把所有瞬时提示追加到位于完整 transcript 列表之后的固定 `_notice_list`，否则任意提示都会视觉上落在历史记录末尾。提示继续由 transient host 管理、可在状态结束/水合/切换会话时丢弃，且不写入 Store、ordinal 或历史快照。导航 follow-mode 与滚动策略仍由 `chat-transcript-navigation` 负责，本 change 不修改其语义。

### 10. 历史工具摘要有严格的同步渲染上限

工具的原始 `result_summary` 可以为审计和后续按需查看而持久化，但历史水合只可同步创建紧凑摘要。对 grep 类结果，状态行最多显示固定数量的命中，每个路径和命中文本均须截断，并明确标记剩余命中；不得连接所有 `matches[].text` 后传给 Markdown/RichTextLabel。该规则同时适用于实时和历史 renderer，以保证单个日志行即使包含完整模型请求也不会占用主线程进行巨型布局。

## Risks / Trade-offs

- [renderer 更新重置用户选择] → 用 entry ID 保存选择/展开状态，并为无法恢复的选区显式清除。
- [单条内容过大] → 完整值始终保留在 Store；默认只创建预览，只有用户明确点击才为该条创建完整富文本，离屏即释放完整节点。
- [Thought 内容在重连后不可展开或在预算边界丢失] → 只从持久化 `kind=thought` payload 重建，并用历史/重连/预算边界 fixture 验证。
- [工具 payload 泄漏敏感/冗长参数] → 白名单摘要字段，原始 payload 默认不展示。
- [已解决审批丢失权限上下文或错误吞并用户补充输入] → 持久化审批操作摘要、路径与结果；补充输入始终创建独立 `kind=user` 条目，缺失字段显式标注而不猜测。
- [复用旧组件保留隐式 role/text 依赖] → 为每个 renderer 添加 typed adapter，拒绝缺少 kind 的输入。
- [服务端有流式正文但前端静默丢弃] → 对每个补丁结果写入脱敏结构化诊断，并为 Projector 拒绝提供确定原因。
- [瞬时系统提示脱离触发上下文并总出现在底部] → transient host 按触发点插入提示，或与对应 transient/乐观消息组成同一局部容器；不得保留全局尾置提示列表。
- [历史工具结果含巨型日志命中导致编辑器无响应] → 紧凑摘要限制条目数和每项字符数，原始结果保持持久化但不在水合时同步布局。

## Migration Plan

1. 定义 renderer entry payload 与纯文本复制规则。
2. 为现有 Markdown、Thought、日志、工具预览、确认和错误组件创建 typed adapter。
3. 让 transcript 宿主按 entry ID 管理控件并进行 revision 更新；将已解决审批替换为文本节点。
4. 把 ChatPanel 的瞬时提示迁入可丢弃的 transient host；移除其直接追加持久聊天块、基于文本的 Thought 推断和文本指纹渲染路径。
5. 在 navigation change 接入前后验证 renderer 重挂载和复制保持一致。
6. 让 transient host 按触发点插入系统提示而非统一尾置。
7. 对工具结果的历史紧凑摘要施加明确体积上限，并以含超长日志命中的 grep fixture 验证水合不会创建巨型富文本。
