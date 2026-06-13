# Python LLM 服务架构方案（OpenAI SDK · 多智能体 · 借鉴 Claude Code）

| 项目 | 内容 |
|------|------|
| 文档名称 | AI 游戏开发 Agent —— Python 服务架构方案 |
| 版本 | v0.4.6 |
| 日期 | 2026-06-13 |
| 依据 | 《Godot 内嵌 AI 游戏开发 Agent 需求文档》v0.8.1；借鉴 `docs/` 下 Claude Code 工作原理（入口、配置、Prompt、Agent、文件/LSP、MCP、Retry、Hook、Memory、Doctor、Command、恢复指针等） |
| 范围 | **Python LLM 服务（Agent 层）**；前端（GDScript 插件）的工具执行与预览 UI 不在本文，但定义二者协议 |
| 变更 | **v0.4.6（命名一致性）**：agent 名统一 kebab-case，与 AgentDefinition 文件名/响应 `agent` 字段一致；§6.1 加命名约定、§6.4 注明与详设A §2.2 同一模型。见术语表 §4.A item 15 |
| 变更 | **v0.4.5（跨文档一致性校对）**：依据更新到需求文档 v0.8.1；与详设A/B v0.3、术语表 v1.1 对齐 Claude Code 同构 Agent/Skill 与扩展内容安全边界 |
| 变更 | **v0.4.4（Agent/Skill 改为 Claude Code 同构模式）**：Agent/Skill 从"参考/适配"收敛为本项目原生同构扩展模型；Agent 使用 markdown frontmatter + body 的 `AgentDefinition`；Skill 作为 PromptCommand/SkillTool 同构能力；补充扩展内容安全边界、来源层级、工具解析与 Doctor 展示要求 |
| 变更 | **v0.4.3（继续吸收 Claude Code 可迁移机制）**：补入 System Prompt 分段/Output Style、Thinking/Effort/Advisor 档位、FileState/LSP、配置 schema + migrations、MCP 连接治理、API 重试分级、Memory、Doctor、Command、Hook、AgentDefinition/TaskType、最小恢复指针 |
| 变更 | **v0.4.2（补齐 Agent 工作流）**：拆出 `QueryEngine` 门面与 `query_loop` 内核；明确 prompt 构建/缓存边界；加入上下文压缩梯度（microcompact/full compact/熔断/恢复）；ToolDef 增安全默认与并发分区元数据；补充事件流、Hook 与 ToolSearch 的后续落点 |
| 变更 | **v0.4.1（与前端方案对齐）**：Context 增 `dotnet_enabled`（C# 工具暴露依据，PRD D2）；ToolResult 增 `grant_session_allow`（授权升级）；`enrich` 改为结构化合并（前端签名是 dict 非字符串）；`/reset` 明确 body 带 `session_id` |
| 变更 | **v0.4（评审收紧 v1 边界）**：本地 HTTP 一次性 token 鉴权；ToolDef 细化 effect 元数据；LLM 抽象为 `LLMProvider`（默认 OpenAI 兼容）；`delegate` 单独调用约定；结构化 DTO；`turn_id`/幂等/并发锁；MCP 默认仅静态分析（editor-state 需 Godot 在线）；会话本地持久化 |
| 变更 | v0.3：引入多入口（HTTP+MCP）、多智能体（coordinator+专家）、权限系统、安全边界、代码检索、Skill 系统；保留 v0.2 的混合文档接地 |

---

## 1. 目标与依据

本服务是三层架构中的 **Agent 层**（需求文档 §4.1）。在 v0.3，它从"单循环 Agent 服务"升级为一个**小型 Agent 运行时**，借鉴 Claude Code 的几条核心架构经验：

| Claude Code 模式 | 本服务的对应落地 |
|------|------|
| 一份源码、四种入口（CLI/SDK/MCP/Sandbox） | **一套工具/权限/skill，两个入口（HTTP for Godot、MCP for 外部 AI 客户端）** |
| 工具调用统一过 `hasPermissionsToUseTool` | **统一权限闸**：每个工具调用先过 `check_permission` |
| 专用工具 vs 通用 bash 的取舍 | **全专用工具、不暴露通用 shell**，把安全边界焊进工具形状 |
| `coordinator/` + `tasks/` + `AgentTool` 多 Agent | **Claude Code 同构 Agent 运行时**：coordinator + 领域专家子 agent，上下文隔离、可委派 |
| `skills/` 渐进式技能 | **Claude Code 同构 Skill 系统**：Skill 是 prompt command；简述常驻、全文按需加载、可条件激活 |
| `Glob`/`Grep` + 检索 | **代码检索子系统**：精确(Glob/Grep) + 语义(RAG) |
| Sandbox schema、trust dialog、安全/完整环境变量分离 | **安全边界**：项目根约束、信任模型、密钥隔离、settings schema |

---

## 2. 设计原则

1. **一份能力，多入口复用**：工具、权限、skill、检索只实现一次，被 HTTP 与 MCP 两个入口共享。
2. **工具即权限边界**：不提供通用 `bash`/`eval`；只暴露**类型化的领域工具**，每个工具自带 `side / effect 元数据（reads/writes/executes/network…）/ permission`，权限闸据此决策。
3. **默认不可逆操作需确认**：改动型工具默认 `ask`（→ 前端预览确认），只读直接放行（呼应 PRD 预览—确认机制）。
4. **上下文隔离的多智能体**：复杂任务由 coordinator 拆给领域专家子 agent，各自只带相关工具/文档，降幻觉、省 token、可并行。
5. **渐进式披露**：skill/工具/文档**按需加载**，固定上下文保持精简（利于缓存与成本）。
6. **最小信任面**：只操作受信任的工程目录；密钥仅本地、不外泄；检索/文件只读且限定项目根。
7. **对话循环可观测、可恢复**：Agent 主循环以事件驱动，记录每次继续/恢复原因（tool result、compact、重试、用户拒绝），便于前端展示、日志回放与断点恢复。
8. **Prompt 分层稳定**：system/工具 schema/常驻 skill 简述保持稳定；编辑器快照、检索结果、工具结果放动态区，避免频繁破坏缓存前缀。
9. **工具安全默认关闭**：只读、并发安全、可自动放行都默认 `False`；工具作者必须显式声明，宁可串行或询问，也不误放危险能力。
10. **配置变更可迁移**：任何会影响历史用户的配置/模型/目录改动都进入 `migrations/`，幂等、可重复跑、失败不阻塞启动。
11. **记忆选择性注入**：项目记忆、会话记忆、Agent 记忆只按需召回，避免把长期记忆全文常驻上下文。
12. **诊断优先于猜测**：提供 Doctor 自检，把 Python、端点、tool calling、doc dump、索引、MCP、权限规则、上下文体积等事实集中展示。

---

## 3. 技术选型

| 组件 | 选型 | 说明 |
|------|------|------|
| Web 框架 | **FastAPI** | 异步、Pydantic 校验、OpenAPI |
| ASGI | **uvicorn** | 本地 `127.0.0.1` + **一次性 token 鉴权**（§9.0）、随机/可配端口 |
| LLM 抽象 | **`LLMProvider`**（默认实现：**openai** Chat Completions） | function calling；`base_url` 适配任意 OpenAI 兼容端点（BYO key、本地模型）；后续可接 Responses / Anthropic / Gemini provider |
| MCP | **mcp**（Python SDK） | 把同一套工具暴露为 MCP server（第二入口） |
| 数据模型 | **pydantic v2** | DTO、settings schema |
| 检索 | ripgrep/glob（精确）+ FAISS/Chroma + embedding（语义，M2） | 代码检索子系统 |
| 配置 | pydantic-settings / `.env` + 项目内 settings 文件 | 端点/密钥/权限/skill |
| 配置迁移 | `migrations/` + `config_version` | 启动时幂等迁移旧配置 |
| LSP | `pygls`/JSON-RPC client（可选） | GDScript/C# 诊断、符号、跳转（M2+） |
| 记忆 | 本地 Markdown/SQLite | 项目记忆、会话记忆、Agent 记忆 |
| Doctor | FastAPI `/doctor` + 前端诊断面板 | 集中自检安装、端点、索引、MCP、权限 |

> **Chat Completions 是默认 provider profile**（最大化 OpenAI 兼容端点覆盖），不作为架构硬约束；官方将 Responses API 作为更新的 primitive，二者均支持 tool calling。LLM 访问统一走 `LLMProvider` 抽象（§15），便于后续切换。Prompt caching 仅"OpenAI 端点可利用"，不假定所有 `base_url` 支持。

---

## 4. 总体分层架构

```
                         ┌──────────── 入口层（多张脸）────────────┐
   Godot 前端 ──HTTP──►   │  api/   FastAPI（/chat /health /reset）  │
   外部 AI 客户端 ─MCP─►   │  mcp/   MCP server（ListTools/CallTool） │
                         └───────────────────┬──────────────────────┘
                                             │  统一进入
   ┌─────────────────────────── 编排层 ──────▼──────────────────────────┐
   │  query/         QueryEngine 门面 + query_loop 事件内核              │
   │  coordinator/   主控 agent：理解目标 → 规划 → 委派                  │
   │  agents/        领域专家子 agent（program / map / scene / resource）│
   │  orchestrator/  工具分流、agent 帧栈、resume、transition.reason      │
   └───────────────────┬───────────────────────────────────────────────┘
                       │  每个工具调用都经过
   ┌───────────────────▼──────────── 能力与守卫层 ─────────────────────┐
   │  permissions/   权限闸（allow / ask / deny，模式 + 规则）           │
   │  security/      安全边界（项目根、信任、密钥隔离、settings schema）  │
   │  tools/         工具注册表（域 / side / mutating / 权限元数据）      │
   │      ├─ front 工具（前端执行：脚本/节点/地图/资源）                 │
   │      └─ server 工具（本服务执行：检索、文档增强、内容生成）         │
   └───────────────────┬───────────────────────────────────────────────┘
                       │  支撑子系统
   ┌───────────────────▼──────────────────────────────────────────────┐
   │ retrieval/ 代码检索(Glob/Grep + RAG)  lsp/ 符号/诊断              │
   │ memory/ 项目/会话/Agent 记忆          skills/ 技能(渐进披露)       │
   │ sessions/ 会话与 agent 栈   llm/ OpenAI 封装   prompt/ 提示构建    │
   │ compact/ 上下文压缩与恢复   hooks/ 生命周期事件   commands/ 命令   │
   │ docs/ 文档接地(混合)        config/ 配置快照+migrations           │
   │ doctor/ 自检                 state/ 运行时状态/告警/缓存 latch     │
   └───────────────────────────────────────────────────────────────────┘
                                             │ 调用
                                             ▼
                        用户配置的大模型端点（OpenAI 兼容）
```

### 目录结构（建议）

```
server/
├── app/
│   ├── main.py
│   ├── config.py                  # 端点/密钥/权限/skill 配置
│   ├── api/                       # 入口①：HTTP（routes.py, schemas.py）
│   ├── mcp_server/                # 入口②：MCP（server.py）
│   ├── query/                     # QueryEngine 门面 + query_loop 事件内核
│   ├── coordinator/               # 主控 agent
│   ├── agents/                    # 领域专家子 agent 定义
│   ├── orchestrator/              # 工具分流 + agent 帧栈 + resume
│   ├── permissions/               # 权限模式 + 规则 + 决策
│   ├── security/                  # 项目根/信任/密钥/settings schema
│   ├── tools/                     # 注册表 + schema + server_tools/
│   ├── retrieval/                 # glob_grep.py（精确）+ rag.py（语义）
│   ├── lsp/                       # 可选 LSP：诊断/符号/定义/引用
│   ├── memory/                    # project/session/agent memory
│   ├── skills/                    # skill 加载与渐进披露
│   ├── commands/                  # /compact /doctor /reset /permissions /index...
│   ├── docs/                      # 混合文档接地 enrich
│   ├── sessions/                  # 会话与 agent 栈
│   ├── llm/                       # OpenAI 封装
│   ├── prompt/                    # system prompt 分段构建 + cache 边界
│   ├── compact/                   # microcompact/full compact/恢复/熔断
│   ├── hooks/                     # SessionStart/PreToolUse/PostToolUse/PreCompact...
│   ├── doctor/                    # 自检聚合器
│   ├── migrations/                # 配置/目录/模型别名迁移
│   ├── recovery/                  # 最小恢复指针（session_id + pending_turn）
│   └── state/                     # 运行时 store：告警、cache latch、配置快照
└── skills/                        # Claude Code 同构 Skill 目录（bundled/user/project/plugin，每个一 SKILL.md）
```

---

## 5. 多入口形态（HTTP + MCP）

借鉴 Claude Code"一份源码多张脸"：同一套**工具 / 权限 / skill / 检索**被两个入口复用。

| 入口 | 调用方 | 用途 | 上下文 |
|------|------|------|------|
| **HTTP**（主） | Godot 前端插件 | 完整能力：含改动型工具（经预览确认）、多智能体 | 完整 `ToolContext` |
| **MCP server** | 外部 AI 客户端（Claude Desktop / Cursor 等） | **默认仅文件检索 / 静态分析**（读盘脚本、glob/grep、文档 prose）；editor-state 工具（场景树/选中/ClassDB 签名）**需 Godot 在线才暴露** | **简化 `ToolContext`**：`interactive=False`、不启多智能体 |

要点（对齐 Claude Code 的"简化版 ToolUseContext"）：

- MCP 入口构造**简化上下文**：**默认只暴露不依赖编辑器在线的工具**——文件检索、静态分析、文档 prose；**editor-state 工具**（读场景树/选中节点/ClassDB 签名）与**改动型工具**只有在 **Godot 前端在线**时才暴露（否则这些状态根本拿不到、改动也无法落地），离线时对应调用 `deny`。
- 两入口共用 `permissions/`、`tools/`、`skills/`、`retrieval/`，避免实现分叉。
- MCP 入口让本项目**同时覆盖竞品两大流派**（编辑器内插件 + MCP server，见需求文档 §1.5）。

### 5.1 MCP 连接治理与工具注解

Claude Code 的 MCP 价值不在"能接任意服务"，而在**统一连接状态、权限与工具风险元数据**。本项目采用轻量版：

- **连接状态用 tagged union**：`disabled` / `connecting` / `ready` / `failed` / `offline_waiting_godot`，不要用散落的 bool；`/doctor` 与前端面板直接展示该状态。
- **配置来源可追踪**：每个 MCP server 配置标注 `source=editor_settings|project_config|runtime`；项目内配置只能减少能力，不能新增高风险 server 或提升权限。
- **工具注解对齐 `ToolDef`**：MCP 工具的 `readOnlyHint`、`destructiveHint`、`idempotentHint` 等映射为 `is_read_only`、`writes_project`、`executes_process`、`is_concurrency_safe`，统一进入权限闸。
- **Godot 离线时 fail-closed**：依赖 editor-state 或写工程的工具进入 `offline_waiting_godot`，`ListTools` 不列出或 `CallTool` 返回可解释拒绝。
- **不把外部 MCP 当绕权通道**：MCP 入口没有前端确认 UI，`ask` 默认降级为 `deny`；若未来支持 MCP 客户端确认，也必须把确认结果转成同一份 `ToolResult` 审计记录。

---

## 6. 多智能体架构（coordinator + 领域专家）

借鉴 Claude Code 的 `coordinator/` + `AgentTool` + `tasks/`。

> 📐 **详细设计与全链路时序图见**《多智能体与权限系统详细设计》§1–2（数据结构、`delegate` 工具、帧栈生命周期、上下文隔离、并行子 agent）。

### 6.1 角色

| Agent | 职责 | 工具子集 | 模型档位（建议） |
|------|------|------|------|
| **coordinator（主控）** | 理解目标、规划、把子任务**委派**给专家、汇总 | `delegate`、`read_*`、检索、skill | 强模型 |
| **programming-agent** | 写/改/重构/修脚本、单测 | program 域工具 + 文档接地 + 检索 | 强模型 |
| **map-agent** | 瓦片地图建造 | map 域工具 + 瓦片上下文 | 中/快模型可 |
| **scene-agent** | 节点/场景搭建 | scene 域工具 + ClassDB 接地 | 中模型 |
| **resource-agent** | 资源/项目配置 | resource/project 域工具 | 快模型可 |

- **上下文隔离**：每个子 agent 只带**本域工具 + 相关上下文/skill**，互不污染——降幻觉、省 token（Claude Code 子 agent 隔离思想）。
- **委派工具 `delegate(agent, task)`**：coordinator 调用它派发子任务；服务为该子 agent 开一个**独立的 agent 帧**（独立 messages、独立工具集），跑到产出结果后把**摘要**回传 coordinator。
- **模型档位可不同**：简单域用便宜/快模型（呼应需求文档创新点⑥ 多模型路由）。
- **命名约定**：agent 名一律 **kebab-case**，与其 `AgentDefinition` 文件名一致（`programming-agent.md` 等）。`delegate(agent=...)` 的取值、响应 `calls[].agent`、`frame.agent.name` 都用同一套 name；委派可选值**运行时由注册表生成**，非硬编码（见详设A §2.3）。

### 6.2 Agent 帧栈与跨进程挂起

多智能体 + 跨进程预览确认的关键：**会话内维护一个 agent 帧栈**。

```
session
└── agent_stack: [ frame(coordinator), frame(map-agent), ... ]   # 栈顶为当前活跃 agent
    每个 frame: { agent_id, messages, tools }
```

- 任一帧产生**前端工具调用**（需 Godot 执行）时，**整个栈挂起**，把该调用（标注来源 `frame_id`）返回前端。
- 前端执行/确认后回传 `tool_results`，服务按 `frame_id` 找到对应帧、append 结果、从该帧**继续**。
- 子 agent 结束 → 弹栈，把结果作为 `delegate` 工具结果回传上一层（coordinator）。
- **v1 先串行**（同一时刻一个活跃帧）；**并行子 agent**（IN-6 自主多步的基础）列为后续。

### 6.3 何时启用多智能体

- 简单单域请求（如"填一块草地"）→ coordinator 可**直接调用**该域工具，不必委派（避免过度编排，对应 Claude Code"别为单文件读起 subagent"的告诫）。
- 跨域/复杂目标（如"做一个完整主菜单 + 玩家控制 + 测试"）→ 拆给多个专家。
- 是否委派由 coordinator 的 system prompt 给出明确判据。

### 6.4 AgentDefinition：Claude Code 同构 Agent 文件

各 agent 不在代码里硬编码 prompt 和工具清单，而采用与 Claude Code 同构的 **markdown frontmatter + body** 模式：frontmatter 是元数据，markdown 正文是该 agent 的 system prompt。这里的"同构"指数据模型和加载方式一致；运行时仍由本项目的 `QueryEngine`、`delegate`、`agent_stack`、权限闸和 Godot 前端确认承接。

```python
class AgentDefinition(BaseModel):
    name: str
    source: Literal["bundled","user","project","plugin"]
    description: str                         # coordinator 何时委派；等价 Claude Code description
    prompt: str                              # markdown body，作为 agent system prompt
    tools: list[str] | None = None           # None 或 ["*"] 表示当前上下文可见工具
    disallowed_tools: list[str] = []         # 额外 denylist，优先级更高
    skills: list[str] = []                   # agent 启动时预加载的 Skill 名称
    model: str | Literal["inherit"] | None = "inherit"
    effort: Literal["quick","standard","deep","verify","advisor"] = "standard"
    max_turns: int = 12
    memory_scope: Literal["none","session","project","agent"] = "session"
    required_mcp_servers: list[str] = []
    hooks: dict | None = None                # 仅注册本项目支持的内部 Hook
```

Agent 文件示例：

```markdown
---
name: map-agent
description: 适合 TileMap、地形绘制、关卡铺设、瓦片连边相关任务
tools: read_scene_tree, read_class_docs, fill_rect, draw_line, set_cells, clear_rect
model: inherit
effort: standard
skills: tilemap-terrain, godot-tilemap-4x
---

你是 Godot TileMap 领域专家。优先读取当前选中 TileMapLayer 与 tile_catalog，
所有写入必须走地图域工具，不要臆造不存在的 source_id / atlas_coords。
```

落地规则：

- `description` 进入 coordinator 的稳定 prompt，正文 prompt 只在 agent 真正启用时加载，减少常驻 token。
- `tools` 只解析为本项目已注册 `ToolDef` 的交集；`["*"]` 也只是"当前入口/权限模式下可见工具"，不能新增能力。
- `disallowed_tools` 永远优先于 `tools`，并且高风险工具仍按权限闸和前端确认处理。
- `skills` 在 agent 启动时预加载为 user/attachment 消息；若 skill 不存在或被禁用，只写 warning，不让 agent 加载失败。
- `max_turns`、`effort`、`model` 由 `LLMProvider` 执行，但 `QueryEngine` 统一记录每次选择原因，便于 `/doctor` 排查"为什么这轮变慢/变贵"。
- 内置 agent 放 `agents/bundled/*.md`；用户 agent 放用户目录；项目 agent 放工程目录但默认按信任模型启用，不能通过 frontmatter 提权。
- 上面是核心字段；运行时解析还会补 `can_delegate`（仅 coordinator）、`effective_tools`/`warnings`（裁剪后工具集与告警）等——完整数据结构以详设A §2.2 为准，二者同一模型。

### 6.5 Effort、Advisor 与 TaskType

Claude Code 的 `ThinkingConfig/Effort/Advisor` 可迁移为**任务档位**，不照搬其终端交互形态：

| 档位 | 用途 | 行为 |
|------|------|------|
| `quick` | 小范围问答、读状态 | 快模型/低 token；不启子 agent；少检索 |
| `standard` | 默认编辑器任务 | 默认模型；必要时检索/加载 skill；可委派 |
| `deep` | 跨域生成、复杂 bug | 强模型；更高 compact 阈值；允许更多检索和子 agent |
| `verify` | 检查改动、解释报错 | 只读优先；更偏向 `grep/read/run_tests`，写工程工具默认 ask/deny |
| `advisor` | 方案审查/替代路径 | 输出权衡和风险，不直接落地改动，适合"先帮我看看" |

后台任务统一建模为 `TaskType`，而不是把长任务塞进 `/chat` 请求：

```python
TaskType = Literal[
    "main_turn",          # 一次用户消息或 tool_results 回传
    "subagent_turn",      # 子 agent 独立循环
    "run_tests",          # 运行测试/游戏，支持取消与日志
    "index_rebuild",      # RAG 索引构建/增量更新
    "memory_extract",     # 会话 compact 后提取可保存记忆
    "doctor_check"        # 自检任务
]
```

每个 task 记录 `status=pending|running|succeeded|failed|killed`、`started_at`、`last_event_seq`、`cancel_token`；前端只消费状态和日志，取消/重试仍通过服务端统一路由。

---

## 7. 工具系统与注册表

每个工具携带元数据，决定**在哪执行、是否需确认、归哪个 agent、权限默认值**。

```python
# app/tools/registry.py
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

@dataclass
class ToolDef:
    name: str
    domain: str                          # program | map | scene | resource | project | core
    side: Literal["front", "server"]     # front=前端执行；server=本服务执行
    # —— 细化的 effect 元数据（取代单一 mutating，更准确表达工具风险）——
    reads_project: bool = False          # 读工程文件/编辑器状态
    writes_project: bool = False         # 写工程（→ 预览确认 + 可撤销）
    executes_process: bool = False       # 运行游戏/测试（→ 确认 + 超时 + 取消 + 日志 + 沙箱）
    uses_network: bool = False           # 外联（embedding 索引 / 多模态等）
    needs_preview: bool = False          # 是否需前端预览确认
    timeout_ms: Optional[int] = None     # 执行型超时
    is_read_only: bool = False           # 默认 False：工具必须显式声明只读
    is_concurrency_safe: bool = False    # 默认 False：工具必须显式声明可并发
    deferred: bool = False               # True 时不常驻 prompt，由 ToolSearch 按需发现（M2+）
    search_hint: Optional[str] = None    # ToolSearch 关键词/摘要
    render_kind: Optional[str] = None    # 前端预览渲染类型：diff/list/run/log/map...
    path_args: list[str] = field(default_factory=list)  # 哪些入参是路径（供 path_ok 校验）
    schema: dict = field(default_factory=dict)          # OpenAI function schema
    handler: Optional[Callable] = None   # server 工具的实现
    enrich: Optional[Callable] = None    # front 工具的服务端增强（如 read_class_docs 合并 prose）
    permission: str = "auto"             # 默认权限策略键（被权限模式/规则覆盖）

    @property
    def mutating(self) -> bool:          # 兼容旧概念：写工程或执行即"需确认"
        return self.writes_project or self.executes_process

REGISTRY: dict[str, ToolDef] = {}
def register(t: ToolDef): REGISTRY[t.name] = t
def tools_for(agent) -> list:            # 给某 agent 的 OpenAI tools（稳定排序利缓存）
    loaded = [REGISTRY[n] for n in agent.tools if not REGISTRY[n].deferred]
    return [{"type": "function", "function": t.schema}
            for t in sorted(loaded, key=lambda t: t.name) ]
```

- **不提供通用 `bash`/`eval`**：安全边界靠"只有类型化领域工具"来保证（§9）。
- 新增能力域/工具 = 注册一个 `ToolDef` + 归到某 agent，**编排/权限/入口零改动**（NFR-10）。
- **effect 元数据驱动**：`writes_project`/`executes_process` → 默认 `needs_preview`/确认；`path_args` 喂 `path_ok` 校验（§9）；`executes_process` 触发超时/取消/日志/沙箱；`uses_network` 受网络边界与隐私提示约束。
- **fail-closed 默认值**：`is_read_only`、`is_concurrency_safe`、自动授权均默认关闭；忘记声明只会降低性能或多问一次，不会误并发写入或绕过确认。
- **并发安全分区**：同一 assistant turn 内，连续 `is_concurrency_safe=True` 的 server 只读工具可批量并发（如 glob/grep/doc lookup），遇到写工程、执行进程、前端工具或未知工具立即切回串行。
- **ToolSearch / deferred 工具（M2+）**：MCP 工具、低频调试工具、多模态工具不全部常驻 prompt，只暴露名称与 `search_hint`；模型需要时先调用 `search_tools(query)` 取回 schema，避免大工具集破坏 prompt 缓存。

---

## 8. 权限系统

借鉴 Claude Code 的统一权限校验：**每个工具调用在执行/返回前都过权限闸**，产出三态决策。

> 📐 **详细设计见**《多智能体与权限系统详细设计》§3（决策管线优先级、规则引擎、信任模型、ask→确认→可记忆授权、MCP 权限、默认权限基线表）。

### 8.1 决策三态

| 决策 | 含义 | 行为 |
|------|------|------|
| `allow` | 放行 | 直接执行（server）或直接返回前端静默执行（front 只读） |
| `ask` | 需用户确认 | front 改动型 → 返回前端**预览—确认**；用户同意才落地 |
| `deny` | 拒绝 | 不执行，把"被拒绝 + 原因"作为工具结果回传，模型改方案 |

### 8.2 权限模式（会话级，可配置）

| 模式 | 行为 | 场景 |
|------|------|------|
| `default` | 只读 `allow`；改动型 `ask` | 日常 |
| `plan` | 一切改动型 `deny`（只规划不动手），只读 `allow` | 先看方案 |
| `auto_approve` | 信任工程下，改动型也 `allow`（仍可撤销） | 熟练用户批处理 |
| `read_only` | 所有改动型 `deny` | 纯问答/分析、MCP 入口默认 |

### 8.3 规则（覆盖默认）

- 按 **工具名 / 域 / 路径模式** 配 allow/deny 列表（如"禁止改 `addons/` 下文件""允许 map 域自动放行"）。
- 来自 `config/` 的 settings（项目内可放一份，但**不可提升危险权限**，见 §9 信任模型）。

### 8.4 与编排的衔接

```python
# orchestrator 内，分发每个工具调用前：
decision = permissions.check(tool, args, ctx)   # allow | ask | deny
if decision == "deny":
    append_tool_result(call, "被拒绝：<原因>", is_error=True); continue
if tool.side == "server":
    if decision == "allow": run_server_tool()    # ask 对 server 工具罕见，可降级为 allow 或前端弹确认
else:  # front
    front_calls.append((call, decision))          # decision 决定前端是否需要确认 UI
```

- `ask` 的 front 改动型 → 前端渲染 diff/清单等用户确认（这就是 PRD 的预览—确认机制，被纳入统一权限框架）。
- MCP 入口：无前端确认通道，`ask` 默认降级为 `deny`（或由 MCP 客户端自身的确认机制处理）。

---

## 9. 安全边界

借鉴 Claude Code 的沙箱 schema、信任模型、安全/完整配置分离。

> 📐 **详细设计见**《代码检索·Skill·安全边界详细设计》§三（威胁模型、六道边界、`path_ok` 实现、信任模型分层、安全清单）。

### 9.0 传输鉴权（本地 HTTP）

仅绑 `127.0.0.1` **不够**——任何本机进程都能打 `/chat`。落地要求：

- **一次性 token**：Godot 启动服务时生成随机 token，经 **stdin 首行**传给服务（`--token-stdin`，**不入系统进程列表**），前端保存在内存；每个请求带 `Authorization: Bearer <token>`；服务**默认拒绝无 token / token 不符**的请求（401）。
- **Origin / 来源限制**：拒绝带浏览器 `Origin` 头的跨站请求（合法本地客户端无 `Origin`）；不开放宽松 CORS。
- **端口**：随机端口或项目绑定端口，避免固定端口被本机其他程序抢占/探测。
- **生命周期**：token 随服务进程/会话轮换，服务退出即失效。

| 边界 | 策略 |
|------|------|
| **无通用执行** | 不暴露 `bash`/任意代码执行；只类型化领域工具——把可做的事限定在工具集合内 |
| **文件系统范围** | server 工具（检索/读文件）**限定在工程根**，禁止路径穿越（`..`、绝对路径越界一律拒绝），且**只读** |
| **写操作收口** | 所有工程写入都在**前端**经预览确认执行（带 UndoRedo），服务端不直接写工程 |
| **信任模型** | 仅对**受信任工程**启用完整能力；项目内 settings 只能**收紧**权限，**不能提升**危险权限（对应 Claude Code"trust 前只用安全配置子集"） |
| **密钥隔离** | 端点/key/模型仅本地（env/`.env`），不入版本库、不进导出包、不下发前端 |
| **网络** | 工具不做任意外联；仅 LLM 客户端访问用户配置的端点 |
| **配置即 schema** | 用 Pydantic 定义 `SecuritySettings`（allowed_paths、permission_mode、enabled_domains、enabled_tools），宿主读 schema 即知边界（对应 Sandbox `schema 即接口`） |

```python
# app/security/settings.py
class SecuritySettings(BaseModel):
    project_root: str
    permission_mode: Literal["default","plan","auto_approve","read_only"] = "default"
    enabled_domains: list[str] = ["program","map","scene","resource","project"]
    allow_paths: list[str] = []     # 限定可检索/读取的子路径
    deny_paths: list[str] = ["addons/", ".git/"]
```

### 9.1 配置 schema 与 migrations

配置不能只靠"读到什么算什么"。参考 Claude Code 的 settings schema + migration 思路，本项目把配置变更做成可诊断、可回滚的小步迁移：

- **配置来源分层**：`env/.env` 存 LLM endpoint/key；`EditorSettings` 存用户机器级路径、权限模式、输出风格；项目内 `ai_agent.toml` 只允许收紧能力（禁工具、收路径、降权限），不能指定 Python 可执行、API key 或提升 `auto_approve`。
- **schema 统一入口**：`SettingsLoader.load_all()` 返回 `EffectiveSettings`，并附带每个字段的 `source` 和 `is_restricted_by_project`，供 `/doctor` 展示。
- **版本化迁移**：配置文件带 `config_version`；启动时按顺序执行 `migrations/vNN_*.py`，每个 migration 必须幂等、只改一个主题、失败不阻塞启动但写入 `doctor.warnings`。
- **典型迁移**：模型别名改名、权限 key 重命名、旧 skill 目录迁移、RAG 索引 schema bump、输出风格字段从 bool 迁为 `output_style_id`。
- **危险配置不迁入项目**：任何涉及 token、可执行路径、外部 MCP server、自动授权的迁移只能写用户本地配置，不能写进项目目录。

### 9.2 扩展内容安全边界（Agent / Skill / OutputStyle）

采用 Claude Code 同构模式后，Agent、Skill、OutputStyle 都会以 markdown/frontmatter 进入运行时。它们是**提示词与配置资产**，不是授权来源：

- **最高优先级仍是系统安全规则**：系统安全规则 > `ToolDef`/权限闸 > 入口能力边界 > AgentDefinition > Skill/OutputStyle 正文 > 用户输入。
- **来源分层**：`bundled`（内置）/ `user`（用户级）/ `project`（项目级）/ `plugin`（插件级）都保留 `source`，Doctor 展示来源与启用状态；项目级内容在工程未信任前只能收紧能力。
- **工具解析只收敛不扩张**：Agent `tools`、Skill `allowed-tools` 只能从当前入口可见的 `ToolDef` 中取交集；`*` 不代表全局注册表，只代表当前上下文已允许暴露的工具集合。
- **frontmatter 不能提权**：`model`、`effort`、`hooks`、`mcp`、`allowed-tools`、`paths` 等字段都只能影响选择/裁剪/提示词，不允许写入 token、Python 可执行路径、自动授权或外部 server。
- **Hook 同构但受限**：可注册 `PreToolUse`、`PostToolUse`、`SubagentStart` 等本项目内部 hook；不支持任意 shell hook，hook 返回值也不能越过权限闸。
- **路径必须规范化**：Skill `paths`、Agent 文件路径、附带资源路径都做 `realpath`/Windows 规范化，禁止 `..`、绝对路径越界、符号链接逃逸和跨工程触发。
- **提示词注入按不可信处理**：Skill/Agent 正文可以指导模型，但里面若要求"忽略确认/自动改文件/读取密钥/调用未授权工具"，只作为普通文本，最终仍被权限闸拒绝。

---

## 10. 代码检索与 FileState/LSP 子系统

借鉴 Claude Code 的 `Glob`/`Grep` + 检索，做两层（皆 server 工具、限定工程根、只读）：

> 📐 **详细设计见**《代码检索·Skill·安全边界详细设计》§一（工具定义、RAG 切块/索引生命周期、边界限制）。

| 层 | 工具 | 实现 | 用途 | 阶段 |
|----|------|------|------|------|
| **精确** | `list_files(glob)`、`grep_code(pattern)` | ripgrep / glob | 找文件、找符号/字符串（快、确定） | M1 |
| **语义** | `search_codebase(query)` | embedding + 向量库（FAISS/Chroma） | "找和 X 相关的代码"（RAG） | M2 |

- **索引生命周期**（语义层）：首次打开工程构建；文件变更增量更新；可手动重建（§13 待定细化）。
- 检索结果只返回**片段 + 路径 + 行号**，不全量灌入（控成本，呼应渐进披露）。
- 严格限定 `project_root` 与 `deny_paths`（§9）。

### 10.1 FileStateCache：先读后写

Claude Code 的文件工具有一个重要约束：**不能基于过期或局部视图写文件**。本项目虽然由前端最终落地写入，服务端仍要维护 `FileStateCache`，用于模型上下文与写入请求的安全校验：

```python
class FileState(BaseModel):
    path: str
    content_hash: str
    mtime_ns: int
    full_read: bool
    line_start: int | None = None
    line_end: int | None = None
    is_partial_view: bool = False
    last_used_at: datetime
```

- `read_file`、`grep_code`、`search_codebase` 都更新 FileState；grep/RAG 片段标 `is_partial_view=True`。
- 任何 `propose_script_edit` / `replace_file_text` 类工具在生成 diff 前，必须确认该文件已被**完整读取**；只有片段视图时先要求模型读完整文件。
- 前端回传实际写入结果时带 `before_hash/after_hash/mtime_ns`；服务端更新 FileState，并把旧快照从可写依据中失效。
- compact 后保留最近文件的摘要和 hash，但不保留可写资格；下次写入前仍需重新验证。

### 10.2 LSP 与诊断

GDScript/C# 的语义能力优先使用 Godot 编辑器或 LSP 暴露的事实，不让模型猜：

- `lsp/` 作为可选子系统：可接 Godot LSP、C# LSP 或前端转发的诊断；未启用时降级为 grep/ClassDB/运行日志。
- 对外统一 `Diagnostic` DTO：`path`、`line`、`column`、`severity`、`code`、`message`、`source`；API 层使用 1-based 行列，内部转换集中处理。
- 诊断做 LRU 限流：每轮只注入相关文件的最高价值 N 条，避免把整项目 warning 灌进上下文。
- 文件写入后清理该文件旧诊断，等待前端/LSP 新一轮刷新；过期诊断不能作为最终判断依据。

---

## 11. Skill 系统（Claude Code 同构）

采用 Claude Code 同构的 Skill 模式：Skill 本质上是一个 **PromptCommand**，模型在 system prompt 中先看到可用 Skill 的简述，需要时再通过 `load_skill(name)` / `SkillTool` 读取全文。

> 📐 **详细设计见**《代码检索·Skill·安全边界详细设计》§二（SKILL.md 结构、发现/加载、`load_skill`、内置清单、安全防注入提权）。

### 11.1 结构

```
skills/
├── bundled/                         # 内置 Skill（随服务发布）
│   ├── gdscript-4x-idioms/SKILL.md
│   ├── tilemap-terrain/SKILL.md
│   ├── scene-composition/SKILL.md
│   └── csharp-godot-dotnet/SKILL.md
├── user/                            # 用户级 Skill
├── project/                         # 项目级 Skill（受信任模型约束）
└── plugin/                          # 插件级 Skill（后续）
```

每个 Skill 严格采用目录格式，入口文件必须叫 `SKILL.md`：

```markdown
---
name: tilemap-terrain
description: Godot TileMapLayer 地形铺设、连边和 atlas 坐标选择指南
when_to_use: 用户要生成地图、铺瓦片、修改 TileMapLayer 或解释瓦片规则时使用
allowed-tools: read_scene_tree, read_class_docs, fill_rect, draw_line, set_cells, clear_rect
paths:
  - "**/*.tscn"
  - "**/*.tres"
---

这里写完整技能说明、示例、注意事项。
```

字段规则：

- `description` / `when_to_use` 进入稳定的 Skill 列表，帮助模型决定何时调用。
- `allowed-tools` 只裁剪当前可用工具集合，不能新增能力；不填则不改变工具集合。
- `paths` 用于条件激活和动态发现，所有路径先按 §9.2 规范化。
- `model` / `effort` 可作为建议，但受会话级设置和全局成本上限约束。
- `hooks` 可声明本项目支持的内部 hook；不支持任意 shell hook。

### 11.2 渐进式加载

- 启动时扫描 `skills/`，只把 `name + description + when_to_use` 注入 system prompt（占用极小）。
- 任务相关时，agent 调用 `load_skill(name)`（server 只读工具）读取**全文**注入上下文——只在需要时付出 token。
- 模型接触某个路径时触发动态发现：沿路径向上查找本项目 skill 目录，发现新 Skill 后清理 Skill 列表缓存。
- Skill 名称带来源 namespace（如 `bundled:tilemap-terrain`、`project:tilemap-terrain`），UI 可显示短名；内部用规范名避免同名覆盖。

### 11.3 与多智能体

- skill 可**按 agent 绑定**：map-agent 默认带 `tilemap-terrain`，programming-agent 默认带 `gdscript-4x-idioms`/`csharp-godot-dotnet`。
- Agent frontmatter 中的 `skills` 字段会在子 agent 启动时预加载对应 Skill 内容，等价 Claude Code 的 agent skill preload。
- Skill 与 Command 同构：用户可手动通过 `/skill <name>` 或命令面板加载；模型也可通过 `load_skill` 自主加载。

---

## 12. 文档接地（混合：前端签名 + 服务端 prose）

（沿用 v0.2，纳入新结构。）`read_class_docs` 是 **「前端只读工具 + 服务端增强」**：

- **签名来自前端 ClassDB**：方法/属性/信号/枚举/继承，100% 等于用户引擎版本，且含自定义类/GDExtension/插件 API。
- **prose 来自服务端 doc dump**：按引擎版本内置官方文档描述。
- **合并**：模型调用 → 前端查 ClassDB 返签名 → 服务端 `enrich` 钩子按 `class_name` 从版本匹配 dump 取 prose 合并 → 喂回模型。

```python
# app/docs/class_docs.py
# 前端返回的是结构化签名 dict（含 source 字段），enrich 以结构化方式合并，不做字符串拼接
def enrich_class_docs(args: dict, front_result: dict) -> dict:
    if front_result.get("source") != "ClassDB":     # script_class / unknown：无官方文档，不补 prose
        return front_result
    prose = DOC_DUMP.lookup(front_result.get("name", ""))   # 版本匹配
    if prose:
        front_result["prose"] = prose
    return front_result
```

- 版本对齐靠请求里的 `engine_version`；自定义类只给签名、不杜撰 prose（防幻觉）。

---

## 13. Agent 编排循环（整合权限 + 多智能体）

实现上不把所有逻辑塞进一个 `step()`：参考 Claude Code 的 QueryEngine/query 内核拆法，分成两层。

| 层 | 职责 | 生命周期 |
|------|------|------|
| `QueryEngine` 门面 | 持有会话级状态：`messages`、`agent_stack`、`permission_denials`、cache latch、compact 告警；负责 transcript 两段写、SDK/HTTP/MCP 事件翻译 | 一个 `session_id` 一个实例或一份持久化状态 |
| `query_loop` 内核 | 无状态/少状态的单 turn 运行器：构建 prompt → 调 LLM → 处理 tool_calls → 产出事件 → 根据 `transition.reason` 继续或退出 | 每次用户消息或工具结果回传启动一轮 |

单轮工作流：

1. `QueryEngine.submit_user_turn()` 接收用户消息或前端 `tool_results`，先写入本地 transcript，防止进程中途退出后无法 resume。
2. `PromptBuilder` 按 §16 组装稳定 system、工具 schema、常驻 skill 简述、动态编辑器上下文、历史消息。
3. `compact.preflight()` 根据 token 压力执行 microcompact 或 full compact；compact 成功后发 `compact_boundary` 事件并清理旧消息。
4. `query_loop` 调 `LLMProvider.chat()`；重试/限流/模型降级通过事件向上报告，不直接写 UI。
5. 收到 `tool_calls` 后先走权限闸；server 只读并发安全工具可并发执行，front 工具挂起并返回 Godot。
6. 工具结果 append 后，以 `transition.reason="next_turn"` 继续；用户拒绝、权限 deny、compact、max-turn、错误恢复都有独立 reason，便于日志定位。

```python
# app/orchestrator/agent.py（简化）
def step(session, llm, ctx) -> dict:
    frame = session.top_frame()
    while True:
        resp = llm.chat(frame.messages, tools_for(frame.agent), model=frame.agent.model)
        msg = resp.choices[0].message
        frame.messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return finish_frame(session, msg.content)   # 弹栈或 final

        # 约定：delegate 必须是本轮“唯一”的 tool call（不与其他并列），见 §13.1
        if any(c.function.name == "delegate" for c in msg.tool_calls):
            call = msg.tool_calls[0]
            if len(msg.tool_calls) != 1:
                append_tool_result(frame, call, "delegate 必须单独调用，请重试", is_error=True)
                continue
            args = json.loads(call.function.arguments or "{}")
            child = session.push_frame(make_agent(args["agent"]), args["task"])
            child.pending_delegate_call_id = call.id     # 子 agent 结束后用它回答父帧的 delegate
            frame = child
            continue

        front_calls = []
        turn_id = session.new_turn_id(frame)             # 每轮一个 turn_id（幂等，§14.1）
        for call in msg.tool_calls:
            tool = REGISTRY[call.function.name]
            args = json.loads(call.function.arguments or "{}")
            decision = permissions.check(tool, args, ctx)       # allow | ask | deny
            if decision == "deny":
                append_tool_result(frame, call, "被拒绝：<原因>", is_error=True); continue
            if tool.side == "server":
                append_tool_result(frame, call, tool.handler(args))
            else:
                front_calls.append({"id": call.id, "name": tool.name, "input": args,
                                    "needs_confirm": decision == "ask", "frame_id": frame.id,
                                    "agent": frame.agent.name, "render_kind": tool.render_kind})
        if front_calls:
            session.set_pending(turn_id, [c["id"] for c in front_calls])   # 记录待回应（§14.1）
            return {"type": "tool_calls", "turn_id": turn_id, "calls": front_calls}

def finish_frame(session, text) -> dict:
    done = session.pop()
    parent = session.top_frame()
    if parent is None:
        return {"type": "final", "text": text}
    append_tool_result(parent, done.pending_delegate_call_id, summarize(text))  # 回答父帧 delegate
    return step(session, ...)                                              # 继续父帧
```

前端回传结果 → 校验 `turn_id` / `pending_tool_call_ids`（幂等，§14.1）→ 按 `frame_id` 路由、`enrich` 增强后 append → 继续 `step`（见 §12、§8、§14.1）。

内部事件类型（M0 可只记录日志，M2 可通过 `/chat/events` 或 SSE 暴露给前端）：

| 事件 | 用途 |
|------|------|
| `progress` | 当前阶段：构建 prompt、调用模型、执行工具、等待确认、压缩上下文 |
| `stream_event` | 模型流式文本/usage/stop_reason；M0 可折叠为最终 `text` |
| `tool_use_summary` | 工具执行开始/结束、并发批次、耗时、失败原因 |
| `compact_boundary` | 上下文已压缩，前端可在时间线标记"已整理早期上下文" |
| `permission_denied` | 权限拒绝记录，随最终结果进入审计 |

### 13.1 delegate 协议约定

OpenAI tool calling 要求一条 assistant 消息里**所有** tool_calls 都被回应后才能进入下一轮。为与多智能体兼容：

- **`delegate` 必须是该 assistant turn 唯一的 tool call**，不能与其他工具并列（违反则回 `is_error` 让模型重试）。
- 父帧那条 `delegate` 调用**保持 pending**，直到子 agent 结束——此时把子 agent 的**摘要**作为该 `delegate` 的 tool result 回答父帧；期间父帧不再被调用 LLM，OpenAI 不变量不破坏。
- 子 agent 自身的 tool_calls 在**子帧内**闭环回应，互不串扰。
- （等价替代：把 `delegate` 实现为"同步跑完子 agent 再 append 单个 tool result"的 server 工具；但子 agent 若需前端确认会挂起，故采用上面的"单独调用 + pending"模型。）

---

## 14. HTTP 接口规格

### POST `/chat`

```jsonc
{
  "session_id": "uuid",
  "request_id": "r-uuid",                           // 幂等键（§14.1）
  "user_message": "做一个主菜单并给玩家加二段跳",   // 二选一
  "context": {                                      // 结构化，不再是大字符串
    "selection":      { /* 选中节点/脚本 */ },
    "scene_tree":     { /* 当前场景结构 */ },
    "tile_catalog":   [ /* 合法瓦片 */ ],
    "project_files":  [ /* 相关文件清单 */ ],
    "debugger_errors":[ /* 运行时报错 */ ]
  },
  "language_hint": "gdscript",                      // gdscript|csharp
  "engine_version": "4.4.1",                        // 选 doc dump（§12）
  "permission_mode": "default",                     // default|plan|auto_approve|read_only
  "effort": "standard",                             // quick|standard|deep|verify|advisor（§6.5）
  "output_style": "editor_concise",                 // 输出风格 id（§16.1）
  "tool_results": [                                 // 二选一：前端回传
    {"tool_use_id":"call_x","frame_id":"f1","turn_id":"t11",
     "status":"applied",                            // applied|rejected|error
     "result": { /* JSON 值，非裸字符串 */ },
     "error_code": null, "artifact_refs": ["res://..."]}
  ]
}
```

响应（三态）：

```jsonc
{"type":"tool_calls","turn_id":"t12","text":"…","calls":[
  {"id":"call_x","name":"propose_script_edit","input":{...},
   "needs_confirm":true,"frame_id":"f1","agent":"programming-agent","render_kind":"diff"}]}
{"type":"final","text":"主菜单与二段跳已完成。"}
{"type":"error","text":"端点调用失败：401，请检查 key。"}
```

M0/M1 的 `/chat` 可以继续返回上述三态；Agent 内核产生的 `progress`、`stream_event`、`compact_boundary` 等事件先写入本地 `events` 日志。M2 若需要更强的"自主多步进度展示"，再加只读事件通道：

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/chat/events?session_id=...&after=seq` | 长轮询或 SSE；按递增 `seq` 返回内部事件，断线后可从 `after` 续拉 |

事件通道只承载可展示状态，不承载工具确认结果；工具确认仍通过 `/chat` + `tool_results` 闭环，避免把权限决策分裂到两条写路径。

DTO（节选）：

```python
from typing import Literal, Any

class ToolResult(BaseModel):
    tool_use_id: str
    frame_id: str
    turn_id: str
    status: Literal["applied", "rejected", "error"]
    result: Any | None = None            # JSON 值（dict/list/str/...），非裸字符串
    error_code: str | None = None
    artifact_refs: list[str] = []        # 落地产物引用（res:// 路径等）
    grant_session_allow: bool = False    # "总是允许"授权升级（粒度=tool+domain+path+effect；高风险工具忽略，详设 §3.6）

class Context(BaseModel):                # 结构化上下文
    selection: dict | None = None
    scene_tree: dict | None = None
    tile_catalog: list | None = None
    project_files: list | None = None
    debugger_errors: list | None = None
    dotnet_enabled: bool = False         # 前端检测 .csproj；服务端据此决定 C# 工具暴露（PRD D2）

class ChatRequest(BaseModel):
    session_id: str
    request_id: str | None = None        # 幂等键
    user_message: str | None = None
    context: Context | None = None
    language_hint: str | None = None
    engine_version: str | None = None
    permission_mode: str | None = None
    effort: str | None = None            # quick|standard|deep|verify|advisor
    output_style: str | None = None      # OutputStyle id
    tool_results: list[ToolResult] | None = None
```

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | `{ok, model, endpoint_reachable, function_calling_supported}` |
| POST | `/reset` | body `{"session_id": "..."}`；清空该会话（含 agent 栈与本地持久化记录） |
| GET | `/skills` | 列出已发现 skill（name/description） |
| GET | `/doctor` | 返回 Python、鉴权、LLM 能力、MCP、索引、LSP、权限、上下文体积、配置迁移状态 |
| GET | `/commands` | 返回可用 slash/editor command 清单（含参数 schema 与是否需要确认） |
| POST | `/commands/{name}` | 执行服务端命令，如 `compact`、`rebuild_index`、`set_effort`、`set_output_style` |
| GET/POST | `/memory` | 查询/清理/保存本地记忆；默认不跨项目共享 |
| GET | `/recovery-pointer?project_root=...` | 读取最小恢复指针，用于前端重启后提示是否恢复会话 |

### MCP 入口

- `ListTools` → **默认只列不依赖编辑器在线的工具**（文件检索、静态分析、文档 prose）；editor-state / 改动型工具仅在 Godot 在线时列出。
- `CallTool` → 路由到同一 `tool.handler` / 同一权限闸（简化上下文，`ask` 降级 `deny`）。

### 14.1 幂等与并发控制

跨进程恢复必须防"重复提交/连点/重试"把 agent 栈搞乱：

- **per-session 锁**：同一 `session_id` 的请求串行处理；并发请求排队或拒绝（409）。
- **`turn_id`**：每产生一批待前端执行的工具调用，服务返回一个 `turn_id` 并记录其 `pending_tool_call_ids`。
- **回传校验**：前端 `tool_results` 必须带匹配的 `turn_id`，且 `tool_use_id` ∈ 该轮 pending 集；**不匹配/重复一律忽略**（幂等），避免错位 append。
- **`request_id`**：用户消息级幂等键；重复提交同一 `request_id` 直接返回上次结果，不重复跑。

### 14.2 会话持久化（本地）

- 会话的 `messages`、`agent_stack`、各帧 `pending`、`turn` 记录**持久化到服务端本地缓存目录**（按 `session_id`，JSON 或 SQLite）。
- 用途：**服务重启后可恢复**进行中的会话；本地**审计/回溯**。
- **仅本地存储、不外传**（呼应隐私边界，PRD NFR-12）；提供保留期/清理与"清空会话"。

### 14.3 最小恢复指针

完整远程 Bridge 不适合 v1，但 Claude Code 的"恢复指针"很适合本项目：前端/服务重启时只保存**足够找到本地会话**的最小信息。

```jsonc
{
  "schema": 1,
  "project_root_hash": "sha256...",
  "session_id": "uuid",
  "last_event_seq": 42,
  "pending_turn_id": "t12",
  "updated_at": "2026-06-12T10:00:00Z"
}
```

- 指针只保存 `session_id`、`last_event_seq`、`pending_turn_id`、时间戳和工程 hash；**不保存 token、API key、绝对隐私文本或完整消息**。
- 启动时若指针未过期且服务端本地会话存在，前端提示"恢复上次 Agent 会话"；否则清理指针。
- 一次性 HTTP token 仍由本次 Godot 进程重新生成；恢复的是会话状态，不是旧鉴权。

---

## 15. LLM Provider 抽象与 OpenAI 用法要点

LLM 访问统一走 `LLMProvider` 接口，**默认实现为 OpenAI 兼容 Chat Completions**；后续可加 Responses / Anthropic / Gemini provider 而不动编排层。

```python
class LLMProvider(Protocol):
    def chat(self, messages: list, tools: list, model: str | None) -> "AssistantTurn": ...
    @property
    def supports_tool_calling(self) -> bool: ...
    @property
    def supports_prompt_cache(self) -> bool: ...   # OpenAI 端点可利用；其余按能力探测
```

默认 OpenAI 兼容实现要点：

- `OpenAI(base_url=..., api_key=...)`；`chat.completions.create(model, messages, tools, tool_choice="auto")`。
- 工具：`{"type":"function","function":{name, description, parameters(JSON schema)}}`。
- 读取：`resp.choices[0].message.tool_calls`，每项 `{id, function:{name, arguments(JSON 串)}}`；`arguments` 一律 `json.loads`。
- 回传：`{"role":"tool","tool_call_id":id,"content":result}`；前一条 assistant（含 tool_calls）必须保留，且其所有 tool_calls 在下次请求前被回答齐。
- 多智能体下：每个 agent 帧是**独立 messages**，各自调用 `chat`（可不同 `model`）。
- 流式（可选/后置）：`stream=True`；工具循环用非流式即可，UI 用"思考/执行"状态。

### 15.1 重试、限流与请求分级

LLM 调用统一包一层 `with_retry()`，不要让编排层散落 SDK 重试逻辑：

- **尊重 `Retry-After`**：429/529/5xx 按服务端提示等待；没有提示时指数退避 + jitter。
- **区分前台/后台**：用户正在等的 `main_turn` 重试次数少、尽快给可解释错误；`index_rebuild`、`memory_extract`、`doctor_check` 可后台慢重试。
- **可降级模型**：`standard/deep` 可配置 fallback model；降级必须发 `progress` 事件并写入 transcript 元数据。
- **流式事件折叠**：streaming 文本按约 100ms 或句子边界聚合为 `stream_event`，避免前端事件风暴。
- **幂等关联**：每次 LLM 请求记录 `request_id`、`provider_request_id`、`session_id`、`turn_id`，用于日志回放和限流排查。

---

## 16. Prompt 缓存与上下文管理

### 16.1 Prompt 调用链

`PromptBuilder.build(frame, ctx)` 统一组装提示词，不让各 agent 临时拼字符串：

1. **静态 system section**：核心身份、工具使用规则、权限规则摘要、输出约束；session 内只计算一次。
2. **工具 schema**：按工具名稳定排序；`deferred=True` 的工具不进入常驻 schema，只进入 ToolSearch 名单。
3. **agent section**：当前 agent 的角色、可用工具子集、默认 skill 简述。
4. **动态 context section**：本轮 Godot 编辑器快照（选中节点、场景树邻域、tile_catalog、debugger_errors）。
5. **output style section**：用户选择的输出风格（如 `editor_concise`、`review_findings_first`、`tutorial`），作为 system 末尾稳定附加段。
6. **messages**：会话历史、最近工具结果、compact summary、用户当前输入。

实现约束：

- 稳定前缀在前：`system` → `tools` → 常驻 skill 简述；易变编辑器状态与检索结果放后面。**Prompt caching 是 OpenAI 端点可利用的优化，不假定所有 `base_url` 支持**（由 `LLMProvider.supports_prompt_cache` 决定）；即使 provider 不支持缓存，稳定前缀也利于一致性。
- `systemPromptSection()` 默认缓存；任何每轮重算的 section 必须走 `uncached_section(name, reason)`，并写明为什么不能缓存，避免无意破坏缓存前缀。
- session 开始后锁存影响缓存键的配置：模型、工具 schema、prompt-cache 能力、feature/header 类开关；`/reset` 或 full compact 后重新开始一段缓存前缀。
- 子 agent/fork agent 复用父帧已渲染的 system/prompt 参数，不重新生成等价文本；否则 feature gate 或配置热更新可能造成字节不一致。
- OutputStyle 用 Markdown/YAML 定义，只允许改变**表达方式**（篇幅、结构、语言偏好），不能改变权限、工具、路径边界；启用/切换通过 `/commands/set_output_style` 记录在会话元数据里。

### 16.2 上下文压缩梯度

多智能体天然控上下文：每个子 agent 只带本域工具/skill/文档，单帧上下文小。但长会话仍需要分层压缩：

| 压力 | 动作 | 说明 |
|------|------|------|
| 低 | 保留原文 | 最近用户目标、最近工具结果、待确认工具调用不压缩 |
| 中 | Microcompact | 清理较旧的大型工具结果，保留 tool_use_id、路径、摘要与 artifact_refs |
| 高 | Full compact | 用模型总结早期对话，剥离推理草稿，只把 summary 注入后续 messages |
| 失败 | 熔断 | 连续 compact 失败达到阈值后停止自动 compact，返回可解释错误，避免每轮重复烧请求 |

compact 后必须重建必要上下文：

- 恢复最近读过/改过的文件摘要（最多 N 个、每个限 token）。
- 恢复已加载 skill 的摘要或短描述，不全量回灌。
- 保留 `agent_stack`、`pending_delegate_call_id`、`turn_id`、`pending_tool_call_ids`，不能让 compact 破坏挂起工具的 resume。
- 对被截断的文件快照标 `is_partial_view=True`；任何写入前必须要求模型重新读取完整内容，不能基于 partial view 编辑。

### 16.3 工具结果后注入

工具执行完成后，下一轮 LLM 调用前可以追加附件消息：

- `tool_result`：真实 JSON 结果或错误，必须满足 OpenAI tool calling 的配对不变量。
- `attachment`：检索片段、ClassDB prose、compact summary、权限拒绝摘要等非工具协议内容。
- `progress`：只给前端/日志消费，不进入模型上下文，避免状态文案污染 prompt。

### 16.4 Memory 子系统

Memory 与 compact 分开：compact 是为了让当前会话继续跑，memory 是为了下次任务更懂这个项目。参考 Claude Code 的多层记忆，但收敛成四类：

| 类型 | 来源 | 注入策略 |
|------|------|------|
| `project` | 用户确认的项目约定、架构决策、Godot 版本/插件规则 | 按相关性检索注入，不全文常驻 |
| `session` | 当前会话 compact 摘要、未完成任务、最近文件事实 | 当前 session 内优先注入 |
| `agent` | 某 agent 的领域经验，如地图生成偏好 | 只给对应 agent |
| `feedback` | 用户明确纠正，如"不要改 addons" | 高优先级短句，仍不能提权 |

落地规则：

- 自动提取只生成候选，默认需要用户确认或显式命令保存；敏感信息、API key、个人路径不保存。
- `RelevantMemorySelector` 根据当前用户输入、工程路径、agent、最近文件 hash 检索 Top-N；不把记忆库全文塞进 system。
- full compact 后可触发 `memory_extract` 后台任务，提取"可复用事实"和"仅本会话事实"，二者分开存。
- `/memory` 与前端面板提供查看、删除、禁用项目记忆的入口。

---

## 17. 错误处理与降级

| 场景 | 处理 |
|------|------|
| 端点不可达 / 401 / 限流 | SDK 重试；失败 → `{"type":"error"}`，不抛 500 |
| 端点不支持 function calling | 能力探测拦截，前端提示更换模型 |
| 工具被权限 `deny` / 模型产非法参数 / 用户拒绝 | 作为 `tool` 结果（`is_error`）回传，模型修正 |
| 子 agent 失败 | 作为 `delegate` 结果回传 coordinator，由其决定重试/换策略 |
| 会话/栈过大 | 按 §16 执行 microcompact/full compact；仍失败则阻塞并提示用户 `/reset` 或缩小任务 |
| compact 连续失败 | 熔断自动 compact，避免每轮重复请求；保留最近错误与 token 压力状态 |
| 流式/重试中断 | 丢弃未确认的流式工具结果；只 append 已完成且通过 pending 校验的 tool result |

---

## 18. 可扩展性

- **新工具/域** = 注册 `ToolDef` + 绑定 agent；编排/权限/入口零改动。
- **新 agent** = 新增 markdown `AgentDefinition`（frontmatter + body + 预加载 skills + 模型/effort/max_turns）。
- **新 skill** = 丢一个 `SKILL.md` 目录即被发现，并被转换为 PromptCommand/SkillTool 可调用能力。
- **新入口** = 复用同一 `tools/permissions/skills`（已示范 HTTP/MCP）。

### 18.1 Command 系统

把常用操作注册为 typed command，供 slash 命令、编辑器按钮和 MCP command 复用：

| 命令 | 行为 |
|------|------|
| `/compact` | 触发当前 session full compact，保留 pending 与 FileState hash |
| `/doctor` | 返回自检报告 |
| `/reset` | 清空指定 session |
| `/permissions` | 查看/切换会话权限模式 |
| `/index rebuild` | 重建 RAG 索引 |
| `/effort quick|standard|deep|verify|advisor` | 切换本 session effort |
| `/output-style <id>` | 切换 OutputStyle |
| `/memory list|save|delete` | 管理本地记忆 |

command 只调用服务端已注册能力，不开放任意 shell；每个 command 声明参数 schema、是否需要确认、是否可在 MCP 入口使用。

Skill 与 Command 同构：Skill 以 `type=prompt` 的 command 进入命令系统；命令面板和模型 `load_skill` 看到的是同一份 Skill 注册表，只是触发方式不同。

### 18.2 Hook 系统

Hook 只作为内部扩展点与审计点，v1 不提供通用 shell hook：

| 事件 | 用途 |
|------|------|
| `SessionStart` / `SessionStop` | 初始化/清理会话资源 |
| `UserPromptSubmit` | 记录用户请求元数据、选择 effort |
| `PreToolUse` / `PostToolUse` | 权限、审计、耗时统计 |
| `PermissionRequest` / `PermissionDenied` | 写入审计与 UI 事件 |
| `PreCompact` / `PostCompact` | compact 前后保存摘要和 pending |
| `SubagentStart` / `SubagentStop` | 子 agent 生命周期统计 |
| `ConfigChange` / `MigrationComplete` | 配置热更新与 Doctor warning |
| `FileStateChanged` / `DiagnosticsChanged` | FileState/LSP 刷新 |
| `AgentLoaded` / `SkillLoaded` | 记录 markdown frontmatter 解析、来源、禁用字段与实际工具集合 |

Hook handler 类型限制为 Python 内部回调、受信任插件回调或本地 HTTP 回调；返回值不能直接越过权限闸。

### 18.3 Doctor 自检

Doctor 是一个可视化事实面板的后端数据源，避免用户/开发者靠猜：

- 安装：Python 可执行、依赖版本、服务端口、token 是否启用。
- LLM：endpoint 可达、模型、tool calling、prompt cache 能力、最近限流/降级记录。
- 工程：Godot 版本、doc dump 版本、ClassDB 签名来源、`.NET` 检测。
- 能力：工具注册表、MCP 连接状态、RAG 索引状态、LSP/诊断状态、Memory 状态。
- 扩展：Agent/Skill/OutputStyle 的来源、frontmatter 校验结果、被忽略字段、实际生效工具集合。
- 安全：有效权限模式、项目配置是否只收紧、deny_paths、最近被拒绝工具。
- 上下文：当前 session token 压力、compact 状态、pending turn、恢复指针是否有效。

---

## 19. 与里程碑对应

| 阶段 | 本服务交付 |
|------|-----------|
| M0 骨架 | HTTP `/chat` + `QueryEngine` 最小门面 + 单 agent `query_loop` + 工具注册表 + **权限闸(default 模式)** + 安全边界(项目根/无 shell) + 1 个最小工具 + `/health`/基础 `/doctor` |
| M1 全域工具 | 各域工具；精确检索(glob/grep)；混合文档接地；语言约束；PromptBuilder 分层；OutputStyle；Skill 简述常驻 + `load_skill`；FileStateCache；配置 schema + migrations；基础 Agent/Skill markdown loader |
| M2 增强 | 语义检索(RAG)；调试错误/单测协议；LSP/诊断；microcompact/full compact；Memory；权限规则与模式完善；MCP 入口与连接治理；事件通道 `/chat/events`；ToolSearch/deferred tools；Command 系统；动态 Skill 发现与缓存失效 |
| M3 创新 | **多智能体 coordinator+专家**全量、Claude Code 同构 AgentDefinition 用户扩展、TaskType 后台任务、并行子 agent、多模态(草图→关卡/看懂资产)、内容生成；Advisor；Prompt cache break 检测；Hook 扩展；最小恢复指针 |

> 多智能体最小形态（串行委派）可在 M2 末引入；并行与自主多步在 M3。

---

## 20. 风险与未决

| 项 | 说明 |
|----|------|
| 多智能体 × 跨进程预览确认 | agent 帧栈挂起/resume 较复杂；v1 限串行、限委派深度 |
| 端点 function calling 兼容性 | 本地/小模型支持参差；能力探测 + 降级提示 |
| 权限规则与 PRD 预览确认的统一表述 | 已统一为权限三态；需与前端 UI 对齐 `needs_confirm` 语义 |
| MCP 入口能力边界 | Godot 离线时拿不到 editor-state（场景树/选中/ClassDB），改动也无法落地；**MCP 默认仅文件检索/静态分析**，editor-state/改动型需 Godot 在线（§5、§14） |
| 本地 HTTP 越权访问 | 任意本机进程可打 `/chat`；用一次性 token + Origin 限制 + 随机端口缓解（§9.0） |
| LLM provider 差异 | 不同端点 tool calling / 缓存能力参差；用 `LLMProvider` 抽象 + 能力探测，默认 OpenAI 兼容（§15） |
| 会话恢复一致性 | 重启恢复 / 并发回传错位；用本地持久化 + per-session 锁 + `turn_id`/pending 校验（§14.1/14.2） |
| Agent 主循环职责膨胀 | 已拆为 `QueryEngine` 门面、`query_loop` 内核、PromptBuilder、compact、hooks；防止所有逻辑堆进一个 `step()`（§13/§16） |
| Prompt cache 被动态上下文破坏 | system/tools/skill 稳定前缀与动态 context 分区；uncached section 必须写 reason；session latch 锁存影响缓存键的配置（§16.1） |
| compact 破坏挂起工具/文件认知 | compact 后保留 `turn_id`/pending/agent_stack；partial view 禁止直接写入，必须完整读取（§16.2） |
| 配置迁移误提权 | migrations 只能幂等小步，危险配置只能写用户本地；项目配置不能迁入 token/可执行路径/自动授权（§9.1） |
| Memory 误存隐私或污染上下文 | 自动提取只出候选；敏感信息不保存；按相关性 Top-N 注入，不全文常驻（§16.4） |
| LSP/诊断过期 | 写文件后清理旧诊断；诊断带时间/来源；只注入相关文件 Top-N（§10.2） |
| Command/Hook 变成绕权入口 | command 只调用 typed 能力；hook v1 不开放 shell，返回值不能越过权限闸（§18.1/§18.2） |
| 恢复指针泄露或误恢复 | 指针只存 session_id/seq/pending/time/hash，不存 token 和消息；过期/工程不匹配即清理（§14.3） |
| Agent/Skill 同构后误以为可提权 | Agent/Skill/OutputStyle 是 prompt/配置资产，不是授权来源；`tools`/`allowed-tools` 只取当前可见 `ToolDef` 交集，`*` 不越过入口和权限边界（§9.2、§11） |
| 项目级 Skill/Agent 覆盖用户预期 | 所有扩展带 `source` 与 namespace；Doctor 展示实际启用来源；项目级内容未信任前只能收紧能力，不能提升权限（§9.2、§11） |
| 条件 Skill 路径逃逸 | `paths`、资源路径和动态发现路径统一 realpath/Windows 规范化，禁止 `..`、绝对路径越界、符号链接逃逸（§9.2） |
| 文档接地 doc dump | 版本管理与打包策略待定（§12） |
| 语义检索索引 | 构建时机/增量/存储位置待定（§10） |
| 多模态端点 | 草图→关卡/看懂资产需端点支持图像输入，需探测 |
| Skill 信任 | 第三方 skill 是注入到 prompt 的指令，需作为不可信内容对待，避免 prompt 注入越权（与 §9 信任模型一致） |
