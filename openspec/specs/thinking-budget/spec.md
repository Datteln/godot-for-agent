# thinking-budget Specification

## Purpose
TBD - created by archiving change effort-tier-switch-and-fallback-retry. Update Purpose after archive.
## Requirements
### Requirement: deep/advisor/verify 档 thinking 预算不限

系统对 `deep`、`advisor`、`verify` 三个 effort 档位 SHALL 将 thinking 预算设为 `-1`（不限预算），即 `enable_thinking: true` 且不附带 `thinking_budget` 上限，使深度推理、分析与复核不被预算截断。

#### Scenario: 三档调用不带上限

- **WHEN** 一个 effort 为 `deep` / `advisor` / `verify` 的帧调用 `llm.chat()`
- **THEN** 传入的 `thinking_budget` 为 `-1`，provider 以 `enable_thinking: true`、无 budget 字段发起请求

#### Scenario: verify 由关 thinking 改为开 thinking

- **WHEN** 一个 `verify` 档帧运行（含自动校验、地图复核、地图结构校验任一）
- **THEN** thinking 处于开启状态且不限预算（不再是 `0` 关闭）

### Requirement: quick/standard 档 thinking 预算上调

系统对 `quick` 档 SHALL 将 thinking 预算设为 `4096`，对 `standard` 档 SHALL 设为 `16384`，给快/常规档更多思考空间，同时仍与不限预算的三档拉开差距。

#### Scenario: quick 与 standard 的预算取值

- **WHEN** 一个 effort 为 `quick` 的帧调用 `llm.chat()`
- **THEN** `thinking_budget` 为 `4096`
- **WHEN** 一个 effort 为 `standard` 的帧调用 `llm.chat()`
- **THEN** `thinking_budget` 为 `16384`

### Requirement: -1 语义为不限预算且可被配置覆盖

`thinking_budget = -1` SHALL 表示启用思考但不设上限。config 中的 `llm_thinking_budget_*` 字段 SHALL 仍优先于内置默认值生效（运维可覆盖任一档位）。

#### Scenario: config 覆盖优先于内置默认

- **WHEN** 环境变量 `AI_AGENT_LLM_THINKING_BUDGET_DEEP` 被设为某正值
- **THEN** `deep` 档使用该正值而非内置 `-1`

