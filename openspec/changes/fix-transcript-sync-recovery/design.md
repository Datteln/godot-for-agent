## Context

服务端为一次地图请求持久化并发布了后续 Thought、助手消息、工具活动与审批；Godot 客户端在 `ClassInfo TileMap` 后却不再显示新条目。现有协议已有 sequence resume、历史快照和 revision-aware projection，但实际运行没有把“已持久化、但前端未显示”的断档转换为可恢复的状态。

该变更跨越 Python 服务的订阅发布/历史接口和 Godot 插件的连接、投影与渲染路径。恢复不能重发聊天命令或工具审批，也不能让旧会话覆盖当前会话。

## Goals / Non-Goals

**Goals:**

- 让任意已持久化的用户可见 entry 最终通过实时补丁或快照显示在正确会话中。
- 对 sequence 缺口、补丁拒绝、投影异常及活跃会话的可见条目停滞实施有界、静默恢复。
- 以 session、event sequence、entry ID、revision 和恢复原因定位断档，不记录模型 Thought 正文。
- 覆盖长 Thought 与大地图结果后仍持续产生工具/审批条目的真实链路。

**Non-Goals:**

- 不改变模型的思考内容、工具执行语义或审批授权。
- 不把 HTTP 命令响应当作可见转录的替代通道。
- 不显示会干扰用户的“同步失败/恢复中”聊天消息。

## Decisions

### 使用“可见进度”而非任意网络活动判断停滞

客户端分别维护收到、投影和渲染的最高连续 event sequence，以及最后一次可见 entry 推进时间。服务端继续提供每个会话的最新可见序列水位。只有服务端水位或活跃会话确有新的可见 entry、而本地投影/渲染水位没有推进时，才触发恢复。

备选方案是将心跳或任意工具事件视为健康信号。拒绝，因为它们会掩盖“ClassInfo 后 Thought/审批没有显示”的问题。

### 以最慢的可见水位而非接收水位决定同步健康

服务端 `visible_seq` 必须与客户端 `min(received_seq, projected_seq, rendered_seq)` 比较，而不是只与 WebSocket 的最高连续接收序号比较。`received_seq` 只能证明传输层接收并确认，不能证明流式补丁已离开 `_patch_batcher`、被 Projector 接受或已由视口挂载。

流式补丁批处理必须维护最早待投影时间和待处理数量；若其非空且在一个有界时间内没有推进 `projected_seq`，客户端将其视为同步失败并进入既有恢复状态机。`rendered_seq` 仅可在 renderer/viewport 已接受对应 Store 修订后推进，不能仅因 Store 水位更新而乐观追平。

备选方案是周期性无条件加载历史。拒绝，因为它会隐藏实时链路故障、造成不必要的快照重建，并可能在活跃审批期间扰乱滚动位置。

### 实时事件必须在提交后确认，而非在解包时确认

当前 socket 在解析到连续事件后立即推进同一个连续游标并发送 ACK，随后才 `event_received.emit` 给 ChatPanel。该顺序把“网络包已到达”错误等同于“条目已为用户可见”：批处理、Projector 或视口随后停滞时，服务端已把该事件视为消费完成，实时重放无从发生；历史快照因从持久化 transcript 重建，才会在人工加载时重新出现。

客户端必须拆分 `received_seq` 与 `committed_seq`。前者只用于检测网络包的连续到达及入站队列排序；后者表示对应 event/revision 已由 Projector 写入权威 Store，且 renderer/viewport 接受其呈现。只有 `committed_seq` 可以作为 ACK 游标和下一次 subscribe 的 `after_seq`。一个流式补丁在 `_patch_batcher` 中等待时尚未 committed；同一批次的提交按事件序号连续推进 ACK。拒绝、generation 不匹配或渲染失败不得推进 committed 游标，必须保留未提交事件或使服务器可从最后 committed 游标重放。

备选方案是在 socket 收到后写入内存队列便 ACK。拒绝，因为 Godot 主线程、Projector 或视口随后失败时，内存队列并不提供“用户已看到”的提交保证，仍会重现本次实时消失而历史可见的问题。

### 半开订阅与 Reset 都必须有明确边界

WebSocket 连接处于 Open 状态但超出心跳/可见事件新鲜度窗口时，不得被视为健康。客户端应以有界的 recovery-pointer 或 history probe 确认服务端活跃状态；探针表明服务端领先或无法确认时，走续传/快照恢复，而不是无限等待一个不会到来的事件。

用户 Reset 是命令边界而非单纯 UI 清空：客户端先取消当前 HTTP 请求、清空排队的 chat/tool-result 请求并请求 `/chat/interrupt`，再停止订阅、执行 `/reset`、递增 generation 并水合新会话。每个异步响应均以 session、turn 和 generation 校验，旧轮次产生的 late response 或 patch 不得恢复、重新订阅或污染新状态。

### 恢复顺序为续传、再快照，并保持请求活跃

检测到断档时，客户端停止接受不能连续应用的可见补丁，并以最后连续游标重连订阅。服务端明确返回 retention/subscriber gap 时，或一次续传后仍不能闭合缺口时，客户端原子水合 history snapshot，并从其 `upto_event_seq` 重新订阅。相同 active turn 在此期间保持等待；恢复逻辑绝不重新发送 HTTP 聊天、批准或中断命令。

备选方案是立即取消后重新发送请求。拒绝，因为可能重复执行地图编辑或审批。

### 投影错误与传输缺口使用同一恢复入口

解析失败、entry/revision 不连续、Projector 拒绝或渲染路由未能建立，都记录类型化脱敏诊断并进入同一个有界恢复状态机。快照替换 Store 后，渲染从 Store 重建所有 entry kind；不可只补最后一条 Thought 或 HTTP final。

备选方案是在 ChatPanel 中直接追加 HTTP/tool 文本。拒绝，因为会绕过 revision/ordering 保证并造成重复条目。

### 虚拟视口只缓存稳定的条目布局，并为瞬时提示预留可见挂载点

`TranscriptViewport` 仅在条目完成有效布局后记录其高度；未布局、零宽布局或与当前内容预算明显不相称的瞬时测量值不得写入高度缓存或 spacer。内容、宽度桶或展示模式改变时，应使旧测量失效并在稳定布局后重新测量，避免一个 Thought 的错误高度将滚动底部推离实际条目。

本地 waiting、error、report 等 transient 提示不属于 transcript 条目或虚拟化测量，但必须挂载在专用的可见提示区域，而非被 `bottom_spacer` 隔开的同级末尾。显示提示并请求滚动到底部后，最后一个实际条目与提示都必须位于可视范围。

备选方案是对所有测量高度设固定上限。拒绝，因为合法的长条目会被错误压缩；应依据布局稳定性和当前内容/宽度状态判定测量是否可缓存。

### 订阅过载必须显式要求同步

服务端对单订阅者不能静默丢弃包含可见状态变化的事件。超出有界队列能力时，服务端发出可机读的 `resync_required` 并终止或重置订阅，使客户端走权威快照路径。

备选方案是继续丢弃旧事件并维持连接。拒绝，因为客户端无法区分空闲与不完整转录。

## Risks / Trade-offs

- [长 Thought 或密集地图结果增加事件与重放量] → 使用连续游标、已有 revision 去重和每个停滞水位一次有界恢复，快照仅作为兜底。
- [短暂编辑器卡帧造成误判] → 基于服务端可见水位与连续进度的组合，并将阈值配置化。
- [恢复期间收到新 live patch] → 暂存或拒绝非连续 patch，完成 snapshot/续传后按 cursor 恢复。
- [传输已确认但批处理或 renderer 停滞] → 用最慢水位和批处理积压时钟触发既有有界恢复，不将 received 水位视作用户可见进度。
- [事件被过早 ACK 而未显示] → 拆分 received/committed 游标，仅从 committed 游标确认与续传；恢复只处理真实传输/进程故障而非弥补正常链路提前确认。
- [Open 但半开的 WebSocket 或 Reset 与旧请求竞争] → 用订阅新鲜度探针及中断优先的 Reset 屏障确认状态，并用 session/turn/generation 拒绝迟到响应。
- [诊断泄露提示词或 Thought] → 日志只保存标识符、计数、序列、原因和耗时。

## Migration Plan

1. 增加兼容的可见水位、显式重同步信号和客户端恢复诊断；旧客户端忽略新增字段。
2. 先启用观测与集成测试，再启用有界自动恢复。
3. 发布服务端与 Godot 插件；模拟长 Thought、掉补丁、投影拒绝和历史水合。
4. 如需回滚，关闭自动恢复触发器，保留既有 resume 与诊断，不改变持久化 transcript 格式。

## Open Questions

- 默认停滞阈值需要依据真实 Godot 编辑器帧停顿和模型 token 间隔测量后确定。
- 服务端可见水位适合放入心跳、订阅确认还是独立诊断事件，需要在现有协议实现处确定。


## ClassInfo 断档成因分析（任务 1.1）

对发布/保留/订阅/投影/渲染全链路追踪后，确认 ClassInfo 之后前端停摆不是单一丢包点，而是"恢复链本身存在无重试的死路"：

1. 触发条件：`read_class_docs`（TileMap）产生超大工具结果，随后长 Thought 持续流式发布。编辑器主线程在重负载期间无法轮询 `chat_event_socket._process`，TCP 接收缓冲堆满，服务端 `websocket.send_json` 阻塞。
2. 服务端背压：`OutboundBuffer` 条数/字节预算耗尽后正确发出 `resync_required` 并关闭订阅（`app/events/store.py`、`app/events/websocket.py`），此环节没有静默丢失。
3. 客户端恢复链：`resync_required`/序列缺口/Projector 拒绝都汇入 `_begin_hydration` + `_request_hydration_history`。活跃轮次中走探针通道（`probe_get /sessions/{id}/history`）。
4. 具体死路（静默停摆点）：
   - 探针请求失败或响应迟到时没有任何重试与看门狗；Projector 停在 `HYDRATING`，之后每条实时补丁都以 `projector_not_ready` 被拒，只写日志、不再升级，视口永久停在 ClassInfo 条目。
   - `_on_probe_response` 只在 `_hydrating_for_recovery`/`_idle_recovery_active` 为真时处理 `transcript` 响应；状态标志在会话切换/超时等路径被清掉后，迟到的水合响应被整体丢弃。
   - 服务端心跳不携带任何可见进度，客户端无法区分"模型空闲"与"已持久化的可见条目没有送达"。
   - 传输缺口当前直接跳过快照前的"从连续游标续传"一步：`_handle_event` 发现 `seq != contiguous+1` 立即水合；若保留窗口本可重放，也会因水合失败而卡死。
5. 游标归属：received 游标在 `chat_event_socket.gd`（`_highest_contiguous_seq`），projected 游标在 `transcript_store.gd`（`upto_event_seq`），rendered 无独立游标（视口只从 Store 重建，渲染失败仅记录 `renderer_rejected`）。三者没有统一的恢复入口与有界重试。

结论：恢复入口必须收敛为一个有界状态机（续传 → 快照），并由服务端可见水位驱动的停滞看门狗兜底；对 Open Question 2 的决策：可见水位复用已有心跳周期承载（`heartbeat` 与 `subscribed` 消息新增字段），不新增消息类型。
## Open Questions 处理结果（实现时确定）

- 可见水位承载位置：复用既有消息类型——`subscribed` 与 `heartbeat` 新增 `visible_seq`/`visible_updated_at`（及 `last_seq`）字段；不新增消息类型，旧客户端忽略新字段。
- 停滞阈值：默认 20 秒，暴露为 `TranscriptRecovery.stall_threshold_s`；水合超时重试复用同一阈值。真实编辑器帧停顿实测后可直接调整常量。
- 恢复水合使用 `limit=0` 完整快照，分页裁剪只影响启动时的常规历史加载。
## 现场复诊后的范围调整：从源头消除 ClassInfo 毒帧

15:35 的日志显示 e16 的终态 `read_class_docs(TileMap)` 结果约为 132KB，之后前端应用层不再接收 WebSocket 内容，而 HTTP 仍可工作。该现象表明大终态工具结果绝不能作为实时 transcript 载荷；是否由 TCP、uvicorn 或 Godot peer 的具体实现造成楔死，不改变这一输入边界。

本 change 不新增连接存活看门狗或服务端发送超时。已有的序列缺口、投影失败、重放和快照恢复继续适用于实际可到达的事件；本次新增设计从源头确保 `read_class_docs` 不再生成大实时事件。

### 按需 ClassDB 查询

`read_class_docs` 提供三种有界模式：`overview` 仅返回类名、父类和查询能力；`search` 根据动作词返回有限候选成员的名称与短签名；`members`/`constants` 只返回调用者明确列出的有限成员或常量。请求和响应均受数量与序列化字节上限约束。超过上限时返回 `class_docs_query_too_large` 及可执行的缩小查询提示，绝不截断后伪装为完整结果。

地图 agent 必须先根据已识别的目标类型与拟执行动作选择 API，例如 legacy `TileMap` 的 builder 先请求 `set_cell`、`clear_layer`；不知道成员名时先 `search`，再读取候选中的精确签名。它不得把读取整类文档当作写代码前置条件。

### 分离 LLM 工具事实与可见记录

当前一次工具调用返回的少量精确签名只在该模型步骤中作为工具事实使用；完整 ClassDB/API 文本不得进入会话持久化帧、权威 transcript、history、WebSocket 或 renderer。后续步骤若再需要 API，必须重新发起受限查询。

可见 `tool_activity` 只保留 ClassDB 查询的 class 名和查询成功状态；ClassInfo 标题精确为 `ClassInfo <class_name>`，不附加成员数量、载荷字节或原始 JSON。其他工具结果沿用各自既有的展示边界，不新增统一实时字节上限。

备选方案是把大 WebSocket 内容分片重组。拒绝，因为会扩大事件顺序、重放和 renderer 复杂度，仍会让不必要的 ClassDB 占据 LLM/会话上下文。

### 检索结果必须在工具源头分级且有界

`grep_code` 的默认 `**/*` 不能等同于“所有项目内文本都适合交给模型或 UI”。运行日志、缓存、服务状态与生成诊断文件是可观测数据，不是源码检索语料；它们必须由路径策略在扫描前排除。需要诊断日志时，应使用专门的、分页且逐行摘要的日志读取能力，而不是复用代码检索。

每个可返回多行匹配的检索工具必须在产生结果时将每项规范化为：相对路径、行号、受限 excerpt、原始行是否被截断以及匹配/扫描计数。不得把完整命中行先传到编排层、再仅在 UI 格式化时截断。模型上下文与可见 transcript 只能接收该规范化结果；历史快照同样不得恢复未受限原文。

该决定不是全局“可见工具结果 4 KiB”封顶：`read_file`、差异预览、ClassInfo、检索和前端工具可继续使用各自的语义边界。约束的是不可信/非源码的递归数据与单个检索匹配不得突破其专用 excerpt 合约。

### 终态工具补丁在进入 Godot 前必须可解析

现有实时有界化仅针对增长型 Thought/assistant 补丁；工具 resolved/failed 是不可合并终态补丁，可能携带检索摘要的嵌套大字符串。前端随后在 Godot 主线程同步 JSON 解析整包，因此“渲染时截断”无法避免帧阻塞。

服务端必须在 WebSocket fan-out 前衡量终态 `transcript_patch` 的序列化字节数。正常路径发布已受工具语义约束的摘要；若由于兼容数据或未知工具仍超出终态补丁预算，服务端不得发送原始补丁。它必须替换为不含原文的安全摘要（工具名、状态、计数、截断/超限标志），或对该订阅显式 `resync_required`，使客户端通过权威的已净化快照恢复。前端也必须设置包大小拒绝阈值，在 `JSON.parse_string` 前拒绝异常 packet 并进入既有恢复状态机，绝不能在主线程解包后才发现超限。

备选方案是只增加 RichTextLabel 展示预算。拒绝，因为主线程在 RichTextLabel 创建之前已经完成 WebSocket packet 解码、JSON 解析和 Dictionary/String 分配，无法阻止 `Grep · map-agent` 后的帧饥饿。
