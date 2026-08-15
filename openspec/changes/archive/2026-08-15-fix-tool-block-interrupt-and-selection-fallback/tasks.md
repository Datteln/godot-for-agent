## 1. 中断收尾

- [x] 1.1 `chat_panel.gd _on_interrupt`：中断边界内对当前 turn 非终态工具条目批量 finalize(interrupted)/discard
- [x] 1.2 收尾范围以 turn_id/epoch 过滤，不误伤其他 turn

## 2. 选择回退

- [x] 2.1 `map_tools.gd describe_tilemap_selection`：无选中时回退到首个兼容 TileMapLayer，结果记录 `selection_fallback`
- [x] 2.2 无兼容层时返回 typed unavailable + 封闭恢复动作列表

## 3. 验证

- [x] 3.1 回归测试：中断后时间线不存在 status=pending 的工具块
- [ ] 3.2 回归测试：无选中调用 describe_tilemap_selection 得到回退结果；无兼容层得到 typed unavailable
- [ ] 3.3 手动验证：复现截图场景，停止后 describe_map_region 块不再永久 pending
