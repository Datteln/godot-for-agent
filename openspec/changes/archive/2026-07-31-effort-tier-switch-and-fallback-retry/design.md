## Context

当前 effort→model 解析链与降级/预算现状已核对（见 proposal 与需求文档）：

- `_resolve_effort`（agent.py:300-316）：根帧用 `session.effort`，子帧一律用 `frame.agent.effort`。
- `EFFORT_THINKING_BUDGET`（agent.py:291-297）：quick 1024 / standard 4096 / deep 16384 / verify 0 / advisor 2048；`resolve_thinking_budget` 允许 config 覆盖。
- 降级在 `OpenAICompatibleProvider.chat`（provider.py:282-302）：主模型 1 次 + 降级模型 1 次；`_chat_once` 对连接错误流式重连 5 次、对状态码 0 次重试直接抛。
- 前端 `chat_panel.gd:457` 以五档构造 OptionButton。
- provider 对 `thinking_budget < 0` 已支持 `enable_thinking:true` 无 budget 语义（provider.py:332-337），故 `-1` 可直接表达无限，无需新逻辑。

## Goals / Non-Goals

**Goals:**

- 会话级 `session.effort` 对所有非固定档 agent 整体生效，委派不再弹回 standard。
- 降级在当轮（单次 `chat()`）内可重试至 5 次，覆盖 429/5xx，带退避。
- deep/advisor/verify 不限思考预算；quick/standard 上调 ×4。
- 前端不暴露固定档位 verify/advisor。

**Non-Goals:**

- 不改 `EFFORT_TEMPERATURE`（温度映射不变）。
- 不引入新 effort 档位。
- 不改手动 effort 写入路径（`request.effort` / `set_effort` 仍是用户入口，仅选项收窄）。
- 不改 `app/config.py` 字段结构（`llm_thinking_budget_*` 保留作运维覆盖）。
- 不改 effort→model 映射（`_model_for_effort` 不变）。

## Decisions

### 1. 固定档位白名单驱动 `_resolve_effort`

引入 `FIXED_EFFORT_AGENTS = {"advisor", "map-reviewer-agent", "map-validator-agent"}`。`_resolve_effort` 子帧分支：agent 在白名单 → 返回 `frame.agent.effort`；否则返回 `session.effort`。

- 理由：白名单显式、可维护，优于"声明值 ≠ standard 才固定"的隐式推断。
- 备选：给 `AgentDefinition` 加 `effort_policy: fixed | follow_session` 字段。更通用但需改类型与全部 agent 定义；当前仅 3 个固定 agent，白名单更轻。若未来固定 agent 增多可再升级为字段。

### 2. programming-agent 跟随会话

白名单不含 `programming-agent` → 跟随 `session.effort`。其 `effort: deep` 声明跟随后不再驱动选择，**建议删除** `agent_defs/programming-agent.md` 的 `effort:` 行以避免误导（保留则需注释说明其已失效）。

### 3. 直接改内置 `EFFORT_THINKING_BUDGET`

`deep/advisor/verify` → `-1`；`quick` → 4096；`standard` → 16384。复用 provider 既有的"`<0` → enable_thinking:true 无 budget"语义，无新逻辑。不改 config 字段——`resolve_thinking_budget` 对非 `None` 的 `-1` 原样返回，故 config 覆盖路径仍有效。

- 备选：把 `llm_thinking_budget_*` config 默认改为 -1。但内置 16384 仍在、默认不无限，不如直接改内置表。

### 4. 降级重试重构为单轮至多 5 次

把 `chat()` 的"主模型 1 次 + 降级 1 次"重构为单轮至多 5 次的尝试序列。建议默认分配（proposal §4-B）：主模型重试 2 次（含首次）→ 仍失败转降级模型，累计至多 5 次。

- `_chat_once` 内的流式重连（连接错误 5 次）与本变更的"当轮 5 次"是两个层次：流式重连管单次请求的连接抖动，当轮 5 次管模型级失败，两者叠加。

### 5. 错误分类

`_chat_once` 现有 `APIStatusError` 分支区分可重试（429/5xx/超时）与不可重试（401/403/400）。可重试进入当轮预算并带指数退避；不可重试直接抛 `LLMError`，不消耗预算。

### 6. 事件

首次降级发 `agent_model_fallback`（沿用 `_fallback_callback`）；后续重试不重复发。verify（`runner.py:237`）/ compact（`compactor.py:269`）路径补传 `on_fallback`，现状静默。

### 7. 前端选择器收窄

`chat_panel.gd:457` 列表改为 `["quick", "standard", "deep"]`。`_sync_effort_selection` 对历史 config 值为 verify/advisor 的情况降级回 standard，避免选中不存在的项。

## Risks / Trade-offs

- **[programming-agent 降档]** 会话设 quick 时编码推理从 deep 降到 quick → 代码质量可能下降。→ 缓解：文档提示；用户可手动设 deep；接受此行为变更（已确认）。
- **[verify 开 thinking 不再完全确定]** 复核结果可能不再可复现。→ 缓解：温度仍 0；保留回退 0 的注释；如复核不稳定可回退。
- **[降级 5 次 + 退避增加延迟]** 单轮最坏情况延迟上升。→ 缓解：仅可重试错误触发；退避有上限；不可重试错误立即失败。
- **[固定档白名单与 agent 声明脱节]** 未来新增固定 agent 需同步白名单。→ 缓解：白名单集中定义、注释说明。

## Migration Plan

- 后端改动均改内置默认，无需迁移配置；`.env` 显式设的 `llm_thinking_budget_*` 仍优先生效。
- 前端历史 `ai_agent/effort` 配置若为 verify/advisor，启动时降级为 standard。
- 无数据迁移、无外部 API 变更。

## Open Questions

- 降级 5 次的精确分配（主模型几次 / 降级模型几次）与退避基数/上限——建议默认见 proposal §4-B/C，待实施确认。
- `programming-agent` 的 `effort: deep` 声明是否删除（跟随 session 后无意义）——建议删除避免误导，待确认。
