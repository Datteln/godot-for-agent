## Why

现有执行路径将项目写入、在线 Godot Editor 操作和前端工具结果转发混在一起，导致自动化修改难以隔离、验证语义不一致，并可能覆盖用户在 Editor 中的内存修改。现在需要将执行层收敛为以项目文件为唯一持久化源的 CodeAct 循环，使各执行 agent 能安全地读、改、测、查看 diff 并基于结果继续修复。

## What Changes

- 新增 Windows 后端 Execution Gateway，统一调度项目读写、受限 worker 命令、Git 只读信息和按需在线 Editor 验证。
- 新增每任务复用的 WSL2 rootless Docker worker：仅向其挂载经解析且允许的项目根、任务临时目录和隔离的 `.godot` 缓存，并默认禁用网络、凭据与宿主访问。
- 新增 `project.read/search/edit`、`shell.run`、`godot.headless`、`git.status/diff`、`skill.load/tool.search` 的统一工具协议，令所有持久化项目写入经 worker 文件路径完成。
- 新增基于内容摘要的工作区基线与冲突检测、实际 diff 归属、目标化验证、失败后修复循环及按任务关联的审计时间线。
- 新增只读/验证用途的 EditorPlugin 本机 RPC；Editor 不再向模型暴露场景、资源或地图写入以及任意 GDScript 执行能力。
- **BREAKING** 废弃由前端转发的在线 Editor 写工具、任意宿主 Shell 与宿主 Editor GDScript 执行通道；普通文件写入不再走 proposal/approval/commit 事务。
- **BREAKING** 地图写入改为 worker 文件修改后的范围/语义验证；验证耗尽、取消或无法修复时以 `failed_validation` 结束并保留可见 diff，而非自动回滚写入。

## Capabilities

### New Capabilities

- `codeact-execution-gateway`: 为 agent 提供统一、带权限、超时和类型化结果的 CodeAct 工具调度。
- `isolated-worker-execution`: 在任务专属的无网络 Docker worker 中执行项目文件写入、Shell、Godot headless 与临时脚本。
- `workspace-codeact-safety`: 通过已解析路径边界、内容摘要基线、Editor 打开文件预检和差异归属保护工作区。
- `editor-observation-rpc`: 通过安全的本机 EditorPlugin RPC 提供按需 reload、截图和运行时只读观察。
- `codeact-verification-and-audit`: 定义按修改对象验证、地图失败修复循环、审批与完整可审计执行时间线。
- `codeact-role-orchestration`: 定义 programming、map、scene、advisor 与 coordinator 在统一工具协议下的权限和单写入者编排。

### Modified Capabilities

- `map-edit-transactions`: 将地图持久化写入从 Editor Undo/回滚事务改为 worker 文件编辑后的范围与语义验证，失败保留 diff。
- `map-workflow-state-and-evidence`: 为地图 CodeAct 写后验证、重试预算、失败验证结果和任务 diff 证据增加持久化工作流语义。
- `domain-owned-execution-workflows`: 将 map owner 的执行模型适配为统一 CodeAct 角色编排，并约束同一项目同一时刻仅一个写入型 agent。

## Impact

影响后端工具注册、权限和路径安全模块、worker 生命周期、Godot/headless 调用、地图验证与工作流状态、EditorPlugin、前端预览/审批展示及审计存储。需要 WSL2 rootless Docker 和适配项目 Godot 版本的 worker 镜像；前端不再承担工具执行或 `tool_results` 回填。
