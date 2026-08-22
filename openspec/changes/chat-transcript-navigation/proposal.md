## Why

长任务会产生大量 Markdown、代码、工具与进度条目。若 Godot 为整段历史持续保留
所有 `Control` 和 `RichTextLabel`，布局、重绘和节点数量会持续增长，最终可能造成
编辑器卡顿甚至崩溃。

需要为权威展示稿增加虚拟滚动和可恢复导航，使完整历史仍可访问，但 UI 同时只挂载
视口附近的有限条目。

## What Changes

- **BREAKING** 将聊天记录容器改为由 transcript Store 驱动的窗口化虚拟列表，而非
  为所有历史条目永久保留 Godot 控件。
- 定义渲染窗口、节点回收、条目测量缓存、内容改变后的视口锚点恢复和严格资源上限。
- 定义 follow mode、用户上滑时停止自动滚动、回到最新按钮和流式更新时的尾部行为。
- 定义加载更早记录、快照分页与虚拟窗口协作方式，确保离屏条目仍可按 ID 找回并复制。

## Capabilities

### New Capabilities

- `chat-transcript-navigation`: 面向长会话展示稿的虚拟滚动、锚点恢复、分页与 follow 导航。

### Modified Capabilities

<!-- None. -->

## Impact

- 依赖 `authoritative-chat-transcript-and-projection` 的稳定 entry ID、ordinal 和 Store。
- 影响 Godot 聊天视图容器、条目 renderer 生命周期、滚动/测量逻辑和长会话测试。
- 不改变可见聊天条目语义或 WebSocket 传输；为历史展示稿读取增加向前分页参数与游标响应。
