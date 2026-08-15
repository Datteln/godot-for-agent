## Context

canonical Timeline 由 `ChatTimelineProjector`（纯投影）+ `ChatTimelineStore`（按 order_key 全局排序）+ `ChatTimelineController`（本地条目与入口）构成。原缺陷：三种条目使用互不兼容的 order_key（事件类 `[seq:int,…]`、消息/工具类 `[frame_id:str,…]`、本地类 `[1_000_000_000+n:int,0,0]`），`_compare_order` 对 int/str 混合默默退化为字符串比较，排序按键空间分组；用户气泡完全依赖服务端 `user_submitted` 回声，发送瞬间无显示。

实现期发现的关键约束：流式 delta 对同一 item_id 反复发 insert（幂等去重依赖 order_key 稳定），因此**不能**简单把消息键换成每事件 seq（每次 delta 的 seq 不同会触发 ambiguous_item_identity 拒绝）。故采用"控制器 stamping"方案，投影器保持纯函数不变。

## Goals / Non-Goals

**Goals:**
- 发送瞬间可见用户气泡，且位于该 turn 所有响应之前；
- 所有条目按真实时间交错排列（实时与历史回放一致）；
- order_key 单一整数空间，混合类型失败闭环。

**Non-Goals:**
- 不重构 VirtualScroller 与历史分页；
- 不移动服务端 `user_submitted` 发布点（stamping + 乐观气泡已使回声到达时序无关紧要，避免服务端手术风险）；
- 不改变 HTTP 响应仅作为 command acknowledgement 的约定。

## Decisions

1. **乐观本地用户气泡 + 回声一次性对账**。`_on_send` 立即 `present_local_text("user", …)` 记录 item_id；`user_submitted` 到达时经 `promote_local_to_next_insert` 移除本地条目并把其 order_key 保留为 `_reserved_stamp`，使紧随其后的回声 insert 继承发送时刻的位置。回声缺失时本地气泡保留，历史恢复兜底。
2. **控制器 stamping 统一键空间**。`present_event` 记录 `high_water_seq`；对投影出的 insert，若 order_key 首元素非 int（消息/工具类的字符串 frame 键），替换为 `[high_water_seq, ++stamp_counter, 原第三元素]` 并以 item_id 记忆保持稳定（后续同 id 的重复 insert 复用同一键）。历史回放同路径 stamp，按到达序产生一致相对顺序。
3. **本地条目键改为 `[high_water_seq, 1_000_000+n, 0]`**。排在所有已接受事件之后、未来事件之前；废弃 `1_000_000_000` 固定偏移。
4. **store 强制整数键**。`_insert` 校验 order_key 全元素为 int，否则以 `mixed_order_key_types` 拒绝（失败闭环）；`_compare_order` 不再需要跨类型退化。

## Risks / Trade-offs

- [stamping 依赖到达序] → 实时与历史都按"首事件到达序"stamp，二者一致；WebSocket 有序投递保证实时序正确。
- [同 batch 内 stamped 第二元素与事件 message_index 小值交错] → 语义为"同一 seq 批次内"，可接受；本地条目用 1_000_000 段隔离。
- [promote 后回声 insert 失败] → reserved stamp 残留最多影响下一个 insert 的位置一次；epoch reset 清空。
- [投影器仍产出字符串键] → 控制器是唯一入口，stamping 全覆盖；store 整数校验兜底。