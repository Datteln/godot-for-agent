## Why

`describe_map_region` 处理函数（map_tools.gd 1259-1270 行）手工拼 `{x,y,z,width,height,depth}` 字典传给 `_live_object_occupancy`，缺少 `min_*/max_*` 键；其内部对每个碰撞对象调用 `MapValidator.in_region`，硬下标 `region["min_x"]` 触发 Godot 运行时错误（map_validator.gd:842，场景有多少碰撞对象刷多少次）。更隐蔽的是下标失败返回 null 使 region 退化为零尺寸，`object_occupancy` 永远空数组，LLM 拿到错误事实。同一函数内已有 `region_from_input` 规范化 region 却未复用。

## What Changes

- map_tools.gd 1259 行改传规范化 region（复用该处理函数内已有的 `region_from_input` 结果，或把手工字典过一遍 `MapValidator.region_from_input`）。
- 加固 `MapValidator.in_region` 与 `map_tools.gd _entry_in_region`：不再硬下标 region 键；键缺失时失败闭环（typed invalid-region error），禁止 null 退化为零边界。

## Capabilities

### New Capabilities

（无）

### Modified Capabilities

- `platform-traversal-validation`: "Collision facts are authoritative" 增加 region 规范化与失败闭环要求：占用读取与穿越校验消费的 region 必须经 canonical 构造器产出；缺失边界键必须 typed 报错而非运行时键错误或零尺寸退化。

## Impact

- 前端：`tools/map_tools.gd`（1259 行调用点、`_entry_in_region`）、`tools/map_validator.gd`（`in_region`）。
- 直接影响 `describe_map_region` 返回的 `object_occupancy` 事实质量；消除编辑器 Output 的 min_x 报错刷屏。
