## Context

interrupt 流程（chat_panel.gd `_on_interrupt`，1336-1351 行）目前只发 `/chat/interrupt`、切状态、呈现"已停止"本地通知，不触碰 timeline 中非终态的工具条目；截图实证 describe_map_region 块在中断后永久 `pending`。`describe_tilemap_selection` 的 "Select a TileMapLayer first" 为 front_tool_failed 裸错误（map_tools.gd 工具实现），map-agent 在无选中场景下必然踩中。

## Goals / Non-Goals

**Goals:**
- 中断后时间线无永久 pending 的工具块；
- 无选中时 describe_tilemap_selection 给出可用事实（回退）或 typed unavailable + 封闭恢复动作。

**Non-Goals:**
- 不 redesign interrupt 的服务端语义（`/chat/interrupt` 协议不变）；
- 不改变 needs_confirm 审批中取消的既有行为（已有 discard 路径）；
- 不扩展到其他选择依赖工具（本变更只覆盖 describe_tilemap_selection）。

## Decisions

1. **中断边界批量收尾**。`_on_interrupt` 在呈现停止通知前，遍历当前 turn 的 provisional 工具条目（status 非终态），经既有 store mutation 标记 `interrupted`（finalize）或 discard；优先 finalize-with-status，保留可见痕迹（用户能看到"被停止的工具"），与"清晰失效"的 spec 语义一致。
2. **回退目标确定性**。describe_tilemap_selection 无选中时按"edited scene 中首个 TileMapLayer 节点（深度优先）"回退，结果附 `selection_fallback: {target, reason}`；无兼容层时返回 `{ok:false, error_code:"no_compatible_layer", recovery_actions:[...]}` 封闭列表。
3. **不引入新 mutation 类型**。复用 store 既有 finalize/patch（status）mutation，避免存储层改动扩散。

## Risks / Trade-offs

- [中断收尾误伤下一 turn 的条目] → 以 turn_id/epoch 过滤收尾范围；测试覆盖跨 turn 中断。
- [回退目标与用户意图不符] → 结果显式记录 fallback，模型可据此改用 describe_map_region 指定目标。
