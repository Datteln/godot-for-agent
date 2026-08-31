## Why

地图代理能够描述 `GridMap` 的三维区域，但无法对该区域执行精确截图；截图工具在 3D 模式只接受整个 `Node3D`。这使代理无法以有限格子范围复核三维地图编辑，也使 2D 与 3D 地图工作流的目标模型不一致。

## What Changes

- 让 `capture_viewport_screenshot` 在 `mode: "3d"` 下接受 `target.type: "map_region"`，并以 `GridMap` 的三维单元格范围自动取景。
- 为三维地图区域定义明确的目标契约：`path`、`x/y/z/width/height/depth`、可选取景边距，以及不适用于 3D 的 `map_layer` 处理规则。
- 统一地图选择与代理指导，使 `TileMapLayer`、旧 `TileMap` 和 `GridMap` 的可发现性及截图前检查一致。
- 扩展 `describe_tilemap_selection`，使其能描述二维和三维地图节点，并为后续区域工具给出维度匹配的参数提示。
- 以机器可读错误和警告替代模糊失败信息，尤其是错误节点类型、缺少三维边界、非法尺寸和空区域。
- 将地图 builder 的即时 Godot 编译校验设为 reload/rebuild 的前置条件；编译失败时返回可定位诊断并要求先修复，不引入跨调用的验证状态门禁。
- 保持现有 2D `map_region` 截图行为及 3D `node_3d` 截图行为兼容。

## Capabilities

### New Capabilities
- `three-dimensional-map-region-capture`: 对 `GridMap` 的有限三维单元格区域进行可恢复相机状态的自动取景与截图。
- `dimension-aware-map-selection-description`: 描述当前选择的 TileMapLayer、旧 TileMap 或 GridMap，并返回地图维度和后续区域工具所需的参数提示。

### Modified Capabilities
- `incremental-map-editing-guidance`: 要求地图代理在二维与三维局部编辑后使用与地图维度匹配的区域检查和证据范围说明。

## Impact

- 影响 `ai_agent_frontend/addons/ai_agent/tools/scene_tools.gd` 的截图目标解析和相机取景。
- 影响工具 schema、工具说明及 map-agent 指导，以公开 3D `map_region` 输入契约。
- 影响 builder 的写后、reload 前和 rebuild 前校验结果协议，使失败能保留路径、行列、错误码与后续修复动作。
- 增加 `GridMap` 区域解析、错误契约和既有 2D/3D 路径兼容性的自动化测试。
