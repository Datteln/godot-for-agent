## Context

当前工具面同时包含服务端读搜工具、前端转发的 Godot 写工具、任意宿主命令和 Editor 内执行代码，写入来源、权限边界和验证结果并不统一。项目实际运行路径可能通过受控软连接到 `ai_agent_frontend`，而 Git 根又可能高于该目录；因此“项目路径”“可挂载路径”和“Git 观察路径”必须分别处理。

本设计将 programming、map、scene 等现有执行 agent 保留为同一行动循环中的不同角色，而不是引入 Python-only 运行时、常驻 CodeSession 或独立 code-agent。coordinator 仍只负责拆分、委派和汇总。

## Goals / Non-Goals

**Goals:**

- 让全部项目持久化修改经由隔离 worker 对文件系统完成，并支持读、改、运行、验证、修复的连续循环。
- 在不需要在线信息时完全不触碰 Editor；需要时只通过后端代理的受限本机 RPC 获得观察和验证证据。
- 以路径、容器、审批、内容摘要和 diff 归属共同收敛损害范围，保留清晰的失败与审计证据。
- 让地图、场景、资源和代码依据其对象类型获得目标化验证，地图验证失败可重试修复但绝不伪造成功。

**Non-Goals:**

- 不构建通用跨文件事务、proposal/lease/commit 写入协议或自动回滚机制。
- 不在在线 Editor 中暴露写工具、打开新场景、保存项目资源或执行模型提供的 GDScript。
- 不以同机 Docker 作为对抗宿主高权限攻击者或容器逃逸的安全承诺。
- 不在本阶段支持多写入 agent 并发、云端 worker 或未打开场景的自动截图。

## Decisions

### 1. Execution Gateway 是唯一的工具编排入口

后端为每一次任务创建 `task_execution_id`，并对 `project.*`、`shell.run`、`godot.headless`、`git.*`、`skill.load`、`tool.search` 与 `godot.editor.*` 统一执行身份、角色、参数、路径、超时、审批和审计检查。工具结果以类型化结构自动续接原 agent；前端只显示日志、diff、artifact 和审批，绝不代为执行或通过新的 chat 请求回填结果。

选择该网关而非让 worker 直连 Editor 或让前端中继，可将项目匹配、取消、迟到结果和审批集中在一个可信边界，并切断前端伪造工具结果的路径。

### 2. 解析后的受控项目根是 worker 写入边界

启动任务时计算 `logical_project_root`、`resolved_project_root` 与 `repository_root`。只将已解析、位于允许目标集内的 `resolved_project_root` 及显式允许的软连接目标挂载到 worker 的 `/workspace`；每个文件和 cwd 校验其真实解析路径。Git 状态和 diff 在 `repository_root` 只读计算，除非该根被单独允许挂载。

这比将服务路径或 Git 根直接作为写入根更能避免软连接扩大权限。解析失败、越界或根不匹配必须在创建 worker 前拒绝任务。

### 3. 每任务复用受限 Docker worker，并隔离导入缓存

Gateway 为每个任务创建一个 WSL2 rootless Docker 容器，在整个任务的 Shell、headless 和临时脚本调用间复用该容器和任务临时目录；每项调用仍启动独立进程，不能依赖 Shell 内存状态。worker 非 root、无网络、无宿主用户目录、Docker socket、Git 凭据和长期凭据，并施加 CPU、内存、进程、时长及输出上限。

任务专属 volume 覆盖 `/workspace/.godot`，使 Windows Editor 的 `.godot` 缓存与 worker 导入缓存不共享；任务终止、取消或超时时清理容器、临时目录与缓存。相比直接运行 Windows Shell/Godot，这可实际限制模型产生的副作用；相比每调用新建容器，它保留同一任务所需的导入缓存和临时文件。

### 4. 文件补丁与受限临时脚本是唯一写入路径

`project.edit` 负责创建或应用小型可审查补丁。复杂场景和地图变换可在任务临时目录写入临时 GDScript，并只通过 worker 中的 `godot --headless --path /workspace --script` 运行。每一次变更动作之后收集实际任务 diff 并运行对象匹配的验证；临时脚本结束即清理，除非明确经 `project.edit` 保留为项目脚本。

不采用逐文件审批或事务提交，因为普通迭代会变慢且对同一用户的日常编辑价值有限。高风险 shell、联网、项目外访问、批量破坏性写入、依赖安装和 Editor UI reload 仍走 allow/ask/deny 策略。

### 5. 内容摘要基线和单写入者取代通用事务

任务开始记录 Git status、已有 diff 摘要和执行 id；首次触及每个文件时记录内容摘要，并在应用补丁前重新比对。摘要改变时返回 `workspace_conflict`，不覆盖文件。每个 shell 或临时脚本调用后立即采集 diff，并与开始前的已有变更分开归属。coordinator 同一项目只调度一个写入型 agent；只读工作可并行。

该策略不能防止用户直接编辑磁盘，因此冲突发生时 agent 停止触及冲突文件并明确报告。它比 proposal/lease 更适合 CodeAct 循环，同时避免把并发变更误记为任务结果。

### 6. 在线 Editor 是只读观察仪器

EditorPlugin 使用仅本机可达的持久 RPC 注册项目标识、Editor 实例标识及后端签发的短期令牌。Gateway 仅向匹配任务项目的最新存活实例发送 allowlist 方法：`status`、`reload_for_validation`、截图、受限运行时状态、调试器错误和性能快照。请求携带执行 id、调用 id、参数和超时；取消、busy、项目不匹配、不可用和迟到结果均返回或记录为类型化状态。

`reload_for_validation` 仅在在线验证确有需要、目标已打开且内存无未保存修改时执行；因为会影响 UI，按策略审批。Plugin 已连接时，`project.edit` 在编辑 `.tscn`、`.tres` 等可能被 Editor 管理的文件前检查状态，只要文件打开（clean 或 dirty）即返回 `editor_open_conflict`。此额外限制消除 clean 检查到保存旧内存文件之间的竞态。

命名管道和受限 loopback 是待验证的 IPC 载体；两者均须满足本机监听、令牌和项目标识检查。选择在技术验证后定案，避免假设 Godot 插件可用的 API。

### 7. 验证与失败完成语义按对象类型定义

每次写入后先展示 diff，再执行最小相关验证：脚本/测试运行对应测试或静态检查；场景加载 `PackedScene` 并按需要安全实例化；资源通过 `ResourceLoader.load` 和类型断言；地图运行现有范围、语义和目标区域校验。在线截图、运行时、Debugger 和 Profiler 只是附加证据，可能过期或包含不可信文本，不能改变权限规则。

地图修改后的失败报告应回注入原 map agent，直至通过、预算耗尽、取消或不可修复。后一类以 `failed_validation` 结束并保留当前可见 diff，取代既有自动回滚写组语义。这真实反映文件 CodeAct 的工作区状态，也允许用户审阅或手工处理未完成修改。

### 8. 审计记录证据，而非控制指令

Gateway 以执行 id 关联文件补丁及前后摘要、临时脚本 hash/路径/命令/产物、Shell 与 Godot 参数摘要和输出 artifact、Editor RPC 与审批决定、验证器版本和结果、重试及最终回复。完整日志、代码和截图在持久化前按项目策略限制大小并过滤敏感内容；artifact 内文本、项目文件、日志和模型输出一律是数据，不能提升权限。

## Risks / Trade-offs

- [Docker 镜像、Godot 版本或导入依赖与项目不兼容] → 先用真实项目验证镜像、导入和测试入口；不兼容时返回不可用验证而不是声称通过。
- [软连接或 Git 根解析放大 worker 边界] → 启动前解析所有目标，与显式 allowlist 比对并拒绝越界；Git 根默认只读。
- [Editor API 不能可靠报告 dirty/reload/调试数据] → 进行小型技术验证；无法满足时返回 `editor_unavailable`，保留 headless 方案。
- [用户磁盘修改与 worker 修改竞争] → 内容摘要重检、打开文件预检、单写入 agent 和可归属 diff；冲突时停止而不是覆盖。
- [保留失败 diff 可能让工作区处于半完成状态] → 最终回复和 UI 明示 `failed_validation`、未通过验证与 diff，用户决定恢复或继续修复。
- [临时脚本或工具输出注入高副作用操作] → worker 无网络/凭据、命令策略、资源限制、参数审计及高风险审批。

## Migration Plan

1. 实现路径解析、worker 生命周期、命令策略、资源限制和任务专属缓存，先以只读和受限 Shell 验证隔离。
2. 将现有 server 读搜、artifact 读取、skill 和工具发现归并到统一协议，并让 programming agent 跑通 `read/search/edit + shell + diff`。
3. 在 worker 中接入 Godot headless、临时脚本和场景/资源/地图目标化验证；将地图改为写后范围与语义验证循环和保留 diff 的失败语义。
4. 移除或拒绝任意宿主 Shell、前端 Editor 写工具和宿主 Editor GDScript 通道。
5. 以特性开关接入最小 EditorPlugin RPC，完成注册撤销、取消、审批和本机 IPC 技术验证后，迁移截图与在线观察调用。
6. 分阶段放量并保留审计与 diff 可见性；回滚时关闭 Gateway 新工具路由和 Editor RPC 开关。已由 worker 写出的文件不自动回滚，须由用户依据 Git diff 处理。

## Open Questions

- 目标项目的 Godot headless 镜像版本、导入依赖和可执行测试入口是什么？
- 命名管道与受限 loopback 中，哪一种满足 Godot EditorPlugin 的实际可用 API、认证和取消需求？
- 地图重试预算、单次行动的新增/改写文件上限、artifact 保留期与脱敏规则的默认项目策略为何？
- 受控软连接允许跟随的宿主目标集合及 Git 根解析链如何配置和测试？
