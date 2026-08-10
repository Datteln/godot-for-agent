## 1. 调用点修复

- [x] 1.1 map_tools.gd 1259 行：`_live_object_occupancy` 改传规范化 region（复用 1068 行变量或经 `region_from_input` 包装）
- [x] 1.2 核对 map_tools.gd / map_validator.gd 全部 `in_region(`、`_entry_in_region(` 调用点的 region 来源，确认均经 canonical 构造器

## 2. 失败闭环加固

- [x] 2.1 `map_validator.gd in_region`：边界键缺失时失败闭环（typed invalid-region），不再硬下标
- [x] 2.2 `map_tools.gd _entry_in_region`：同样消除硬下标

## 3. 验证

- [x] 3.1 回归测试：describe_map_region 对含碰撞对象的场景返回非空且正确的 `object_occupancy`
- [x] 3.2 回归测试：构造缺失 min_x 的 region 调用校验入口，返回 typed error 而非运行时键错误
- [ ] 3.3 手动验证：编辑器 Output 不再出现 map_validator.gd:842 min_x 报错
