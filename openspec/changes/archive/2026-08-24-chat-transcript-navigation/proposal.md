## Why

长任务会产生大量 Markdown、代码、工具与进度条目。若 Godot 为整段历史持续保留
所有 `Control` 和 `RichTextLabel`，布局、重绘和节点数量会持续增长，最终可能造成
编辑器卡顿甚至崩溃。

需要为权威展示稿增加虚拟滚动和可恢复导航，使完整历史仍可访问，但 UI 同时只挂载
视口附近的有限条目。

## What Changes

- **BREAKING** 将聊天记录容器改为由 transcript Store 驱动的窗口化虚拟列表，而非
  为所有历史条目永久保留 Godot 控件。
- 定义渲染窗口、节点回收、条目测量缓存、内容改变后的视口锚点恢复，以及挂载节点和初始预览富文本的严格资源上限；用户主动显示完整单条内容属于受控的按需渲染。
- 定义 follow mode、用户上滑时停止自动滚动、回到最新按钮和流式更新时的尾部行为。
- 定义加载更早记录、带事件水位的分页快照与虚拟窗口协作方式，确保离屏条目仍可按 ID 找回并复制。
- 为实时 `transcript_patch` 在水合、页面合并和窗口切换期间的拒绝结果增加脱敏结构化诊断，避免服务端已生成正文却只能从后端日志反推前端未显示的原因。
- 定义内联工具确认框向 `approval`/`tool_activity` 展示稿条目移交执行前预览控件的所有权规则，避免确认框销毁后缓存或 renderer 再访问已释放的 Godot 实例。
- 明确区分用户拒绝与用户已允许后的工具执行失败；确认 UI 和 durable transcript 必须保留真实结果语义，不能把参数或运行错误显示成“已拒绝”。

## Capabilities

### New Capabilities

- `chat-transcript-navigation`: 面向长会话展示稿的虚拟滚动、锚点恢复、分页与 follow 导航。

### Modified Capabilities

<!-- None. -->

## Impact

- 依赖 `authoritative-chat-transcript-and-projection` 的稳定 entry ID、ordinal 和 Store。
- 影响 Godot 聊天视图容器、条目 renderer 生命周期、滚动/测量逻辑和长会话测试。
- 不改变可见聊天条目语义或 WebSocket 传输；为历史展示稿读取增加向前分页参数与游标响应。
- 瞬时等待/执行提示不属于虚拟展示稿窗口，创建后可被直接丢弃且不参与重新渲染。
- 影响内联确认 UI、预览缓存和 approval/tool activity renderer 之间的控件所有权交接与回收。
- 影响工具确认后的状态文案、approval/tool activity 展示以及结果诊断。
