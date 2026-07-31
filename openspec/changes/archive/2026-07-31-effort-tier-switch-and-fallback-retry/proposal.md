## Why

当前 effort 档位（quick/standard/deep/verify/advisor）体系存在四组与"会话级档位应整体生效、深度推理不该被预算截断、降级要稳"相悖的行为：①`_resolve_effort` 对所有委派子帧一律返回各 agent 自身声明的 effort，导致用户把 `session.effort` 设成 deep/quick 后，委派给 standard 类子 agent 时模型弹回 standard、不跟随会话档位；`programming-agent` 硬编码 deep、不受会话档位影响。②降级重试只在每次 `chat()` 内对降级模型重试 1 次，且 `_chat_once` 仅对连接/超时错误做流式重连、对 429/5xx 等状态码 0 次重试直接抛；verify/compact 路径降级时还不发事件。③thinking 预算对 deep/advisor/verify 截断（16384/2048/0），限制深度推理、分析与复核。④前端 effort 选择器把 verify/advisor 这两个固定内部档位暴露给用户手选，与"它们由特定 agent 在委派时固定使用"的设计冲突。

## What Changes

- **委派 effort 作用域（BREAKING）**：非固定档位的子 agent（含 `programming-agent` 与所有 standard 类）一律跟随 `session.effort`，不再用自身声明的 effort；仅 `advisor`（advisor）、`map-reviewer-agent`、`map-validator-agent`（verify）保留固定档位。`programming-agent` 的 `effort: deep` 声明不再驱动模型选择。
- **降级重试**：单次 `chat()`（当轮）内最多 5 次尝试，含主模型与降级模型的组合；429/5xx/超时纳入重试预算并带指数退避，401/403/400 等鉴权/参数错误直接失败；仅首次降级发 `agent_model_fallback` 事件，并给 verify/compact 路径补上 `on_fallback`（现状静默）。
- **thinking 预算**：`deep/advisor/verify` 改为 -1（不限预算，enable_thinking:true 无 budget）；`quick`→4096、`standard`→16384（×4）。改内置 `EFFORT_THINKING_BUDGET`，不依赖 config 覆盖。
- **前端选择器**：effort 下拉框隐藏 `verify/advisor`，仅留 `quick/standard/deep`；两档仍由对应 agent 按固定档位使用。

## Capabilities

### New Capabilities

- `effort-tier-selection`: effort 档位如何解析到根帧与委派子帧（哪些 agent 跟随 `session.effort`、哪些保留固定档位），以及前端向用户暴露哪些档位可选。
- `thinking-budget`: 各 effort 档位的 thinking token 预算取值与"无限（-1）"语义。
- `llm-fallback-retry`: 单轮 LLM 请求失败后的降级/重试策略——重试次数、错误分类（可重试 vs 直接失败）、退避与事件发射。

### Modified Capabilities

<!-- 无。均为新引入的 effort/LLM 行为契约，现有 specs 不涉及。 -->

## Impact

- 后端：`app/orchestrator/agent.py`（`_resolve_effort`、`EFFORT_THINKING_BUDGET`、`resolve_thinking_budget`、`_fallback_callback`）、`app/llm/provider.py`（`chat`、`_chat_once`）、`app/query/engine.py`（`_model_for_effort`）、`app/verify/runner.py`、`app/query/compactor.py`。
- 前端：`ai_agent_frontend/addons/ai_agent/ui/chat_panel.gd`（effort OptionButton 构造）。
- 配置：`app/config.py` 的 `llm_thinking_budget_*` 字段保留（运维覆盖用），默认值改在代码内置表。
- 无外部 API/依赖变更；纯行为变更。`programming-agent` 由硬编码 deep 改为跟随会话属行为破坏性变更（会话设 quick 时编码推理降档）。
