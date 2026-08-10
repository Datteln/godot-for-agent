## Context

`ToolPreviewRenderer.render_call` 标题为 `name (render_kind)`，`(json)` 只是 `infer_render_kind` 的兜底标签。`_render_json` 把 `call.input` 序列化进只读 TextEdit；`_render_op_list`（list 类）直接委托 `_render_json`。`ChatItemRendererRegistry._render_tool` 对 result 只渲染状态行（applied/error/pending），结果正文不进时间线。审批弹窗经 `create_tool_preview_node` 复用同一 `render_call`。

## Goals / Non-Goals

**Goals:**
- 时间线不再出现裸入参 JSON 块（json/list 类）；
- 审批预览信息不丢失；
- live 与 history 渲染一致（共用 registry 路径）。

**Non-Goals:**
- 不在本变更实现 result 正文展示（仅作可选入口，另立任务）；
- 不改动 diff/map/run 的结构化预览；
- 不改动 render_kind 推断逻辑。

## Decisions

1. **渲染分域开关**。给 `render_call` 增加展示模式参数（如 `compact := false`）：时间线 `_render_tool` 传 compact=true，json/list 类跳过 `_render_json`；审批路径保持默认完整预览。替代方案"在 registry 里删节点"被否：会破坏审批复用与 copy_text 一致性。
2. **copy_text 语义保持**。工具条目 copy_text 仍包含调用与结果的结构化文本（供复制/诊断），仅视觉隐藏入参 JSON。
3. **可选 result 摘要**。若实现，状态行旁加与 Thought 同款折叠 toggle，内容取 result 的截断摘要（受 MAX_MESSAGE_RENDER_CHARS 约束）；默认折叠。

## Risks / Trade-offs

- [用户偶尔需要看入参] → 审批弹窗与 copy_text 仍可得；可选折叠 result/入参入口可后续补齐。
- [compact 模式被误用于审批路径] → 调用点仅两处（registry 时间线 vs 审批），测试固定两路行为。
