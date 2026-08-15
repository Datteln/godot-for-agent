## Why

两个中断/可用性缺陷：(1) 用户中断（停止）后，未完成的工具块（如 describe_map_region）永远停在 `pending` 状态——interrupt 流程没有把 provisional 工具条目收尾（finalize/discard），时间线残留"僵尸块"；(2) `describe_tilemap_selection` 在编辑器没有选中 TileMapLayer 时硬失败 "Select a TileMapLayer first"（前端日志 WARN front_tool_failed），产生红色 error 块并浪费一次模型循环，而该场景完全可以确定性地回退到主/首个兼容 TileMapLayer。

## What Changes

- interrupt/cancel 边界必须收尾所有非终态的 provisional 工具条目：标记为 interrupted（finalize with status）或 discard，禁止任何条目永久停留 `pending`。
- `describe_tilemap_selection` 在无选中时确定性回退到主/首个兼容 TileMapLayer，并在结果中记录回退事实；若不存在任何兼容层，返回 typed unavailable 结果与封闭恢复动作（而非裸错误）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `chat-event-streaming`: "Provisional previews have an explicit lifecycle" 扩展——中断/取消边界同样必须收尾 pending 工具条目。
- `verification-outcomes`: "Unavailable verification supplies closed recovery actions" 扩展——依赖编辑器选中的地图读取在无选中时确定性回退或返回 typed unavailable + 封闭恢复动作。

## Impact

- 前端：`ui/chat_panel.gd`（_on_interrupt 收尾路径）、`timeline/chat_timeline_store.gd`（discard/finalize 既有 mutation 复用）、`tools/map_tools.gd`（describe_tilemap_selection 回退逻辑）。
- 用户体验：停止后时间线状态干净；无选中时模型拿到可用事实而非错误。
