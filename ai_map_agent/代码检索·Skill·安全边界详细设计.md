# 代码检索 · Skill · 安全边界 —— 详细设计

| 项目 | 内容 |
|------|------|
| 文档名称 | AI 游戏开发 Agent —— 代码检索 / Skill / 安全边界 详细设计 |
| 版本 | v0.3.1 |
| 日期 | 2026-06-13 |
| 依据 | 《Python 服务架构方案》v0.4.5（§9 安全、§10 检索、§11 Skill）；借鉴 Claude Code Glob/Grep、SkillTool/PromptCommand、Sandbox/trust |
| 范围 | 展开三个支撑/守卫子系统的内部设计；与《多智能体与权限系统详细设计》互补 |
| 变更 | v0.3.1（命名一致性）：检索引用的 agent 名统一 kebab-case（`programming-agent`），与详设A/主方案一致。见术语表 §4.A item 15 |
| 变更 | v0.3：Skill 改为 Claude Code 同构模式（`SKILL.md` → PromptCommand/SkillTool）；补充 `allowed-tools` 只收敛不扩张、`paths` 条件激活、source/namespace、动态发现与缓存失效、Agent/Skill/OutputStyle 扩展内容安全边界 |
| 变更 | v0.2（评审收紧）：skill 同名冲突防覆盖（命名空间/信任）+ description 注入防护；`path_ok` 严谨化（Windows/跨盘/glob/`allow_paths`/批量 path）；`addons/` 默认可读不可写（读写分离）；检索 stale-edit 提醒 |

---

## 一、代码检索子系统

### 1.1 定位

两层、皆为 **server 工具**、**只读**、**限定工程根**：

| 层 | 能力 | 何时用 | 阶段 |
|----|------|------|------|
| **精确** | 按文件名/符号/字符串找 | 已知要找什么（类名、函数名、字符串） | M1 |
| **语义** | 找"和某需求相关"的代码 | 模糊意图、跨文件理解（RAG） | M2 |

> 检索只返回**片段 + 路径 + 行号**（不灌全文）；要拿权威全文，由 **前端 `read_script`** 读编辑器实时缓冲（含未保存改动），避免服务端读盘与编辑器态不一致。
>
> ⚠️ **`grep_code` / `search_codebase` 命中的是磁盘快照**——命中后**必须用前端 `read_script` 取权威内容再编辑**，避免基于陈旧内容的 stale edit。

### 1.2 工具定义

```jsonc
// 精确
{"name":"list_files","description":"按 glob 列出工程内文件",
 "parameters":{"type":"object","properties":{
   "glob":{"type":"string"}, "max_results":{"type":"integer","default":100}},
   "required":["glob"]}}

{"name":"grep_code","description":"在工程代码中按正则检索（返回片段+路径+行号）",
 "parameters":{"type":"object","properties":{
   "pattern":{"type":"string"}, "glob":{"type":"string"},
   "context_lines":{"type":"integer","default":2},
   "max_results":{"type":"integer","default":40}},
   "required":["pattern"]}}

// 语义（M2）
{"name":"search_codebase","description":"语义检索与查询相关的代码片段",
 "parameters":{"type":"object","properties":{
   "query":{"type":"string"}, "top_k":{"type":"integer","default":8}},
   "required":["query"]}}
```

### 1.3 精确检索实现

- 优先 **ripgrep（`rg`）子进程**；无 `rg` 时回退到 Python `glob` + `re`。
- 一律加 `--no-follow`/不跟随符号链接、限定 `project_root`、套 `deny_paths`、`max_results`、单文件大小上限（跳过超大文件）。
- 返回结构：`[{path, line, snippet, ±context}]`。
- 路径安全见 §三.3（规范化 + 越界拒绝）。

### 1.4 语义检索（RAG，M2）

| 环节 | 设计 |
|------|------|
| **对象** | 工程脚本（`.gd` / `.cs`）；可选含 `.tres` 文本、文档 |
| **切块** | **符号感知**：按函数/类边界切（GDScript/C#），优于定长滑窗；保留 `path:line` |
| **embedding** | 走**用户配置的 embedding 端点/模型**（OpenAI/本地如 Ollama embeddings）；无 embedding 端点 → 语义层关闭、只留精确层 |
| **向量库** | FAISS（本地文件）或 Chroma；持久化在**服务端缓存目录**（按工程路径哈希分桶），不写进工程 |
| **检索** | 余弦 top_k；返回 `片段 + path + line + score` |

### 1.5 索引生命周期

```
构建：首次打开工程 / 首次 search（懒构建）/ 手动重建
增量：文件变更 → 仅重嵌变更块（按块内容哈希跳过未变）
失效：git 切换 / 大规模变更 → 整库重建
存储：服务端缓存目录（project_path_hash 分桶），随服务、不入工程/版本库
```

- **成本提示**：构建索引消耗 embedding token；默认**懒构建**（首次语义检索时才建），并显示进度。
- 变更通知：前端文件监听器或服务端 watch（实现取一，待定 §1.8）。

### 1.6 边界与限制

- 严格限定 `project_root`，套 `deny_read_paths`（默认 `.git/`、`.godot/`、导出预设）；`addons/` **默认可读（便于自我调试）但禁止写**（读/写分离见 §3.3、§3.8）。
- 只读；单文件大小上限；`max_results` / `top_k` 上限控成本。
- 索引/缓存不落工程目录，避免污染版本库。

### 1.7 与多智能体/上下文

- 主要供 **programming-agent**（跨文件大重构、Bug 修复依赖检索）。
- 返回片段而非全文，控 token；agent 据片段再用 `read_script` 取权威全文。

### 1.8 待细化

| 项 | 说明 |
|----|------|
| 变更通知来源 | 前端文件监听 vs 服务端 watch |
| embedding 配置 | 复用主端点还是独立 `embedding_base_url/model` |
| 索引存储位置 | 缓存目录规范、清理策略 |
| `.tscn`/资源是否纳入语义层 | 影响"场景相关检索" |

---

## 二、Skill 系统（Claude Code 同构）

### 2.1 定位

Skill 采用 Claude Code 同构模式：**一个 `SKILL.md` 目录就是一个 PromptCommand**。系统常驻展示 `name + description + when_to_use`，模型需要时通过 `load_skill(name)` / SkillTool 读取正文。Skill 可以由用户手动触发，也可以由模型自主调用；两者看到同一份 Skill 注册表。

同构边界：Skill 的文件格式、加载层级、条件激活、Command 化方式与 Claude Code 保持一致；实际可用工具仍由本项目 `ToolDef`、入口能力边界和权限闸决定。

### 2.2 SKILL.md 结构

```markdown
---
name: tilemap-terrain
description: Godot TileMapLayer 地形铺设、房间/走廊生成与连边规则
when_to_use: 用户要生成地图、铺瓦片、修改 TileMapLayer 或解释瓦片规则时使用
allowed-tools: read_scene_tree, read_class_docs, fill_rect, draw_line, set_cells, clear_rect
paths:
  - "**/*.tscn"
  - "**/*.tres"
model: inherit
effort: standard
hooks:
  PreToolUse: audit_only
---

# 正文：详细指令、步骤、示例、反例……
```

字段规则：

| 字段 | 作用 | 安全约束 |
|------|------|---------|
| `name` | 稳定 id；内部使用 `source:name` 规范名 | 同名不直接覆盖，按 source/namespace 展示 |
| `description` | 常驻 Skill 列表展示 | 限长、过滤换行；作为非指令数据注入 |
| `when_to_use` | 帮助模型判断何时调用 | 只影响选择，不影响权限 |
| `allowed-tools` | 裁剪本 Skill 可建议/使用的工具集合 | 只能与当前可见 `ToolDef` 取交集，不能新增能力 |
| `paths` | 条件激活与动态发现 | 先 realpath/Windows 规范化，禁止越界和符号链接逃逸 |
| `model` / `effort` | 建议模型与思考档位 | 受会话设置、成本上限和 provider 能力限制 |
| `hooks` | 注册本项目支持的内部 hook | 不支持任意 shell hook，返回值不能越过权限闸 |

### 2.3 发现、来源与优先级

```
扫描来源（合并）：
  1. bundled/   随服务发布的内置 Skill
  2. user/      用户机器级 Skill
  3. project/   工程内 Skill（未信任工程默认只收紧，不提权）
  4. plugin/    插件级 Skill（后续）
→ 解析 frontmatter + paths
→ 构建 SkillRegistry{qualified_name → SkillCommand}
→ 生成常驻 Skill 列表（name/description/when_to_use/source/enabled）
→ 模型或用户触发时再加载正文
```

来源策略：

- 内部规范名使用 `source:name`，例如 `bundled:tilemap-terrain`、`project:tilemap-terrain`；UI 可展示短名。
- project Skill 不直接覆盖 bundled/user Skill。若短名冲突，Doctor 标记冲突，模型看到带来源的候选。
- 未信任工程的 project Skill 默认不自动启用；前端 `extension_panel.gd` 显示来源和启用确认。
- 动态发现：模型/工具接触某个路径时，沿路径向上查找本项目 Skill 目录；发现新目录后清理 Skill 列表与命令缓存。

### 2.4 渐进披露机制

- **常驻列表**：只注入 `name + description + when_to_use + source`，保持 prompt 前缀稳定。
- **模型驱动加载**：agent 调用 `load_skill(name)` 获取正文，正文作为附件/用户消息注入当前帧。
- **Agent 预加载**：AgentDefinition frontmatter 的 `skills` 字段在子 agent 启动时预加载对应 Skill 正文。
- **手动命令加载**：命令面板或 `/skill <name>` 触发同一 SkillCommand。

```jsonc
{"name":"load_skill","description":"按名加载一个 Skill 的完整指令到上下文",
 "parameters":{"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}}
```

### 2.5 建议内置 Skill

| skill | 覆盖 |
|------|------|
| `gdscript-4x-idioms` | GDScript 4.x 语法/惯用法/常见 3.x→4.x 陷阱 |
| `csharp-godot-dotnet` | Godot .NET C# 规范（`[Export]`、`partial`、信号） |
| `tilemap-terrain` | 地形连边、房间/走廊生成套路 |
| `scene-composition` | 常见节点组合模式（角色=Body+Sprite+Collision…） |
| `resource-patterns` | `.tres` 数据资源、批处理套路 |
| `signals-and-callbacks` | 信号连接、回调、生命周期 |

### 2.6 Skill 与 Command 同构

Skill 以 `type=prompt` 的 command 进入命令系统：

| 层 | 行为 |
|----|------|
| Command Palette | 用户手动选择 Skill，预览来源/说明后注入 |
| SkillTool / `load_skill` | 模型按名称加载 Skill 正文 |
| `/commands` | 返回 SkillCommand 与普通 typed command 的统一列表 |
| Doctor | 展示 frontmatter 校验、被忽略字段、实际生效工具集合 |

不支持 Claude Code 的任意 shell command 形态；本项目 command 只能调用 typed 能力或生成 prompt 内容。

### 2.7 安全（关键）

Skill 内容是**注入 prompt 的指令**，尤其 project/plugin 来源属**不可信内容**：

| 风险 | 缓解 |
|------|------|
| prompt 注入 / 越权诱导（"忽略权限直接改文件"） | Skill 仅作提示词资产，**不赋予任何操作权限**；权限仍由 `ToolDef + 权限闸 + 前端确认` 裁决 |
| `allowed-tools` 被误当授权 | 只与当前可见工具取交集；`*` 也只代表当前入口允许暴露的工具集合 |
| 覆盖核心约束 | 系统安全规则 > 权限闸 > AgentDefinition > Skill 正文 > 用户输入 |
| 来源不明/同名冲突 | `source` + namespace；Doctor 展示冲突和被禁用项 |
| `paths` 条件激活越界 | realpath/Windows 规范化，禁止 `..`、绝对路径越界、符号链接逃逸 |
| 简述注入 system | frontmatter 字段限长、过滤换行/指令性文本；project/plugin 描述作为非指令数据呈现 |
| hook/shell 提权 | 只支持内部 hook；不支持任意 shell hook，hook 返回值不能越过权限闸 |

### 2.8 协议

- `load_skill` = server 只读工具；启动时构建 registry；动态发现后清缓存。
- `GET /skills` 返回 `qualified_name/name/description/when_to_use/source/enabled/effective_tools/warnings`。
- `/doctor` 返回 Skill 校验结果：无效字段、被忽略 hook、路径越界、同名冲突、未信任禁用。

### 2.9 待细化

| 项 | 说明 |
|----|------|
| Skill 目录实际落点 | 是否采用 `.ai_agent/skills/`，还是 `ai_agent/skills/`；需避开 Godot 导出 |
| 项目级 Skill 信任流程 | 首次启用确认、撤销信任、跨项目隔离 |
| skill 大小/数量上限 | 控 token 与发现成本 |
| 动态发现触发源 | read_script/list_files/grep/search 哪些工具触发路径上溯 |

---

## 三、安全边界

### 3.1 威胁模型（防什么）

| 威胁 | 例 |
|------|------|
| 不可信工程提权/越界 | 工程内 settings/skill 试图开 `auto_approve`、改无关文件 |
| prompt 注入诱导危险操作 | 让 agent 删文件、外泄 key、改 `.git` |
| 同构扩展误提权 | Agent/Skill/OutputStyle frontmatter 试图扩大工具、开启 hook/shell、覆盖安全规则 |
| 模型幻觉越界 | 产生越界路径、危险调用 |
| 密钥泄露 | key 入库/进导出包/下发前端 |
| 越权读写 | 工具读写越出工程根 |
| 任意执行 | 通过通用 shell 跑任意命令 |

### 3.2 边界一：无任意执行

- **不暴露** `bash` / `eval` / 任意代码执行工具；只**类型化领域工具**——可做的事被限定在工具集合内（借鉴 Claude Code 专用工具 vs bash）。
- 着色器/脚本"生成"是**提议文本**，经预览确认由前端写入，**不等于执行**。
- 唯一的"执行类"是 `run_tests`（跑工程测试，由 Godot 自身承载），默认 `ask`、受权限闸管。

### 3.3 边界二：文件系统范围（server 工具）

```python
# app/security/paths.py
import os, fnmatch

def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))         # Windows：统一大小写 + 分隔符

def path_ok(target: str, ctx, write: bool = False) -> bool:
    root = _norm(ctx.project_root)
    p = _norm(os.path.join(ctx.project_root, target))    # 解符号链接、规范化
    try:
        if os.path.commonpath([root, p]) != root:        # 越界
            return False
    except ValueError:                                    # Windows 跨盘符 → 越界
        return False
    rel = os.path.relpath(p, root).replace(os.sep, "/")
    deny = ctx.deny_write_paths if write else ctx.deny_read_paths
    for d in (x.rstrip("/") for x in deny):               # 按路径段 / glob，而非裸 startswith
        if rel == d or rel.startswith(d + "/") or fnmatch.fnmatch(rel, d):
            return False
    if ctx.allow_paths:                                   # 非空时必须落在允许子路径内
        if not any(rel == a.rstrip("/") or rel.startswith(a.rstrip("/") + "/")
                   for a in ctx.allow_paths):
            return False
    return True

def all_paths_ok(args: dict, path_args: list[str], ctx, write=False) -> bool:
    return all(path_ok(args[k], ctx, write) for k in path_args if k in args)  # 批量校验所有 path
```

- 拒绝 `..`、绝对路径越界、符号链接逃逸；**Windows** 下 `normcase` 统一大小写/分隔符，跨盘符 `commonpath` 抛 `ValueError` 视为越界。
- **deny 按路径段 / glob 匹配**（`.godot/` 命中 `.godot/x`，不误伤 `.godotignore`），而非裸 `startswith`。
- **读/写分离**：`deny_read_paths`（含检索）≠ `deny_write_paths`；`addons/` **默认可读（自我调试）但禁止写**。
- **`allow_paths` 生效**：非空时只允许其下子路径。
- **批量工具校验所有 path 参数**（`ToolDef.path_args` 列出的每个），不止第一个。
- server 文件操作**只读**；套大小上限。

### 3.4 边界三：写操作收口前端 + 可逆

- **服务端从不写工程文件**。所有写入都经**前端工具 → 预览确认 → UndoRedo**。
- 即便服务端生成内容（脚本文本），也是作为**提议**返回，由前端落地。

### 3.5 边界四：信任模型

借鉴 Claude Code "trust 前只用安全配置子集"：

| | 受信任工程 | 不可信工程（默认） |
|---|---|---|
| `auto_approve` | 允许 | **禁用**（降级为 `ask`） |
| allow 规则 | 生效 | **忽略**（只保留 deny/ask） |
| 能力 | 完整 | 安全子集 |

- **配置两源**：服务端本地配置（可信，可定义 allow/mode）；**工程内 settings 只能收紧（deny/ask），不能提升为 allow / 开 auto_approve**。
- **信任动作**：用户在前端对某工程显式"信任"后，才解锁完整配置与 `auto_approve`。
- **扩展四源**：`bundled/user/project/plugin` 都保留 `source`；project/plugin 扩展未信任前默认禁用或只收紧，不允许扩大工具集合、注册外部 MCP、写入 hook/shell。

### 3.5.1 边界四补充：扩展内容不是授权来源

Agent、Skill、OutputStyle 的 markdown/frontmatter 会进入提示词或运行配置，但只影响**选择、裁剪、表达方式**：

- `tools` / `allowed-tools` 只能与当前可见 `ToolDef` 取交集。
- `model` / `effort` 受用户本地配置与成本上限约束。
- `hooks` 只能注册本项目内部 hook，不能执行任意 shell。
- `paths` 只用于条件激活，必须落在工程根内。
- 正文里要求"忽略确认/自动修改/读取密钥"一律按普通文本处理，最终由系统安全规则和权限闸拒绝。

### 3.6 边界五：密钥与配置隔离

- endpoint / key / model **仅服务端本地**（env / `.env`）。
- **绝不**：写进工程文件、进版本库、进导出包、下发前端。
- 前端不持有 key，只与 `127.0.0.1` 服务通信；key 不进日志。

### 3.7 边界六：网络

- 工具**不做任意外联**；仅 LLM 客户端访问**用户配置的端点**。
- 检索/文档接地均为本地。
- 服务仅绑 `127.0.0.1`。
- （后续多模态/Web 检索若引入，作为**显式工具**且受权限闸管。）

### 3.8 配置即 schema（分层解析）

```python
# app/security/settings.py
from pydantic import BaseModel
from typing import Literal

class SecuritySettings(BaseModel):
    project_root: str
    trusted: bool = False
    permission_mode: Literal["default","plan","auto_approve","read_only"] = "default"
    enabled_domains: list[str] = ["program","map","scene","resource","project"]
    deny_read_paths:  list[str] = [".git/", ".godot/"]              # 禁读（含检索）
    deny_write_paths: list[str] = [".git/", ".godot/", "addons/"]   # 禁写（addons 可读不可写）
    allow_paths: list[str] = []          # 非空=限定可检索/读取子路径（空=全工程内）

# 解析顺序：服务端本地配置（基线）→ 工程内 settings（只能收紧）
# 未受信任：忽略工程内的 allow/auto_approve 提权
```

### 3.9 与权限系统的关系

- 安全边界 = 权限管线的**第 1 级"安全硬闸"**（不可绕过，见《多智能体与权限系统详细设计》§3.2）。
- 权限模式/规则只能在安全边界**之内**生效；`path_ok` 等硬闸结果喂给 `check()` 最前置判断。

### 3.10 安全清单 & 待细化

**清单（实现时逐条核对）**：

- [ ] 无任何通用执行工具
- [ ] 所有 server 文件操作过 `path_ok` 且只读
- [ ] 写操作 100% 经前端预览确认 + UndoRedo
- [ ] key 不入库/不进导出/不下发前端/不进日志
- [ ] 服务仅绑 `127.0.0.1`
- [ ] 工程内配置只能收紧、不可提权
- [ ] Agent/Skill/OutputStyle 内容按不可信数据对待，不可提权
- [ ] `tools` / `allowed-tools` 只取当前可见 `ToolDef` 交集
- [ ] `paths` / 附带资源路径做 realpath/Windows 规范化

**待细化**：

| 项 | 说明 |
|----|------|
| 信任状态存储 | 受信任工程列表存哪、如何撤销 |
| 导出期防护 | 确保插件/配置不被打包进游戏导出 |
| 审计 | 危险决策（deny/越界）的日志留存 |
| Windows 路径/符号链接 | `realpath`/`commonpath` 在 Windows 的边界用例 |
| 项目级扩展目录 | Agent/Skill/OutputStyle 放置路径与 Godot 导出排除策略 |

---

## 四、三子系统与整体的关系

| 子系统 | 归属层 | 与其他的交汇 |
|------|------|------|
| 代码检索 | 支撑（server 工具） | 受**安全边界**限定工程根；供 programming-agent；结果喂多智能体上下文 |
| Skill | 支撑（Claude Code 同构 PromptCommand） | 受**信任模型**约束（不可信 skill 不提权）；按 agent 绑定；与 Command/SkillTool 同一注册表 |
| 安全边界 | 守卫层 | 是**权限系统**的第 1 级硬闸；约束检索范围与写操作收口 |
