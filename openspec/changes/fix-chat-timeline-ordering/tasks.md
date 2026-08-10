## 1. 控制器键空间统一

- [x] 1.1 `chat_timeline_controller.gd`：记录 high_water_seq；insert 首元素非 int 时 stamp 为 `[hw, ++stamp, 原第三元素]`，按 item_id 记忆保持稳定
- [x] 1.2 本地条目 order_key 改为 `[hw, 1_000_000+n, 0]`，废弃 1_000_000_000 偏移
- [x] 1.3 `reset_epoch` 清空 stamp 记忆、reserved stamp 与计数器

## 2. 存储失败闭环

- [x] 2.1 `chat_timeline_store.gd _insert`：order_key 非全 int 以 `mixed_order_key_types` 拒绝

## 3. 乐观用户气泡与对账

- [x] 3.1 `chat_panel.gd _on_send`：发送瞬间渲染本地 provisional 用户气泡并记录 item_id
- [x] 3.2 `chat_panel.gd _handle_event`：`user_submitted` 到达时 `promote_local_to_next_insert` 移除本地条目并保留其键给回声 insert
- [x] 3.3 `_clear_messages` / epoch reset 清理 pending id

## 4. 验证

- [x] 4.1 headless 回归：stamping 顺序（本地用户气泡经对账位于 turn 首位；事件/消息/本地交错正确）；store 拒绝非 int 键
- [ ] 4.2 手动验证：发送瞬间气泡可见且位于 turn 响应之前；停止/无历史等本地通知位置正确