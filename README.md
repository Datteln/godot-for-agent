# godot-for-agent

> Godot 内嵌 AI 游戏开发 Agent 的完整工程。仓库包含需求/架构文档、Python 本地 LLM 服务，以及 Godot 4 编辑器插件前端。

目标是把 AI 助手放进 Godot 编辑器里：读取工程上下文、检索代码、调用大模型、多智能体分工、预览并确认改动、通过 UndoRedo 撤销，并逐步支持 AI 自测、运行时诊断、草图转关卡和资产理解能力。

<!-- 📸 在此处插入项目总览截图 -->
<!-- ![项目总览](docs/images/overview.png) -->

---

## 目录

- [godot-for-agent](#godot-for-agent)
  - [目录](#目录)
  - [仓库结构](#仓库结构)
  - [功能总览](#功能总览)
  - [后端服务 (`ai_agent_service`)](#后端服务-ai_agent_service)
    - [后端快速启动](#后端快速启动)
    - [API 端点一览](#api-端点一览)
      - [`/chat` 请求体](#chat-请求体)
      - [`/chat` 三态响应](#chat-三态响应)
      - [可用命令](#可用命令)
    - [环境变量完整参考](#环境变量完整参考)
      - [LLM 配置](#llm-配置)
      - [Thinking 预算](#thinking-预算)
      - [项目 \& 运行配置](#项目--运行配置)
      - [权限 \& 安全](#权限--安全)
      - [存储路径](#存储路径)
      - [RAG \& Embedding](#rag--embedding)
      - [RAG 自动构建](#rag-自动构建)
      - [资产理解](#资产理解)
      - [验证系统](#验证系统)
      - [自动压缩](#自动压缩)
    - [多智能体系统](#多智能体系统)
    - [工具系统](#工具系统)
      - [Server 工具（Python 侧执行）](#server-工具python-侧执行)
      - [Front 工具（Godot 侧执行）](#front-工具godot-侧执行)
        - [Core 域](#core-域)
        - [Program 域](#program-域)
        - [Scene 域](#scene-域)
        - [Project 域](#project-域)
        - [Map 域](#map-域)
        - [Resource 域](#resource-域)
    - [RAG 检索系统](#rag-检索系统)
    - [LLM 管理](#llm-管理)
    - [权限与安全](#权限与安全)
      - [权限模式](#权限模式)
      - [权限检查流程](#权限检查流程)
      - [安全规则](#安全规则)
    - [Skill / OutputStyle 扩展](#skill--outputstyle-扩展)
      - [Skill](#skill)
      - [OutputStyle](#outputstyle)
    - [验证系统](#验证系统-1)
    - [会话与恢复](#会话与恢复)
    - [MCP 服务器](#mcp-服务器)
    - [记忆系统](#记忆系统)
    - [Doctor 自检](#doctor-自检)
  - [前端插件 (`ai_agent_frontend`)](#前端插件-ai_agent_frontend)
    - [安装与启用](#安装与启用)
    - [UI 面板](#ui-面板)
    - [上下文采集](#上下文采集)
    - [前端工具](#前端工具)
    - [UndoRedo 集成](#undoredo-集成)
    - [恢复提示](#恢复提示)
    - [EditorSettings 完整参考](#editorsettings-完整参考)
      - [服务连接](#服务连接)
      - [会话 \& UI](#会话--ui)
      - [LLM 配置](#llm-配置-1)
      - [压缩 \& 超时](#压缩--超时)
      - [RAG \& Embedding](#rag--embedding-1)
      - [资产理解](#资产理解-1)
      - [日志 \& 事件](#日志--事件)
      - [测试 \& Headless \& 超时](#测试--headless--超时)
  - [安全模型](#安全模型)
  - [开发自检](#开发自检)
  - [故障排查](#故障排查)

---

## 仓库结构

| 路径 | 说明 |
| --- | --- |
| `ai_map_agent/` | 需求文档、Python 服务架构方案、GDScript 前端架构方案和详细设计文档 |
| `ai_agent_service/` | FastAPI 本地服务，负责 LLM 调用、Agent 编排、权限闸、RAG、Skill、MCP 等 |
| `ai_agent_frontend/` | Godot 4 EditorPlugin 前端，负责编辑器 UI、上下文采集、预览确认和 UndoRedo |

```text
godot-for-agent/
├── ai_map_agent/              # 需求/架构/详设文档
├── ai_agent_service/          # Python 后端
│   ├── app/
│   │   ├── agents/            # Agent 定义 (markdown frontmatter)
│   │   │   └── agent_defs/    # coordinator, programming-agent, scene-agent,
│   │   │                      # map-agent, map-reader-agent, map-planner-agent,
│   │   │                      # map-validator-agent, map-reviewer-agent,
│   │   │                      # resource-agent, advisor
│   │   ├── api/               # FastAPI 路由 & DTO
│   │   ├── doctor/            # 服务自检
│   │   ├── events/            # 实时事件存储
│   │   ├── llm/               # LLM Provider / Prompt 缓存 / 消息变换 / 缓存决策
│   │   ├── lsp/               # LSP 状态
│   │   ├── mcp/               # MCP stdio 服务器
│   │   ├── memory/            # 项目级记忆
│   │   ├── orchestrator/      # Agent 编排器 / 地图进度跟踪 / Worker 管理
│   │   ├── output_styles/     # OutputStyle 目录扫描
│   │   ├── permissions/       # 权限引擎 & 规则
│   │   ├── prompt/            # Prompt 构建 / RAG 上下文注入 / 项目上下文
│   │   ├── query/             # 查询引擎（turn 管理 / 压缩 / 历史转换）
│   │   ├── rag/               # RAG 索引 / 混合检索 / 图融合 / 重排 / 自动构建
│   │   │   └── engine/        # 资产索引 / 场景图索引 / 信号图索引
│   │   ├── recovery/          # 崩溃恢复指针
│   │   ├── security/          # 路径沙箱 & 安全配置
│   │   ├── sessions/          # 会话持久化
│   │   ├── skills/            # Skill 目录扫描
│   │   ├── storage/           # 原子写入工具
│   │   ├── tools/             # 工具注册表
│   │   │   └── server_tools/  # 服务端工具实现 (read_file, grep_code, list_files,
│   │   │                      #   search_codebase, search_tools, load_skill)
│   │   └── verify/            # 编辑后语法/语义校验
│   ├── tests/
│   └── pyproject.toml
└── ai_agent_frontend/         # Godot 4 前端插件
    └── addons/ai_agent/
        ├── config/            # EditorSettings 迁移
        ├── context/           # 上下文采集 (场景树/诊断/文件缓存/ClassDB)
        ├── dto/               # GDScript DTO
        ├── logging/           # 前端日志
        ├── recovery/          # 恢复指针
        ├── service/           # HTTP 客户端 & 服务管理器
        ├── state/             # Agent 事件日志 & 状态存储
        ├── tools/             # 前端工具 (program/scene/map/resource/project)
        │                      # 含地图子系统: intent_parser, layout_planner,
        │                      # algorithms, blueprints, layer_scaffold,
        │                      # platform_plan_validator, reachable_growth, validator
        ├── ui/                # 聊天面板/命令面板/Doctor/Memory/扩展/预览确认/
        │                      # 恢复提示/Markdown 渲染/虚拟滚动/消息存储
        └── undo/              # 统一 UndoRedo 管理
```

---

## 功能总览

| 领域 | 能力 |
| --- | --- |
| **对话** | `/chat` 三态响应（`tool_calls` / `final` / `error`）、SSE 事件流轮询、中断、丢弃 pending、会话重置 |
| **多智能体** | 10 个专业 Agent（含 4 个地图流水线子 Agent）+ `delegate` / `delegate_many` 委派 + `create_plan` 计划 |
| **工具** | 90+ 前端/服务端工具，统一注册、权限闸、预览确认、UndoRedo；覆盖编程、场景、项目、地图、资源 5 大域 |
| **RAG** | BM25 基线 + 可选 Embedding(FAISS) + Symbol + 场景图/信号图 + 混合检索 + 交叉编码器重排 + 查询路由 + 自动增量构建 |
| **LLM** | OpenAI 兼容、5 级 effort 模型、thinking 预算、fallback 降级、prompt 缓存（三级降级） |
| **安全** | 路径沙箱、5 种权限模式、信任模型、deny/allow 规则、会话级 allow、预览确认 |
| **验证** | 编辑后自动语法快检 (Godot CLI) + LLM 语义校验 + 自动修复重试 |
| **地图** | 完整地图编辑流水线：意图解析 → 布局规划 → 可达性分析 → 算法规划 → 小批编辑 → 校验修复 → 截图复核 |
| **扩展** | Skill / OutputStyle / 自定义 Agent（markdown frontmatter 模式） |
| **记忆** | 项目级 JSON 持久化记忆（`/memory` GET/POST） |
| **恢复** | 崩溃恢复指针（`/recovery-pointer`），前端检测后提示恢复 |
| **MCP** | `python -m app --mcp-stdio` 提供只读/服务端工具的 stdio JSON-RPC 入口 |
| **Doctor** | 服务自检（`/doctor`）：LLM 连通性、工具注册、Skill/OutputStyle/Memory 状态 |
| **前端 UI** | 聊天面板、命令面板、Doctor 面板、Memory 面板、扩展面板、预览确认、内联工具确认、恢复提示、虚拟滚动 |
| **自动化** | RAG 索引自动增量构建、会话历史自动压缩（可配 LLM 语义摘要）、编辑后自动校验 |
| **DevOps** | Git 状态/差异读取、系统命令执行（预览确认）、GDScript 脚本执行、项目导出、导航网格烘焙 |

<!-- 📸 在此处插入功能演示 GIF -->
<!-- ![功能演示](docs/images/demo.gif) -->

---

## 后端服务 (`ai_agent_service`)

### 后端快速启动

需要 Python 3.10+。

```powershell
cd ai_agent_service
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

开发时建议显式设置端口和 token：

```powershell
$env:AI_AGENT_PROJECT_ROOT = (Resolve-Path ..).Path
$env:AI_AGENT_PORT = "8765"
$env:AI_AGENT_AUTH_TOKEN = "dev-token"
$env:AI_AGENT_LLM_BASE_URL = "https://api.openai.com/v1"
$env:AI_AGENT_LLM_API_KEY = "<your-key>"
$env:AI_AGENT_LLM_MODEL = "gpt-4o-mini"
python -m app
```

<!-- 📸 在此处插入后端启动终端截图 -->
<!-- ![后端启动](docs/images/backend-startup.png) -->

### API 端点一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/chat` | 主对话入口：发送用户消息或回传工具结果，三态响应 |
| `GET` | `/health` | 健康检查：模型名、端点可达性、function calling 支持 |
| `POST` | `/reset` | 重置指定 session |
| `POST` | `/chat/discard-pending` | 丢弃 session 中 pending 的工具调用 |
| `POST` | `/chat/interrupt` | 中断当前 session 的 agent 执行 |
| `GET` | `/doctor` | 服务自检报告 |
| `GET` | `/skills` | 列出已注册的 Skill |
| `GET` | `/output-styles` | 列出已注册的 OutputStyle |
| `GET` | `/chat/events` | 事件流轮询（长轮询，`session_id` + `after` seq） |
| `GET` | `/sessions/{session_id}/history` | 获取会话历史消息 |
| `GET` | `/recovery-pointer` | 读取崩溃恢复指针 |
| `POST` | `/recovery-pointer/dismiss` | 关闭恢复提示 |
| `GET` | `/commands` | 列出可用命令 |
| `POST` | `/commands/{name}` | 执行命令（`doctor` / `rebuild_index` / `compact` / `set_effort` / `set_output_style` / `refresh_extensions`） |
| `GET` | `/memory` | 读取项目级记忆 |
| `POST` | `/memory` | 写入/更新记忆条目 |

所有端点（除 `/health` 可选外）需在请求头中携带 Bearer token：

```http
Authorization: Bearer <your-token>
```

#### `/chat` 请求体

```jsonc
{
  "session_id": "uuid-string",
  "user_message": "帮我创建一个玩家角色脚本",     // 与 tool_results 二选一
  "tool_results": [],                             // 回传前端工具执行结果
  "context": {                                    // 前端采集的结构化上下文
    "selection": {},
    "scene_tree": {},
    "tile_catalog": [],
    "project_files": [],
    "debugger_errors": [],
    "diagnostics": [],
    "dotnet_enabled": false
  },
  "permission_mode": "default",                   // 可选：default/plan/auto_approve/read_only/full_access
  "effort": "standard",                           // 可选：quick/standard/deep/verify/advisor
  "output_style": "default"                       // 可选：OutputStyle 名称
}
```

#### `/chat` 三态响应

```jsonc
// 1. tool_calls — 需要前端执行工具
{
  "status": "tool_calls",
  "tool_calls": [
    {
      "tool_use_id": "call_xxx",
      "frame_id": "f1",
      "name": "propose_script_edit",
      "args": { "path": "scripts/player.gd", "content": "..." }
    }
  ],
  "turn_id": "turn-xxx"
}

// 2. final — 最终文本回复
{
  "status": "final",
  "message": "已为你创建了玩家角色脚本...",
  "turn_id": "turn-xxx"
}

// 3. error — 出错
{
  "status": "error",
  "error_code": "llm_unreachable",
  "error_message": "..."
}
```

#### 可用命令

| 命令 | 说明 | 参数 |
| --- | --- | --- |
| `doctor` | 返回当前服务自检报告 | 无 |
| `rebuild_index` | 重建本地 RAG 检索索引 | `include`（glob，默认 `**/*`）、`max_files`（默认 4000）、`incremental`（默认 true） |
| `compact` | 压缩指定 session 的早期上下文 | `keep_recent`（默认 12）、`use_llm`（默认 true） |
| `set_effort` | 设置当前 session 的 effort 档位 | `effort`（quick/standard/deep/verify/advisor） |
| `set_output_style` | 设置当前 session 的 OutputStyle | `output_style`（样式名） |
| `refresh_extensions` | 重新扫描 Skill 与 OutputStyle 目录 | 无 |

健康检查示例：

```powershell
Invoke-RestMethod `
  -Uri http://127.0.0.1:8765/doctor `
  -Headers @{ Authorization = "Bearer dev-token" }
```

重建本地 RAG 索引：

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8765/commands/rebuild_index `
  -Headers @{ Authorization = "Bearer dev-token" } `
  -ContentType "application/json" `
  -Body '{"args":{"include":"**/*","max_files":4000,"incremental":true}}'
```

---

### 环境变量完整参考

所有变量均可通过 `AI_AGENT_` 前缀的环境变量或工作目录下的 `.env` 文件配置。

#### LLM 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI 兼容 Chat Completions 端点 |
| `AI_AGENT_LLM_API_KEY` | *(空)* | 大模型 API key（`SecretStr`，不泄露到日志/响应） |
| `AI_AGENT_LLM_MODEL` | `gpt-4o-mini` | 默认对话模型 |
| `AI_AGENT_LLM_QUICK_MODEL` | *(空=用 llm_model)* | quick effort 模型 |
| `AI_AGENT_LLM_STANDARD_MODEL` | *(空=用 llm_model)* | standard effort 模型 |
| `AI_AGENT_LLM_DEEP_MODEL` | *(空=用 llm_model)* | deep effort 模型 |
| `AI_AGENT_LLM_VERIFY_MODEL` | *(空=用 llm_model)* | verify effort 模型 |
| `AI_AGENT_LLM_ADVISOR_MODEL` | *(空=用 llm_model)* | advisor effort 模型 |
| `AI_AGENT_LLM_FALLBACK_MODEL` | *(空=不降级)* | 主模型不可用时的降级模型 |
| `AI_AGENT_LLM_REQUEST_TIMEOUT_S` | `60.0` | 单次 LLM 请求超时（秒） |

#### Thinking 预算

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_LLM_THINKING_BUDGET_QUICK` | *(空=内置 1024)* | quick effort 的 thinking token 预算 |
| `AI_AGENT_LLM_THINKING_BUDGET_STANDARD` | *(空=内置 4096)* | standard effort 的 thinking token 预算 |
| `AI_AGENT_LLM_THINKING_BUDGET_DEEP` | *(空=内置 16384)* | deep effort 的 thinking token 预算 |
| `AI_AGENT_LLM_THINKING_BUDGET_VERIFY` | *(空=内置 0)* | verify effort 的 thinking token 预算（0=关闭 thinking） |
| `AI_AGENT_LLM_THINKING_BUDGET_ADVISOR` | *(空=内置 2048)* | advisor effort 的 thinking token 预算 |

#### 项目 & 运行配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_PROJECT_ROOT` | `cwd` | 当前 Godot 工程根目录 |
| `AI_AGENT_HOST` | `127.0.0.1` | 绑定地址（仅本机回环） |
| `AI_AGENT_PORT` | `0` | 监听端口；`0` = 操作系统随机分配 |
| `AI_AGENT_LOG_LEVEL` | `DEBUG` | 日志等级：DEBUG/INFO/WARNING/ERROR/CRITICAL |
| `AI_AGENT_LOG_DIR` | `logs` | 日志文件存储目录 |
| `AI_AGENT_MAX_TURNS` | `36` | 单次消息的 agent 循环全局上限 |
| `AI_AGENT_MANAGED_PROCESS` | `false` | 是否由 Godot 插件通过管道启动（禁用控制台日志，只保留文件日志） |

#### 权限 & 安全

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_PERMISSION_MODE` | `default` | 会话初始权限模式：`default` / `plan` / `auto_approve` / `read_only` / `full_access` |
| `AI_AGENT_TRUSTED_PROJECT` | `false` | 工程是否已被用户标记为受信任 |
| `AI_AGENT_DENY_RULES` | `[]` | 显式 deny 规则（JSON 数组，始终生效） |
| `AI_AGENT_ALLOW_RULES` | `[]` | 显式 allow 规则（JSON 数组，仅 trusted 生效） |

#### 存储路径

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_SESSION_STORE_DIR` | `.ai_agent_service/sessions` | 会话本地持久化目录 |
| `AI_AGENT_RECOVERY_POINTER_PATH` | `.ai_agent_service/recovery_pointer.json` | 最小恢复指针路径 |
| `AI_AGENT_MEMORY_STORE_PATH` | `.ai_agent_service/memory.json` | 项目本地记忆存储 |
| `AI_AGENT_RAG_INDEX_PATH` | `.ai_agent_service/rag_index.json` | 本地 RAG 索引路径 |
| `AI_AGENT_USER_SKILLS_DIR` | `~/.ai_agent/skills` | 用户级 Skill 目录 |
| `AI_AGENT_PROJECT_SKILLS_DIR` | `.ai_agent/skills` | 项目级 Skill 目录 |
| `AI_AGENT_OUTPUT_STYLES_DIR` | `.ai_agent/output_styles` | 项目级 OutputStyle 目录 |

#### RAG & Embedding

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_EMBEDDING_PROVIDER` | `disabled` | Embedding 提供方：`disabled` / `openai` / `local` / `bge-m3` |
| `AI_AGENT_EMBEDDING_MODEL` | `text-embedding-3-small` | Embedding 模型名 |
| `AI_AGENT_EMBEDDING_ENDPOINT` | `https://api.openai.com/v1` | Embedding API 端点 |
| `AI_AGENT_EMBEDDING_API_KEY` | *(空)* | Embedding API key |
| `AI_AGENT_EMBEDDING_TIMEOUT_S` | `3.0` | Embedding 请求超时（秒，上限 3.0） |
| `AI_AGENT_EMBEDDING_RETRIES` | `1` | Embedding 重试次数（上限 2） |
| `AI_AGENT_RERANK_MODEL` | *(空=跳过)* | 交叉编码器重排模型名 |
| `AI_AGENT_RERANK_TIMEOUT_S` | `2.0` | 重排请求超时（秒，上限 2.0） |
| `AI_AGENT_RAG_QUERY_ROUTER_ENABLED` | `true` | 是否启用查询路由 |
| `AI_AGENT_RAG_TOKEN_BUDGET` | `1500` | RAG 注入 prompt 的 token 预算（下限 128） |
| `AI_AGENT_GRAPH_MAX_DEPTH` | `2` | 图检索最大深度（0–8） |
| `AI_AGENT_GRAPH_MAX_NEIGHBORS` | `5` | 图检索每节点最大邻居数（1–100） |

#### RAG 自动构建

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_RAG_AUTO_BUILD_ENABLED` | `true` | 服务启动后是否在后台自动增量构建 RAG/EARS 全部索引 |
| `AI_AGENT_RAG_AUTO_WATCH_INTERVAL_S` | `1.0` | 文件监视器轮询间隔（秒，0.1–60.0） |
| `AI_AGENT_RAG_AUTO_WATCH_DEBOUNCE_S` | `0.75` | 文件变更去抖延迟（秒，0.0–30.0） |
| `AI_AGENT_RAG_AUTO_WATCH_SCAN_TIMEOUT_S` | `10.0` | 文件监视器扫描项目目录的超时（秒，1.0–120.0） |

#### 资产理解

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_ASSET_UNDERSTANDING_ENABLED` | `false` | 是否启用资产理解（图片→描述） |
| `AI_AGENT_ASSET_UNDERSTANDING_MODEL` | *(空)* | 资产理解模型名 |
| `AI_AGENT_ASSET_UNDERSTANDING_ENDPOINT` | *(空)* | 资产理解 API 端点 |
| `AI_AGENT_ASSET_UNDERSTANDING_API_KEY` | *(空)* | 资产理解 API key |
| `AI_AGENT_ASSET_UNDERSTANDING_TIMEOUT_S` | `10.0` | 资产理解请求超时（秒） |
| `AI_AGENT_ASSET_UNDERSTANDING_MAX_TOKENS` | `500` | 资产理解最大输出 token |
| `AI_AGENT_ASSET_UNDERSTANDING_CONCURRENCY` | `3` | 资产理解并发数（1–16） |

#### 验证系统

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_VERIFY_AFTER_EDIT` | `true` | 编辑类工具落地后是否自动触发校验 |
| `AI_AGENT_VERIFY_TRIGGER_TOOLS` | `["propose_script_edit","apply_text_edit","propose_tests","propose_content_file"]` | 触发自动校验的工具名集合（JSON 数组） |
| `AI_AGENT_VERIFY_SYNTAX_ENABLED` | `true` | 是否启用 Phase 1 语法快检 |
| `AI_AGENT_VERIFY_SYNTAX_TIMEOUT` | `10` | Phase 1 语法快检超时（秒） |
| `AI_AGENT_VERIFY_GODOT_PATH` | `godot` | Godot 可执行文件路径 |
| `AI_AGENT_VERIFY_EFFORT` | `verify` | Phase 2 语义校验使用的 effort 档位 |
| `AI_AGENT_VERIFY_MAX_RETRIES` | `2` | 单次编辑允许的最大校验-修复重试次数 |

#### 自动压缩

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AI_AGENT_AUTO_COMPACT_ENABLED` | `true` | 是否在驱动 LLM 前自动检查会话历史体积，超出阈值时自动压缩 |
| `AI_AGENT_AUTO_COMPACT_TOKEN_THRESHOLD` | `160000` | 自动压缩的 token 阈值（下限 1000） |
| `AI_AGENT_AUTO_COMPACT_KEEP_RECENT` | `12` | 自动压缩时保留的最近消息数（下限 6） |
| `AI_AGENT_AUTO_COMPACT_MIN_NEW_MESSAGES` | `8` | 自动压缩的防抖门槛（下限 1） |
| `AI_AGENT_COMPACT_SUMMARY_USE_LLM` | `true` | 压缩摘要是否调用 LLM 做语义压缩（失败时自动回退机械拼接） |
| `AI_AGENT_COMPACT_SUMMARY_MODEL` | *(空=用 quick 模型)* | 压缩摘要使用的 LLM 模型名 |

---

### 多智能体系统

采用 Claude Code 同构的 **markdown frontmatter + body** 模型定义 Agent。每个 Agent 文件位于 `app/agents/agent_defs/*.md`。

| Agent | 角色 | Effort | 最大轮数 | 核心能力 |
| --- | --- | --- | --- | --- |
| **coordinator** | 主控协调者 | standard | 12 | 所有工具 + `delegate` / `delegate_many` / `create_plan` |
| **programming-agent** | 代码专家 | deep | 10 | 脚本编写、调试、测试、重构、Git 操作、系统命令、导出 |
| **scene-agent** | 场景专家 | standard | 8 | 场景树操作、节点增删改查、信号连接、分组管理、项目设置、导航网格 |
| **map-agent** | 地图任务总控 | standard | 12 | 地图流水线调度、动态 worker 委派、结果合并、最终验收 |
| **map-reader-agent** | 地图读取 | standard | 6 | 读取地图上下文、图层、边界事实和局部区域（只读） |
| **map-planner-agent** | 地图规划 | standard | 8 | 布局规划、可达性分析、算法规划、候选修复方案（只规划） |
| **map-validator-agent** | 地图校验 | verify | 6 | 校验结果解释、失败归因、完成门判断（只校验） |
| **map-reviewer-agent** | 地图视觉复核 | verify | 6 | 截图复核、用户可见质量审查（只复核） |
| **resource-agent** | 资源专家 | standard | 8 | 资源创建、精灵表、动画轨道、着色器材质、内容文件 |
| **advisor** | 只读顾问 | advisor | 10 | 架构分析、设计建议、问题诊断（不写工程） |

**Agent 帧（Frame）模型**：每次对话创建根帧（coordinator），委派时创建子帧，子帧有独立消息上下文、独立轮数预算，执行完毕后结果回流父帧。

**地图流水线**：复杂地图任务按 `reader → planner → writer → validator → reviewer` 串行推进。map-agent 作为总控调度 4 个专职子 Agent，每个阶段只传 `map_worker_result_v1` 结构化结果。writer 通过动态 worker（`worker_spec`）创建，支持 `read_only` / `propose_only` / `write_one_batch` / `review_only` / `repair_propose` / `repair_write_one_batch` 等模式。

**Effort 档位**：`quick` / `standard` / `deep` / `verify` / `advisor`，每档可独立配置模型和 thinking 预算。

<!-- 📸 在此处插入 Agent 委派流程图或截图 -->
<!-- ![Agent 委派](docs/images/agent-delegation.png) -->

---

### 工具系统

工具分为 **server**（Python 侧执行）和 **front**（Godot 侧执行），统一在 `app/tools/registry.py` 注册。每个 `ToolDef` 携带：

- `side`：`server` 或 `front`
- `domain`：`core` / `program` / `scene` / `project` / `map` / `resource`
- 风险元数据：`reads_project` / `writes_project` / `executes_process` / `uses_network`
- `needs_preview`：是否需要前端预览确认
- `render_kind`：前端渲染类型（`diff` / `list` / `run` / `log` / `map` / `json` 等）
- `path_args` / `read_path_args` / `write_path_args`：路径参数声明
- `deferred`：是否延迟加载（M2+ 由 `search_tools` 按需发现）
- 地图写工具自动注入版本控制字段：`expected_revision` / `plan_version` / `batch_index` / `postconditions`

#### Server 工具（Python 侧执行）

| 工具名 | 域 | 说明 |
| --- | --- | --- |
| `read_file` | core | 读取工程内文件 |
| `grep_code` | core | 在工程内正则搜索 |
| `list_files` | core | 列出工程内文件 |
| `search_codebase` | core | RAG 代码检索 |
| `search_tools` | core | 按关键词发现 deferred 工具 |
| `load_skill` | core | 加载指定 Skill 的 markdown 内容 |

#### Front 工具（Godot 侧执行）

##### Core 域

| 工具名 | 写工程 | 说明 |
| --- | --- | --- |
| `delegate` | ✗ | 委派任务给子 Agent（支持 `worker_spec` 动态地图 worker） |
| `delegate_many` | ✗ | 并行委派多个子 Agent |
| `create_plan` | ✗ | 创建结构化执行计划 |

##### Program 域

| 工具名 | 写工程 | 执行进程 | 说明 |
| --- | --- | --- | --- |
| `propose_script_edit` | ✓ | ✗ | 整文件替换脚本/资源内容 |
| `propose_tests` | ✓ | ✗ | 提议测试代码（GUT/WAT） |
| `apply_text_edit` | ✓ | ✗ | 精确查找替换编辑 |
| `read_class_docs` | ✗ | ✗ | 读取 ClassDB 文档 |
| `read_debugger_errors` | ✗ | ✗ | 读取调试器错误 |
| `read_runtime_state` | ✗ | ✗ | 读取运行时节点状态 |
| `read_profiler_snapshot` | ✗ | ✗ | 读取 Profiler 快照 |
| `run_tests` | ✗ | ✓ | 运行项目测试（project / headless_scene） |
| `run_headless_self_test` | ✗ | ✓ | Headless 自测 |
| `run_system_command` | ✓ | ✓ | 执行系统命令（支持多种 shell） |
| `execute_gd_script` | ✗ | ✓ | 执行 GDScript 工具脚本（SceneTree/MainLoop） |
| `git_status` | ✗ | ✗ | 读取 Git 状态（只读） |
| `git_diff` | ✗ | ✗ | 读取 Git diff（支持 --staged） |
| `export_project` | ✓ | ✓ | 导出项目（release/debug） |

##### Scene 域

| 工具名 | 写工程 | 说明 |
| --- | --- | --- |
| `read_scene_tree` | ✗ | 读取场景树结构 |
| `add_node` | ✓ | 添加场景节点（支持位置、纹理资源） |
| `set_node_property` | ✓ | 设置节点属性（Variant 类型自动转换） |
| `delete_node` | ✓ | 删除场景节点 |
| `reparent_node` | ✓ | 移动节点到新父节点 |
| `rename_node` | ✓ | 重命名节点 |
| `instance_scene` | ✓ | 实例化 .tscn/.scn（支持地图坐标定位） |
| `duplicate_node` | ✓ | 复制节点 |
| `connect_signal` | ✓ | 连接信号 |
| `disconnect_signal` | ✓ | 断开信号 |
| `add_to_group` | ✓ | 添加节点到分组 |
| `remove_from_group` | ✓ | 从分组移除节点 |
| `list_node_groups` | ✗ | 列出节点所属分组 |
| `list_node_signals` | ✗ | 列出节点可发射的信号 |
| `list_node_methods` | ✗ | 列出节点的公开方法 |
| `validate_scene_state` | ✗ | 声明式场景校验（节点存在性/类型/属性/分组/信号） |
| `list_groups` | ✗ | 列出场景中所有分组 |
| `get_current_scene_path` | ✗ | 获取当前编辑场景路径 |
| `save_scene` | ✓ | 保存当前场景 |
| `list_open_scenes` | ✗ | 列出打开的场景标签页 |
| `capture_viewport_screenshot` | ✗ | 截取编辑器视口（2D/3D，支持焦点节点/区域） |
| `open_scene` | ✓ | 切换编辑场景 |
| `bake_navigation_mesh` | ✓ | 烘焙导航网格 |

##### Project 域

| 工具名 | 写工程 | 说明 |
| --- | --- | --- |
| `set_project_setting` | ✓ | 设置项目配置（project.godot） |
| `read_project_setting` | ✗ | 读取项目配置 |
| `list_autoloads` | ✗ | 列出自动加载单例 |
| `add_autoload` | ✓ | 注册自动加载 |
| `remove_autoload` | ✓ | 移除自动加载 |
| `list_input_actions` | ✗ | 列出输入映射 |
| `add_input_action` | ✓ | 创建/替换输入映射 |
| `remove_input_action` | ✓ | 移除输入映射 |
| `list_export_presets` | ✗ | 列出导出预设 |

##### Map 域

| 工具名 | 写工程 | 说明 |
| --- | --- | --- |
| `describe_tilemap_selection` | ✗ | 描述选中的 TileMapLayer |
| `describe_map_context` | ✗ | 读取当前场景的地图上下文（TileMap/GridMap/资源注册表/空间索引） |
| `plan_map_layout` | ✗ | 解析自然语言地图请求为结构化 MapIntent 和布局计划 |
| `describe_map_region` | ✗ | 读取指定区域的实际瓦片/网格数据 |
| `convert_map_coords` | ✗ | 地图坐标 ⇄ 世界坐标互转 |
| `plan_map_algorithms` | ✗ | 构建算法计划（区域/Poisson/A*/语法/约束） |
| `validate_platform_level_plan` | ✗ | 校验并编译 2D 平台关卡计划（跳跃可达性/路线连通性） |
| `plan_reachable_map_growth` | ✗ | 从可达前沿规划地图扩展（platformer/topdown/dungeon/3d_grid） |
| `compute_reachable_frontier` | ✗ | 计算从起点可达的所有格子（grid/leap/free 移动模型） |
| `sample_poisson_points` | ✗ | Poisson 盘采样（自然间距的道具/资源/敌人位置） |
| `compose_map_blueprint_grammar` | ✗ | 组合保存的蓝图/预制件为盖章计划 |
| `edit_map` | ✓ | 编辑 TileMap/GridMap（fill/erase/copy，版本控制，空间索引） |
| `paint_terrain_connect` | ✓ | 使用 TileSet terrain 规则自动连接绘制 |
| `place_map_objects` | ✓ | 放置 PackedScene 地图对象（坐标转换/重叠检测/空间索引） |
| `find_placement_anchors` | ✗ | 搜索合法的对象放置锚点 |
| `validate_object_placements` | ✗ | 校验已放置对象的合法性 |
| `repair_placements` | ✓ | 修复有问题的对象放置 |
| `validate_layer_coverage` | ✗ | 校验图层覆盖完整性 |
| `repair_layer_coverage` | ✓ | 修复图层覆盖缺口 |
| `query_spatial_index` | ✗ | 查询空间索引（语义/标签/区域） |
| `compact_spatial_index` | ✓ | 压缩空间索引 |
| `validate_map_region` | ✗ | 校验地图区域（连通性/地形/覆盖率/设计约束） |
| `repair_map_region` | ✓ | 修复地图区域问题 |
| `sample_noise_grid` | ✗ | 噪声网格采样（地形高度/密度/材质变化） |
| `write_resource_registry` | ✓ | 写入语义资源注册表 (resource_registry.json) |
| `save_map_blueprint` | ✓ | 保存当前区域为可复用蓝图 |
| `apply_map_blueprint` | ✓ | 应用已保存的蓝图到地图区域 |
| `ensure_standard_map_layers` | ✓ | 确保标准地图图层存在 |
| `fill_rect` | ✓ | 填充 TileMap 矩形区域（旧接口） |
| `paint_from_image_grid` | ✓ | 从图片网格绘制 TileMap（旧接口） |

##### Resource 域

| 工具名 | 写工程 | 说明 |
| --- | --- | --- |
| `create_resource` | ✓ | 创建 Godot 资源 |
| `read_image_metadata` | ✗ | 读取图片元数据 |
| `create_sprite_frames_from_sheet` | ✓ | 精灵表生成 SpriteFrames |
| `read_resource` | ✗ | 读取 Resource 属性 |
| `set_resource_property` | ✓ | 设置 Resource 属性 |
| `create_animation_track` | ✓ | 创建动画轨道 |
| `create_shader_material` | ✓ | 创建 Shader 材质 |
| `propose_content_file` | ✓ | 生成内容文件（对话/任务/本地化/数据表） |

<!-- 📸 在此处插入工具预览确认截图 -->
<!-- ![工具预览确认](docs/images/tool-preview.png) -->

---

### RAG 检索系统

```mermaid
graph LR
    Q[用户查询] --> QR[查询路由器]
    QR --> BM25[BM25 索引]
    QR --> EMB[Embedding 索引]
    QR --> SYM[Symbol 索引]
    QR --> GR[Graph 融合]
    BM25 --> HYB[混合检索]
    EMB --> HYB
    SYM --> HYB
    GR --> HYB
    HYB --> RR[交叉编码器重排]
    RR --> CTX[注入 Prompt]
```

| 组件 | 说明 |
| --- | --- |
| **BM25 索引** | 本地 TF-IDF 风格索引，`rebuild_index` 命令重建（支持增量模式） |
| **Embedding 索引** | 可选 FAISS 向量索引，支持 `openai` / `local` / `bge-m3` |
| **Symbol 索引** | 代码符号级检索（类、函数、变量） |
| **场景图索引** | 场景树结构、节点关系 |
| **信号图索引** | 信号定义、连接关系 |
| **资产索引** | 图片/资源元数据（支持 LLM 资产理解） |
| **混合检索** | 融合多检索器结果 |
| **图融合** | 利用图结构扩展相关节点 |
| **查询路由器** | 智能分发查询到最合适的检索器 |
| **交叉编码器重排** | 可选的二次精排（`rerank_model` 配置） |
| **自动构建管理器** | 后台文件监视 + 增量构建，无需手动重建索引 |

---

### LLM 管理

- **OpenAI 兼容**：通过 `base_url` / `api_key` / `model` 接入任意兼容端点
- **5 级 Effort**：每级可独立配置模型和 thinking 预算
- **Fallback 降级**：主模型不可用时自动切换到 `llm_fallback_model`
- **Prompt 缓存**：三级降级策略（显式缓存控制 → 隐式前缀稳定 → 无缓存）
- **缓存决策引擎**：智能判断何时启用/复用缓存
- **流式响应**：支持 SSE 流式输出

---

### 权限与安全

#### 权限模式

| 模式 | 行为 |
| --- | --- |
| `default` | 写工程/执行进程需前端确认，只读工具自动放行 |
| `plan` | 只生成计划，不执行任何工具 |
| `auto_approve` | 自动批准所有工具（未信任工程降级为 default） |
| `read_only` | 只允许只读工具 |
| `full_access` | 硬安全边界通过后直接允许所有工具调用（路径沙箱仍生效） |

#### 权限检查流程

```mermaid
graph TD
    A[工具调用] --> B{路径边界}
    B -->|越界| DENY[拒绝]
    B -->|通过| C{工具域启用}
    C -->|未启用| DENY
    C -->|通过| D{Agent 可见工具}
    D -->|不可见| DENY
    D -->|可见| E{deny 规则}
    E -->|匹配| DENY
    E -->|不匹配| F{allow 规则 + trusted}
    F -->|匹配且信任| ALLOW[允许]
    F -->|不匹配| G{会话级 allow}
    G -->|已授权| ALLOW
    G -->|未授权| H{模式默认}
    H -->|full_access| ALLOW
    H -->|auto_approve + trusted| ALLOW
    H -->|default / mutating| ASK[前端确认]
    H -->|read_only + mutating| DENY
```

#### 安全规则

- 默认只绑定 `127.0.0.1`，HTTP 请求需 Bearer token
- server 工具只能访问 `AI_AGENT_PROJECT_ROOT` 内路径
- `.git/`、`.godot/` 默认禁止读写，`addons/` 默认禁止写入
- 所有写工程和执行进程的工具必须经前端确认
- `run_tests`、`run_headless_self_test` 只读取本地 EditorSettings 配置，模型不能传任意命令
- `run_system_command` 需用户预览确认，支持多种 shell（PowerShell/CMD/sh/bash/zsh）
- `execute_gd_script` 只执行 SceneTree/MainLoop 脚本，拒绝 EditorScript
- Agent、Skill、OutputStyle 只是提示词/配置资产，不能授予新权限

---

### Skill / OutputStyle 扩展

#### Skill

Markdown frontmatter + body 格式，每个 Skill 一个子目录：

```text
~/.ai_agent/skills/         # 用户级
.ai_agent/skills/            # 项目级（未信任工程不启用）
```

```markdown
---
name: my-skill
description: 一句话描述
tags: [godot, gameplay]
---

# My Skill

具体指令...
```

#### OutputStyle

```text
.ai_agent/output_styles/    # 项目级
```

```markdown
---
name: concise
description: 简洁输出风格
---

回复规则...
```

内置 OutputStyle：`default` / `concise` / `review`。

通过 `/commands/set_output_style` 或 `/chat` 请求的 `output_style` 字段切换。

---

### 验证系统

编辑类工具（`propose_script_edit`、`apply_text_edit`、`propose_tests`、`propose_content_file`）成功落地后自动触发两阶段校验：

| 阶段 | 方式 | 说明 |
| --- | --- | --- |
| **Phase 1** | Godot CLI 语法快检 | `godot --headless --check-only`，超时 10 秒 |
| **Phase 2** | LLM 语义校验 | 使用 `verify` effort，分析上下文一致性 |

校验失败时自动尝试修复，单文件最多重试 `verify_max_retries`（默认 2）次。

---

### 会话与恢复

- **会话持久化**：`session_store_dir` 目录下按 session_id 存储对话历史
- **恢复指针**：`recovery_pointer.json` 记录最后事件序号和 pending turn
- **前端检测**：插件启动时读取恢复指针，若存在且 `project_hash` 匹配则提示用户恢复
- **自动压缩**：会话历史体积超过 `auto_compact_token_threshold`（默认 160k tokens）时自动压缩早期上下文，保留最近消息和 pending 状态
- **LLM 语义压缩**：可选调用 LLM 将旧摘要与移除消息融合为连贯摘要，失败时回退到机械拼接
- **手动压缩**：`compact` 命令支持 `keep_recent` 和 `use_llm` 参数

---

### MCP 服务器

```powershell
'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python -m app --mcp-stdio
```

提供只读/服务端工具的 stdio JSON-RPC 入口，适合外部 IDE 或 CLI 集成。

---

### 记忆系统

- **存储**：项目级 JSON 文件（`memory_store_path`）
- **读写**：`GET /memory` 读取、`POST /memory` 写入
- **不保存**：token、API key 或完整敏感对话
- **前端面板**：Memory 面板可视化查看

---

### Doctor 自检

`GET /doctor` 返回：

- LLM 端点连通性
- 认证状态
- 已注册工具列表
- Skill / OutputStyle 状态
- 记忆存储状态
- 安全配置告警

---

## 前端插件 (`ai_agent_frontend`)

### 安装与启用

`ai_agent_frontend/` 是独立 Godot 插件工程。实际使用时，把插件目录复制或软链接到目标 Godot 项目的 `addons/` 下：

```text
<your-godot-project>/
  addons/
    ai_agent/
```

然后在 Godot 中启用插件：

```text
Project > Project Settings > Plugins > AI Agent
```

推荐先使用插件的自动启动模式（`auto_start_service = true`），这样 token 会通过 stdin 传给 Python 服务，不会出现在命令行参数里。

<!-- 📸 在此处插入插件启用截图 -->
<!-- ![启用插件](docs/images/plugin-enabled.png) -->

---

### UI 面板

| 面板 | 文件 | 说明 |
| --- | --- | --- |
| **聊天面板** | `chat_panel.gd` | 主对话界面，支持 Markdown 渲染、代码高亮、工具预览卡片、虚拟滚动 |
| **命令面板** | `command_palette.gd` | `/` 触发的命令快捷入口 |
| **Doctor 面板** | `doctor_panel.gd` | 服务自检结果可视化 |
| **Memory 面板** | `memory_panel.gd` | 项目记忆查看/管理 |
| **扩展面板** | `extension_panel.gd` | Skill / OutputStyle 浏览与切换 |
| **预览确认面板** | `preview_confirm_panel.gd` | 写工具的 diff 预览与确认/拒绝 |
| **内联工具确认** | `inline_tool_confirmation.gd` | 聊天流中的内联工具确认卡片 |
| **恢复提示** | `recovery_prompt.gd` | 崩溃恢复提示对话框 |

辅助渲染：

| 组件 | 文件 | 说明 |
| --- | --- | --- |
| **Markdown 渲染器** | `markdown_renderer.gd` | Markdown → RichTextLabel BBCode |
| **工具预览渲染器** | `tool_preview_renderer.gd` | 工具 diff/列表/日志的预览渲染 |
| **日志条目渲染器** | `log_entry_renderer.gd` | Agent 事件日志条目渲染 |
| **事件格式化器** | `event_formatter.gd` | 事件类型 → 可读文本 |
| **聊天面板主题** | `chat_panel_theme.gd` | 聊天面板主题/配色 |
| **聊天面板文本** | `chat_panel_text.gd` | 文本处理/复制/选择 |
| **聊天消息存储** | `chat_message_store.gd` | 消息持久化存储 |
| **聊天节点工厂** | `chat_node_factory.gd` | 消息 UI 节点工厂 |
| **虚拟滚动器** | `chat_virtual_scroller.gd` | 长会话虚拟滚动渲染 |

<!-- 📸 在此处插入聊天面板截图 -->
<!-- ![聊天面板](docs/images/chat-panel.png) -->

<!-- 📸 在此处插入命令面板截图 -->
<!-- ![命令面板](docs/images/command-palette.png) -->

<!-- 📸 在此处插入 Doctor 面板截图 -->
<!-- ![Doctor 面板](docs/images/doctor-panel.png) -->

<!-- 📸 在此处插入预览确认截图 -->
<!-- ![预览确认](docs/images/preview-confirm.png) -->

<!-- 📸 在此处插入扩展面板截图 -->
<!-- ![扩展面板](docs/images/extension-panel.png) -->

<!-- 📸 在此处插入 Memory 面板截图 -->
<!-- ![Memory 面板](docs/images/memory-panel.png) -->

---

### 上下文采集

| 采集器 | 文件 | 说明 |
| --- | --- | --- |
| **上下文采集器** | `context_collector.gd` | 汇总当前选区、场景树、项目文件等 |
| **ClassDB 阅读器** | `classdb_reader.gd` | 读取 Godot 内置类文档 |
| **诊断采集器** | `diagnostics_collector.gd` | 采集编辑器诊断错误/警告 |
| **文件状态缓存** | `file_state_cache.gd` | 缓存已读文件内容，避免重复 IO |

每轮对话时，前端自动采集上下文并通过 `ChatRequest.context` 发送给后端。

---

### 前端工具

| 模块 | 文件 | 工具 |
| --- | --- | --- |
| **工具执行器** | `tool_executor.gd` | 接收后端 `tool_calls`，分发到对应模块执行 |
| **编程工具** | `program_tools.gd` | `propose_script_edit`、`apply_text_edit`、`propose_tests`、`run_tests`、`run_headless_self_test`、`run_system_command`、`execute_gd_script`、`git_status`、`git_diff`、`export_project` |
| **场景工具** | `scene_tools.gd` | `read_scene_tree`、`add_node`、`set_node_property`、`delete_node`、`reparent_node`、`rename_node`、`instance_scene`、`duplicate_node`、`connect_signal`、`disconnect_signal`、`add_to_group`、`remove_from_group`、`validate_scene_state`、`save_scene`、`open_scene`、`capture_viewport_screenshot`、`bake_navigation_mesh`、`set_project_setting`、`read_project_setting`、`list_autoloads`、`add_autoload`、`remove_autoload`、`list_input_actions`、`add_input_action`、`remove_input_action` |
| **地图工具** | `map_tools.gd` | `describe_tilemap_selection`、`describe_map_context`、`describe_map_region`、`convert_map_coords`、`edit_map`、`paint_terrain_connect`、`place_map_objects`、`validate_map_region`、`repair_map_region`、`query_spatial_index`、`compact_spatial_index`、`validate_layer_coverage`、`repair_layer_coverage` 等 |
| **地图子系统** | `map_intent_parser.gd`、`map_layout_planner.gd`、`map_algorithms.gd`、`map_blueprints.gd`、`map_layer_scaffold.gd`、`map_platform_plan_validator.gd`、`map_reachable_growth.gd`、`map_validator.gd` | 意图解析、布局规划、算法计划、蓝图组合、图层脚手架、平台关卡验证、可达性增长、区域校验 |
| **资源工具** | `resource_tools.gd` | `create_resource`、`read_image_metadata`、`create_sprite_frames_from_sheet`、`read_resource`、`set_resource_property`、`create_animation_track`、`create_shader_material`、`propose_content_file` |
| **项目工具** | `project_tools.gd` | 项目设置、自动加载、输入映射、导出预设等 |
| **路径工具** | `path_utils.gd` | `res://` ↔ 绝对路径互转 |

---

### UndoRedo 集成

`unified_undo_manager.gd` 将所有写操作包装为 Godot `UndoRedo` action：

- 每个写工具执行前创建 action
- 记录旧文件内容 / 旧场景状态
- 用户可通过 `Ctrl+Z` 撤销 AI 的任何改动
- 支持跨工具的统一撤销栈

---

### 恢复提示

`recovery_pointer.gd` 在每次事件后更新恢复指针。插件启动时：

1. 读取 `/recovery-pointer`
2. 校验 `project_hash` 是否匹配当前工程
3. 若匹配则弹出恢复提示，用户可选择恢复或重新开始

---

### EditorSettings 完整参考

在 Godot 编辑器中通过 `Edit > Editor Settings > Ai Agent` 配置：

#### 服务连接

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/service_url` | string | `http://127.0.0.1:8765` | 服务地址 |
| `ai_agent/auto_start_service` | bool | `false` | 是否由插件自动启动 Python 服务 |
| `ai_agent/python_executable` | string | *(空=自动检测)* | Python 可执行文件路径 |
| `ai_agent/service_module_dir` | string | *(空)* | `ai_agent_service` 目录的绝对路径 |

#### 会话 & UI

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/session_id` | string | `default` | 当前会话 ID |
| `ai_agent/ui_language` | string | `zh` | UI 语言（`zh` / `en`） |
| `ai_agent/permission_mode` | string | `default` | 权限模式：`default` / `plan` / `auto_approve` / `read_only` / `full_access` |
| `ai_agent/effort` | string | `standard` | Effort 档位：`quick` / `standard` / `deep` / `verify` / `advisor` |
| `ai_agent/output_style` | string | `default` | OutputStyle 名称 |
| `ai_agent/trusted_project_extensions` | bool | `false` | 是否信任项目扩展（允许项目级 Skill） |
| `ai_agent/show_recovery_prompt` | bool | `true` | 是否显示崩溃恢复提示 |
| `ai_agent/session_history_json` | string | *(空)* | 会话历史 JSON（内部缓存） |

#### LLM 配置

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/llm_base_url` | string | `https://api.openai.com/v1` | OpenAI 兼容 Chat Completions 端点 |
| `ai_agent/llm_api_key` | string | *(空)* | 大模型 API key |
| `ai_agent/llm_model` | string | `gpt-4o-mini` | 默认对话模型 |
| `ai_agent/llm_quick_model` | string | *(空=用 llm_model)* | quick effort 模型 |
| `ai_agent/llm_standard_model` | string | *(空=用 llm_model)* | standard effort 模型 |
| `ai_agent/llm_deep_model` | string | *(空=用 llm_model)* | deep effort 模型 |
| `ai_agent/llm_verify_model` | string | *(空=用 llm_model)* | verify effort 模型 |
| `ai_agent/llm_advisor_model` | string | *(空=用 llm_model)* | advisor effort 模型 |
| `ai_agent/llm_fallback_model` | string | *(空=不降级)* | 主模型不可用时的降级模型 |
| `ai_agent/llm_request_timeout_s` | float | `60.0` | 单次 LLM 请求超时（秒） |

#### 压缩 & 超时

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/compact_summary_use_llm` | string | `default` | 压缩摘要是否使用 LLM 语义压缩：`default`（跟随服务端）/ `on` / `off` |
| `ai_agent/compact_summary_model` | string | *(空)* | 压缩摘要使用的 LLM 模型名 |
| `ai_agent/request_timeout_sec` | float | `30.0` | 轻量级 API 请求超时（秒） |
| `ai_agent/chat_request_timeout_sec` | float | `300.0` | `/chat` 请求超时（秒），收到事件后重置 |

#### RAG & Embedding

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/embedding_provider` | string | `disabled` | Embedding 提供方：`disabled` / `openai` / `local` / `bge-m3` |
| `ai_agent/embedding_model` | string | `text-embedding-3-small` | Embedding 模型名 |
| `ai_agent/embedding_endpoint` | string | `https://api.openai.com/v1` | Embedding API 端点 |
| `ai_agent/embedding_api_key` | string | *(空)* | Embedding API key |
| `ai_agent/embedding_timeout_s` | float | `3.0` | Embedding 请求超时（秒） |
| `ai_agent/embedding_retries` | int | `1` | Embedding 重试次数 |
| `ai_agent/rerank_model` | string | *(空=跳过)* | 交叉编码器重排模型名 |
| `ai_agent/rerank_timeout_s` | float | `2.0` | 重排请求超时（秒） |
| `ai_agent/rag_query_router_enabled` | bool | `true` | 是否启用查询路由 |
| `ai_agent/rag_token_budget` | int | `1500` | RAG 注入 prompt 的 token 预算 |
| `ai_agent/graph_max_depth` | int | `2` | 图检索最大深度 |
| `ai_agent/graph_max_neighbors` | int | `5` | 图检索每节点最大邻居数 |

#### 资产理解

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/asset_understanding_enabled` | bool | `false` | 是否启用资产理解（图片→描述） |
| `ai_agent/asset_understanding_model` | string | *(空)* | 资产理解模型名 |
| `ai_agent/asset_understanding_endpoint` | string | *(空)* | 资产理解 API 端点 |
| `ai_agent/asset_understanding_api_key` | string | *(空)* | 资产理解 API key |
| `ai_agent/asset_understanding_timeout_s` | float | `10.0` | 资产理解请求超时（秒） |
| `ai_agent/asset_understanding_max_tokens` | int | `500` | 资产理解最大输出 token |

#### 日志 & 事件

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/log_level` | string | `info` | 日志等级：`debug` / `info` / `warning` / `error` |
| `ai_agent/log_to_file` | bool | `true` | 是否写入日志文件 |
| `ai_agent/log_file_path` | string | `res://logs/ai_agent_frontend.log` | 前端日志文件路径 |
| `ai_agent/enable_event_stream` | bool | `true` | 是否启用事件流轮询 |
| `ai_agent/event_poll_interval_sec` | float | `1.0` | 事件轮询间隔（秒） |
| `ai_agent/enable_lsp_diagnostics` | bool | `true` | 是否启用 LSP 诊断采集 |

#### 测试 & Headless & 超时

| EditorSettings key | 类型 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `ai_agent/test_executable` | string | *(空)* | 项目测试 runner 可执行文件 |
| `ai_agent/test_args` | string | *(空)* | 测试 runner 参数 |
| `ai_agent/test_output_log` | string | *(空)* | 测试输出日志路径 |
| `ai_agent/headless_executable` | string | *(空)* | M3 headless 自测 runner 可执行文件 |
| `ai_agent/headless_args` | string | *(空)* | Headless runner 参数 |
| `ai_agent/headless_output_log` | string | *(空)* | Headless 输出日志路径 |
| `ai_agent/runner_timeout_ms` | int | `120000` | Runner 超时（毫秒） |
| `ai_agent/system_command_timeout_ms` | int | `120000` | 系统命令执行超时（毫秒） |
| `ai_agent/gd_script_timeout_ms` | int | `60000` | GDScript 脚本执行超时（毫秒） |
| `ai_agent/export_timeout_ms` | int | `600000` | 项目导出超时（毫秒） |

<!-- 📸 在此处插入 EditorSettings 配置截图 -->
<!-- ![EditorSettings](docs/images/editor-settings.png) -->

---

## 安全模型

- 默认只绑定本机地址（`127.0.0.1`），HTTP 请求需要 Bearer token
- server 工具只能访问 `AI_AGENT_PROJECT_ROOT` 内路径
- `.git/`、`.godot/` 默认禁止读写，`addons/` 默认禁止写入
- 所有写工程和执行进程的工具必须经前端预览确认
- `run_tests`、`run_headless_self_test` 只读取本地 EditorSettings 中配置的可执行文件和参数，模型不能传任意命令
- `run_system_command` 允许执行系统命令但必须经用户预览确认
- `execute_gd_script` 只执行 SceneTree/MainLoop 脚本，拒绝 EditorScript
- Agent、Skill、OutputStyle 只是提示词/配置资产，不能授予新权限，也不能绕过权限闸
- Token 来源：`--token-stdin`（推荐，通过 stdin 传入）、`AI_AGENT_AUTH_TOKEN` 环境变量、或自动生成一次性 token

---

## 开发自检

后端语法检查：

```powershell
cd ai_agent_service
python -m compileall app
```

MCP stdio 快速检查：

```powershell
'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}' | python -m app --mcp-stdio
```

格式空白检查：

```powershell
git diff --check -- ai_agent_service ai_agent_frontend
```

运行测试：

```powershell
cd ai_agent_service
python -m pytest tests/
```

当前本机环境中还需要额外安装/配置后才能运行：

- `python -m mypy app`：需要安装 `mypy`
- Godot GDScript CLI 校验：需要把 `godot` 加入 PATH

---

## 故障排查

| 问题 | 排查 |
| --- | --- |
| LLM 不可达 | 检查 `AI_AGENT_LLM_BASE_URL` 和 `AI_AGENT_LLM_API_KEY`；用 `/doctor` 诊断 |
| 工具被拒绝 | 检查权限模式和路径沙箱；确认 `AI_AGENT_PROJECT_ROOT` 指向正确的工程根目录 |
| 索引为空 | 执行 `/commands/rebuild_index` 重建 RAG 索引，或等待自动增量构建完成 |
| 插件连不上服务 | 检查 `ai_agent/service_url` 和 `ai_agent/auto_start_service`；查看服务是否已启动 |
| Token 不匹配 | 确认前端和后端使用相同的 auth token |
| 恢复提示异常 | 删除 `.ai_agent_service/recovery_pointer.json` 清除恢复指针 |
| 会话上下文过长 | 调整 `auto_compact_token_threshold` 或手动执行 `/commands/compact` |
| 地图编辑版本冲突 | 检查 `map_revision`，使用 `describe_map_region` 获取最新 revision 后重试 |
| 对象放置被拒绝 | 使用 `find_placement_anchors` 查找合法锚点，或检查 `validate_object_placements` 结果 |
