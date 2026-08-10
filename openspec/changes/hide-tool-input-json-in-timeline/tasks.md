## 1. 渲染分域

- [x] 1.1 `tool_preview_renderer.gd render_call` 增加 compact 展示模式：json/list 类在 compact 下不渲染入参 JSON
- [x] 1.2 `chat_item_renderer_registry.gd _render_tool` 时间线路径传 compact=true；审批路径（create_tool_preview_node）保持默认

## 2. 可选 result 摘要

- [x] 2.1 状态行旁增加折叠 toggle，展开显示 result 截断摘要（默认折叠；可标为可选任务）

## 3. 验证

- [x] 3.1 回归测试：json/list 类工具条目时间线仅标题+状态行；diff/map/run 预览不变
- [x] 3.2 回归测试：needs_confirm 审批弹窗仍展示完整入参预览
- [x] 3.3 历史回放渲染与实时一致（无裸入参 JSON 块）
