## Context

`MapValidator.region_from_input`（map_validator.gd 770-783 行）是唯一产出 x/y/width/height + min_*/max_* 全键 region 的 canonical 构造器。describe_map_region 处理函数在 1068 行已持有规范化 region，却在 1259 行另拼手工字典传入 `_live_object_occupancy`；后者 2593 行调 `in_region`，841-844 行硬下标 `region["min_x"]`。GDScript 对 Dictionary 缺失键下标会报运行时错误并返回 null，`int(null)=0` 使 region 退化为零尺寸。`_entry_in_region`（map_tools.gd 6305-6319 行）存在同样的硬下标模式。

## Goals / Non-Goals

**Goals:**
- 消除 describe_map_region 的 min_x 运行时报错；
- `object_occupancy` 返回区域内真实碰撞对象；
- region 键缺失时 typed 报错，杜绝 null→0 静默退化。

**Non-Goals:**
- 不重构 region 在 planner/validator 间的整体传递链（`_expanded_transition_region` 等路径已验证安全）；
- 不改变 `describe_map_region` 的对外结果 schema。

## Decisions

1. **调用点复用规范化 region**。1259 行直接传处理函数内 1068 行的 `region` 变量；若 origin 与 input 原点语义不同，则用 `region_from_input` 包装手工字典。理由：最小改动消除 schema 泄漏，且与既有 canonical 构造器一致。
2. **边界读取失败闭环**。`in_region` / `_entry_in_region` 改为先校验六个边界键齐全（或统一经 `region_from_input` 归一），缺失即返回 typed error；不采用 `.get()` 默认值兜底，避免把坏 region 当成合法零区域。
3. **in_region 签名兼容**。保持 `static func in_region(coords, region) -> bool` 对外行为；键缺失的失败闭环以断言 + 返回 false + 日志表达，或新增 `region_error(region) -> Dictionary` 供入口预检（实现时二选一，倾向入口预检）。

## Risks / Trade-offs

- [入口预检遗漏个别调用点] → 全仓 rg `in_region(` / `_entry_in_region(` 调用点清单核对；`_compare` 测试覆盖主要工具路径。
- [断言在 release 构建被剥离] → 选择入口预检 typed error 方案时不依赖断言；若用断言仅作调试期辅助。
