# Python LLM 服务架构方案（OpenAI SDK · 多智能体 · 借鉴 Claude Code）

| 项目 | 内容 |
|------|------|
| 文档名称 | AI 游戏开发 Agent —— Python 服务架构方案 |
| 版本 | v0.3 |
| 日期 | 2026-06-05 |
| 依据 | 《Godot 内嵌 AI 游戏开发 Agent 需求文档》v0.7；借鉴《深入 Claude Code 源码 · 第 1 章》架构 |
| 范围 | **Python LLM 服务（Agent 层）**；前端（GDScript 插件）的工具执行与预览 UI 不在本文，但定义二者协议 |
| 变更 | v0.3：引入多入口（HTTP+MCP）、多智能体（coordinator+专家）、权限系统、安全边界、代码检索、Skill 系统；保留 v0.2 的混合文档接地 |

---

## 1. 目标与依据

本服务是三层架构中的 **Agent 层**（需求文档 §4.1）。在 v0.3，它从"单循环 Agent 服务"升级为一个**小型 Agent 运行时**，借鉴 Claude Code 的几条核心架构经验：

| Claude Code 模式 | 本服务的对应落地 |
|------|------|
| 一份源码、四种入口（CLI/SDK/MCP/Sandbox） | **一套工具/权限/skill，两个入口（HTTP for Godot、MCP for 外部 AI 客户端）** |
| 工具调用统一过 `hasPermissionsToUseTool` | **统一权限闸**：每个工具调用先过 `check_permission` |
| 专用工具 vs 通用 bash 的取舍 | **全专用工具、不暴露通用 shell**，把安全边界焊进工具形状 |
| `coordinator/` + `tasks/` + `AgentTool` 多 Agent | **coordinator + 领域专家子 agent**，上下文隔离、可委派 |
| `skills/` 渐进式技能 | **Skill 系统**：简述常驻、全文按需加载 |
| `Glob`/`Grep` + 检索 | **代码检索子系统**：精确(Glob/Grep) + 语义(RAG) |
| Sandbox schema、trust dialog、安全/完整环境变量分离 | **安全边界**：项目根约束、信任模型、密钥隔离、settings schema |

---

## 2. 设计原则

1. **一份能力，多入口复用**：工具、权限、skill、检索只实现一次，被 HTTP 与 MCP 两个入口共享。
2. **工具即权限边界**：不提供通用 `bash`/`eval`；只暴露**类型化的领域工具**，每个工具自带 `side / mutating / permission` 元数据，权限闸据此决策。
3. **默认不可逆操作需确认**：改动型工具默认 `ask`（→ 前端预览确认），只读直接放行（呼应 PRD 预览—确认机制）。
4. **上下文隔离的多智能体**：复杂任务由 coordinator 拆给领域专家子 agent，各自只带相关工具/文档，降幻觉、省 token、可并行。
5. **渐进式披露**：skill/工具/文档**按需加载**，固定上下文保持精简（利于缓存与成本）。
6. **最小信任面**：只操作受信任的工程目录；密钥仅本地、不外泄；检索/文件只读且限定项目根。

---

## 3. 技术选型

| 组件 | 选型 | 说明 |
|------|------|------|
| Web 框架 | **FastAPI** | 异步、Pydantic 校验、OpenAPI |
| ASGI | **uvicorn** | 本地 `127.0.0.1` |
| LLM SDK | **openai**（Chat Completions） | function calling；`base_url` 适配任意 OpenAI 兼容端点（BYO key、本地模型） |
| MCP | **mcp**（Python SDK） | 把同一套工具暴露为 MCP server（第二入口） |
| 数据模型 | **pydantic v2** | DTO、settings schema |
| 检索 | ripgrep/glob（精确）+ FAISS/Chroma + embedding（语义，M2） | 代码检索子系统 |
| 配置 | pydantic-settings / `.env` + 项目内 settings 文件 | 端点/密钥/权限/skill |

> 用 **Chat Completions** 而非 Responses API，保证跨端点兼容（BYO key、本地模型）。

---

## 4. 总体分层架构

```
                         ┌──────────── 入口层（多张脸）────────────┐
   Godot 前端 ──HTTP──►   │  api/   FastAPI（/chat /health /reset）  │
   外部 AI 客户端 ─MCP─►   │  mcp/   MCP server（ListTools/CallTool） │
                         └───────────────────┬──────────────────────┘
                                             │  统一进入
   ┌─────────────────────────── 编排层 ──────▼──────────────────────────┐
   │  coordinator/   主控 agent：理解目标 → 规划 → 委派                  │
   │  agents/        领域专家子 agent（program / map / scene / resource）│
   │  orchestrator/  Agent 循环：工具分流、agent 帧栈、resume            │
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
   │ retrieval/ 代码检索(Glob/Grep + RAG)  skills/ 技能(渐进披露)       │
   │ sessions/ 会话与 agent 栈   llm/ OpenAI 封装   prompt/ 提示构建    │
   │ docs/ 文档接地(混合)        config/ 端点/密钥/权限/skill 配置       │
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
│   ├── coordinator/               # 主控 agent
│   ├── agents/                    # 领域专家子 agent 定义
│   ├── orchestrator/              # Agent 循环 + agent 帧栈 + resume
│   ├── permissions/               # 权限模式 + 规则 + 决策
│   ├── security/                  # 项目根/信任/密钥/settings schema
│   ├── tools/                     # 注册表 + schema + server_tools/
│   ├── retrieval/                 # glob_grep.py（精确）+ rag.py（语义）
│   ├── skills/                    # skill 加载与渐进披露
│   ├── docs/                      # 混合文档接地 enrich
│   ├── sessions/                  # 会话与 agent 栈
│   ├── llm/                       # OpenAI 封装
│   └── prompt/                    # system prompt 构建
└── skills/                        # 用户/社区 skill 目录（每个一 SKILL.md）
```

---

## 5. 多入口形态（HTTP + MCP）

借鉴 Claude Code"一份源码多张脸"：同一套**工具 / 权限 / skill / 检索**被两个入口复用。

| 入口 | 调用方 | 用途 | 上下文 |
|------|------|------|------|
| **HTTP**（主） | Godot 前端插件 | 完整能力：含改动型工具（经预览确认）、多智能体 | 完整 `ToolContext` |
| **MCP server** | 外部 AI 客户端（Claude Desktop / Cursor 等） | 主要只读/分析（检索、读脚本/场景、文档接地）；把本服务当工具源 | **简化 `ToolContext`**：`interactive=False`、不启多智能体 |

要点（对齐 Claude Code 的"简化版 ToolUseContext"）：

- MCP 入口构造**简化上下文**：默认只暴露**只读工具 + 自动放行的改动型**；需要"前端预览确认"的改动型工具，因为没有 Godot 交互通道，默认 `deny` 或要求 Godot 同时在线（改动最终仍要 Godot 执行）。
- 两入口共用 `permissions/`、`tools/`、`skills/`、`retrieval/`，避免实现分叉。
- MCP 入口让本项目**同时覆盖竞品两大流派**（编辑器内插件 + MCP server，见需求文档 §1.5）。

---

## 6. 多智能体架构（coordinator + 领域专家）

借鉴 Claude Code 的 `coordinator/` + `AgentTool` + `tasks/`。

> 📐 **详细设计与全链路时序图见**《多智能体与权限系统详细设计》§1–2（数据结构、`delegate` 工具、帧栈生命周期、上下文隔离、并行子 agent）。

### 6.1 角色

| Agent | 职责 | 工具子集 | 模型档位（建议） |
|------|------|------|------|
| **Coordinator（主控）** | 理解目标、规划、把子任务**委派**给专家、汇总 | `delegate`、`read_*`、检索、skill | 强模型 |
| **ProgrammingAgent** | 写/改/重构/修脚本、单测 | program 域工具 + 文档接地 + 检索 | 强模型 |
| **MapAgent** | 瓦片地图建造 | map 域工具 + 瓦片上下文 | 中/快模型可 |
| **SceneAgent** | 节点/场景搭建 | scene 域工具 + ClassDB 接地 | 中模型 |
| **ResourceAgent** | 资源/项目配置 | resource/project 域工具 | 快模型可 |

- **上下文隔离**：每个子 agent 只带**本域工具 + 相关上下文/skill**，互不污染——降幻觉、省 token（Claude Code 子 agent 隔离思想）。
- **委派工具 `delegate(agent, task)`**：coordinator 调用它派发子任务；服务为该子 agent 开一个**独立的 agent 帧**（独立 messages、独立工具集），跑到产出结果后把**摘要**回传 coordinator。
- **模型档位可不同**：简单域用便宜/快模型（呼应需求文档创新点⑥ 多模型路由）。

### 6.2 Agent 帧栈与跨进程挂起

多智能体 + 跨进程预览确认的关键：**会话内维护一个 agent 帧栈**。

```
session
└── agent_stack: [ frame(coordinator), frame(MapAgent), ... ]   # 栈顶为当前活跃 agent
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

---

## 7. 工具系统与注册表

每个工具携带元数据，决定**在哪执行、是否需确认、归哪个 agent、权限默认值**。

```python
# app/tools/registry.py
from dataclasses import dataclass
from typing import Callable, Literal, Optional

@dataclass
class ToolDef:
    name: str
    domain: str                          # program | map | scene | resource | project | core
    side: Literal["front", "server"]     # front=前端执行；server=本服务执行
    mutating: bool                       # 改动型→默认需权限确认；只读→默认放行
    schema: dict                         # OpenAI function schema
    handler: Optional[Callable] = None   # server 工具的实现
    enrich: Optional[Callable] = None    # front 工具的服务端增强（如 read_class_docs 合并 prose）
    permission: str = "auto"             # 默认权限策略键（被权限模式/规则覆盖）

REGISTRY: dict[str, ToolDef] = {}
def register(t: ToolDef): REGISTRY[t.name] = t
def tools_for(agent) -> list:            # 给某 agent 的 OpenAI tools（稳定排序利缓存）
    return [{"type": "function", "function": t.schema}
            for t in sorted(agent.tools, key=lambda n: n) ]
```

- **不提供通用 `bash`/`eval`**：安全边界靠"只有类型化领域工具"来保证（§9）。
- 新增能力域/工具 = 注册一个 `ToolDef` + 归到某 agent，**编排/权限/入口零改动**（NFR-10）。

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

---

## 10. 代码检索子系统

借鉴 Claude Code 的 `Glob`/`Grep` + 检索，做两层（皆 server 工具、限定工程根、只读）：

> 📐 **详细设计见**《代码检索·Skill·安全边界详细设计》§一（工具定义、RAG 切块/索引生命周期、边界限制）。

| 层 | 工具 | 实现 | 用途 | 阶段 |
|----|------|------|------|------|
| **精确** | `list_files(glob)`、`grep_code(pattern)` | ripgrep / glob | 找文件、找符号/字符串（快、确定） | M1 |
| **语义** | `search_codebase(query)` | embedding + 向量库（FAISS/Chroma） | "找和 X 相关的代码"（RAG） | M2 |

- **索引生命周期**（语义层）：首次打开工程构建；文件变更增量更新；可手动重建（§13 待定细化）。
- 检索结果只返回**片段 + 路径 + 行号**，不全量灌入（控成本，呼应渐进披露）。
- 严格限定 `project_root` 与 `deny_paths`（§9）。

---

## 11. Skill 系统

借鉴 Claude Code `skills/` 的**渐进式披露**：技能简述常驻、全文按需加载。

> 📐 **详细设计见**《代码检索·Skill·安全边界详细设计》§二（SKILL.md 结构、发现/加载、`load_skill`、内置清单、安全防注入提权）。

### 11.1 结构

```
skills/
├── gdscript-4x-idioms/SKILL.md      # GDScript 4.x 惯用法与陷阱
├── tilemap-terrain/SKILL.md         # 地形自动连边/房间生成套路
├── scene-composition/SKILL.md       # 常见节点组合模式
├── csharp-godot-dotnet/SKILL.md     # Godot .NET C# 规范
└── <用户/社区自定义>/SKILL.md
```

每个 `SKILL.md` 含：`name`、`description`（一句话）、`when_to_use`、正文（详细指令/示例）。

### 11.2 渐进式加载

- 启动时扫描 `skills/`，把每个 skill 的**一句话 description** 注入 system prompt（占用极小）。
- 任务相关时，agent 调用 `load_skill(name)`（server 只读工具）读取**全文**注入上下文——只在需要时付出 token（Claude Code 渐进披露）。
- **可配置/可扩展**：用户/社区**丢一个 skill 目录即被发现**（呼应开源 + 创新点⑤ 社区可扩展）。

### 11.3 与多智能体

- skill 可**按 agent 绑定**：MapAgent 默认带 `tilemap-terrain`，ProgrammingAgent 默认带 `gdscript-4x-idioms`/`csharp-godot-dotnet`。

---

## 12. 文档接地（混合：前端签名 + 服务端 prose）

（沿用 v0.2，纳入新结构。）`read_class_docs` 是 **「前端只读工具 + 服务端增强」**：

- **签名来自前端 ClassDB**：方法/属性/信号/枚举/继承，100% 等于用户引擎版本，且含自定义类/GDExtension/插件 API。
- **prose 来自服务端 doc dump**：按引擎版本内置官方文档描述。
- **合并**：模型调用 → 前端查 ClassDB 返签名 → 服务端 `enrich` 钩子按 `class_name` 从版本匹配 dump 取 prose 合并 → 喂回模型。

```python
# app/docs/class_docs.py
def enrich_class_docs(args: dict, front_result: str) -> str:
    cls = args.get("class_name", "")
    prose = DOC_DUMP.lookup(cls)           # 版本匹配；自定义类查不到
    return front_result + ("\n\n[官方文档]\n" + prose if prose else "")
```

- 版本对齐靠请求里的 `engine_version`；自定义类只给签名、不杜撰 prose（防幻觉）。

---

## 13. Agent 编排循环（整合权限 + 多智能体）

```python
# app/orchestrator/agent.py（简化）
def step(session, llm, ctx) -> dict:
    frame = session.top_frame()                       # 当前活跃 agent 帧
    while True:
        resp = llm.chat(frame.messages, tools_for(frame.agent), model=frame.agent.model)
        msg = resp.choices[0].message
        frame.messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return finish_frame(session, msg.content)  # 弹栈：子 agent 结果回传上层，或 final

        front_calls = []
        for call in msg.tool_calls:
            tool = REGISTRY[call.function.name]
            args = json.loads(call.function.arguments or "{}")

            if tool.name == "delegate":                # 多智能体：压入子 agent 帧
                session.push_frame(make_agent(args["agent"]), args["task"]); break

            decision = permissions.check(tool, args, ctx)   # allow | ask | deny
            if decision == "deny":
                append_tool_result(frame, call, "被拒绝：<原因>", is_error=True); continue
            if tool.side == "server":
                result = tool.handler(args)            # 检索/skill/内容生成（只读或安全）
                append_tool_result(frame, call, result)
            else:                                       # front 工具
                front_calls.append({"id": call.id, "name": tool.name,
                                    "input": args, "needs_confirm": decision == "ask",
                                    "frame_id": frame.id})
        if front_calls:
            return {"type": "tool_calls", "calls": front_calls}   # 挂起整栈，回前端
        # 否则继续（全 server 工具 / delegate 后切到新帧）
```

前端回传结果 → 按 `frame_id` 路由、`enrich` 增强后 append、继续 `step`（见 §12、§8）。

---

## 14. HTTP 接口规格

### POST `/chat`

```jsonc
{
  "session_id": "uuid",
  "user_message": "做一个主菜单并给玩家加二段跳",   // 二选一
  "context": "<selection/scene/tiles ...>",        // 可选
  "language_hint": "gdscript",                      // gdscript|csharp
  "engine_version": "4.4.1",                        // 选 doc dump（§12）
  "permission_mode": "default",                     // default|plan|auto_approve|read_only
  "tool_results": [                                 // 二选一：前端回传
    {"tool_use_id":"call_x","frame_id":"f1","content":"OK 已写入","is_error":false}
  ]
}
```

响应（三态）：

```jsonc
{"type":"tool_calls","text":"…","calls":[
  {"id":"call_x","name":"propose_script_edit","input":{...},
   "needs_confirm":true,"frame_id":"f1","agent":"ProgrammingAgent"}]}
{"type":"final","text":"主菜单与二段跳已完成。"}
{"type":"error","text":"端点调用失败：401，请检查 key。"}
```

DTO（节选）：

```python
class ToolResult(BaseModel):
    tool_use_id: str
    frame_id: str
    content: str
    is_error: bool = False

class ChatRequest(BaseModel):
    session_id: str
    user_message: str | None = None
    context: str | None = None
    language_hint: str | None = None
    engine_version: str | None = None
    permission_mode: str | None = None
    tool_results: list[ToolResult] | None = None
```

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | `{ok, model, endpoint_reachable, function_calling_supported}` |
| POST | `/reset` | 清空会话（含 agent 栈） |
| GET | `/skills` | 列出已发现 skill（name/description） |

### MCP 入口

- `ListTools` → 把**只读/自动放行**工具翻译成 MCP `Tool`。
- `CallTool` → 路由到同一 `tool.handler` / 同一权限闸（简化上下文，`ask` 降级 `deny`）。

---

## 15. OpenAI SDK 用法要点

- `OpenAI(base_url=..., api_key=...)`；`chat.completions.create(model, messages, tools, tool_choice="auto")`。
- 工具：`{"type":"function","function":{name, description, parameters(JSON schema)}}`。
- 读取：`resp.choices[0].message.tool_calls`，每项 `{id, function:{name, arguments(JSON 串)}}`；`arguments` 一律 `json.loads`。
- 回传：`{"role":"tool","tool_call_id":id,"content":result}`；前一条 assistant（含 tool_calls）必须保留，且其所有 tool_calls 在下次请求前被回答齐。
- 多智能体下：每个 agent 帧是**独立 messages**，各自调用 `chat`（可不同 `model`）。
- 流式（可选/后置）：`stream=True`；工具循环用非流式即可，UI 用"思考/执行"状态。

---

## 16. Prompt 缓存与上下文管理

- 稳定前缀在前：`tools`（稳定排序）→ system（含常驻 skill 简述、冻结）→ 易变上下文放 messages 末尾。OpenAI 对 ≥1024 token 前缀自动缓存；其余端点按"稳定前缀"尽力而为。
- 多智能体天然控上下文：每个子 agent 只带本域工具/skill/文档，单帧上下文小。
- 长会话：裁剪/摘要早期 `tool` 结果（M2）。

---

## 17. 错误处理与降级

| 场景 | 处理 |
|------|------|
| 端点不可达 / 401 / 限流 | SDK 重试；失败 → `{"type":"error"}`，不抛 500 |
| 端点不支持 function calling | 能力探测拦截，前端提示更换模型 |
| 工具被权限 `deny` / 模型产非法参数 / 用户拒绝 | 作为 `tool` 结果（`is_error`）回传，模型修正 |
| 子 agent 失败 | 作为 `delegate` 结果回传 coordinator，由其决定重试/换策略 |
| 会话/栈过大 | 裁剪、限制委派深度与帧数 |

---

## 18. 可扩展性

- **新工具/域** = 注册 `ToolDef` + 绑定 agent；编排/权限/入口零改动。
- **新 agent** = 定义工具子集 + system prompt + 默认 skill + 模型档位。
- **新 skill** = 丢一个 `SKILL.md` 目录即被发现（社区可扩展）。
- **新入口** = 复用同一 `tools/permissions/skills`（已示范 HTTP/MCP）。

---

## 19. 与里程碑对应

| 阶段 | 本服务交付 |
|------|-----------|
| M0 骨架 | HTTP `/chat` + 单 agent 循环 + 工具注册表 + **权限闸(default 模式)** + 安全边界(项目根/无 shell) + 1 个最小工具 |
| M1 全域工具 | 各域工具；精确检索(glob/grep)；混合文档接地；语言约束；Skill 简述常驻 + `load_skill` |
| M2 增强 | 语义检索(RAG)；调试错误/单测协议；上下文裁剪；权限规则与模式完善；MCP 入口 |
| M3 创新 | **多智能体 coordinator+专家**全量、并行子 agent、多模态(草图→关卡/看懂资产)、内容生成 |

> 多智能体最小形态（串行委派）可在 M2 末引入；并行与自主多步在 M3。

---

## 20. 风险与未决

| 项 | 说明 |
|----|------|
| 多智能体 × 跨进程预览确认 | agent 帧栈挂起/resume 较复杂；v1 限串行、限委派深度 |
| 端点 function calling 兼容性 | 本地/小模型支持参差；能力探测 + 降级提示 |
| 权限规则与 PRD 预览确认的统一表述 | 已统一为权限三态；需与前端 UI 对齐 `needs_confirm` 语义 |
| MCP 入口的改动型工具 | 无前端确认通道，默认降级；改动最终仍需 Godot 执行，MCP 模式定位为只读/分析为主 |
| 文档接地 doc dump | 版本管理与打包策略待定（§12） |
| 语义检索索引 | 构建时机/增量/存储位置待定（§10） |
| 多模态端点 | 草图→关卡/看懂资产需端点支持图像输入，需探测 |
| Skill 信任 | 第三方 skill 是注入到 prompt 的指令，需作为不可信内容对待，避免 prompt 注入越权（与 §9 信任模型一致） |
