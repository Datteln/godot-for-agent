## Why

时间线工具块里的 "(json)" 代码块渲染的是 LLM 工具调用的**入参**（`tool_preview_renderer.gd _render_json` 渲染 `call.input`），不是工具输出；`read_scene_tree` / `describe_map_context` / `describe_tilemap_selection` 等无参工具入参为 `{}`，块看起来像"输出坏掉的空壳"，误导用户。入参是模型内部调用细节，工具结果正文也从不渲染（仅状态行），裸 JSON 块在时间线里没有信息价值。

## What Changes

- 时间线 `json` 类工具块不再渲染入参 JSON，只保留标题 + 状态行（如 `read_scene_tree · applied`）。
- `list` 类（内部委托 `_render_json`）时间线内同样不铺入参 JSON。
- `diff` / `map` / `run` 三类保留结构化预览（计算后的 diff、地图操作、执行确认信息，非裸入参）。
- 审批/确认弹窗行为不变：仍经 `render_call` 展示完整入参预览。
- 可选：状态行旁增加折叠的 result 摘要入口（查看工具输出）。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `chat-event-streaming`: "Tool previews and all visible nodes use the renderer registry" 增加条款——时间线对 json/list 类工具条目不得展示裸入参 JSON；确认预览可继续展示完整入参。

## Impact

- 前端：`ui/tool_preview_renderer.gd`、`timeline/chat_item_renderer_registry.gd`。
- 时间线视觉噪音下降；copy_text 与历史渲染策略保持一致（live 与 history 共用 registry）。
