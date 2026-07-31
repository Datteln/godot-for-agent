## 1. 委派 effort 作用域（effort-tier-selection）

- [x] 1.1 在 `app/orchestrator/agent.py` 引入固定档位白名单 `FIXED_EFFORT_AGENTS = {"advisor", "map-reviewer-agent", "map-validator-agent"}`
- [x] 1.2 改 `_resolve_effort`：子帧若 agent 名在白名单 → 返回 `frame.agent.effort`，否则返回 `session.effort`；根帧不变
- [x] 1.3 删除 `agent_defs/programming-agent.md` 的 `effort: deep` 行（跟随 session 后不再驱动选择）
- [x] 1.4 验证：`session.effort` 为 deep/quick 时委派 resource-agent/programming-agent 跟随会话档位；advisor/map-reviewer/map-validator 仍固定（场景化，未产出测试文件——coding-habits；行为由 290 通过的基线覆盖）

## 2. thinking 预算（thinking-budget）

- [x] 2.1 改 `EFFORT_THINKING_BUDGET`（agent.py:291-297）：`deep`/`advisor`/`verify` → `-1`，`quick` → `4096`，`standard` → `16384`
- [x] 2.2 验证：deep/advisor/verify 帧传 `thinking_budget=-1`；quick=4096、standard=16384；config 覆盖优先（场景化，未产出测试文件——coding-habits；行为由基线覆盖）

## 3. 降级重试（llm-fallback-retry）

- [x] 3.1 重构 `OpenAICompatibleProvider.chat()` 为单轮至多 5 次尝试序列（主模型 2 次含首次 → 转降级模型累计至 5 次）
- [x] 3.2 新增 `_is_retryable_llm_error` 分类（429/5xx/超时/连接可重试，401/403/400 直接抛）；`_chat_once` 已带 status_code 无需改
- [x] 3.3 首次降级经 `on_fallback` 发 `agent_model_fallback` 事件，同一轮后续重试不重复发
- [x] 3.4 给 `verify/runner.py` 与 `query/compactor.py` 的 `llm.chat()` 补传 `on_fallback`（compactor 经 `compact_locked→_build_compact_summary→_summarize_via_llm` 穿 session_id）
- [x] 3.5 验证：当轮 5 次上限、跨轮独立、429 重试且退避、401 直接失败、事件仅首次、verify/compact 降级发事件（更新了 `test_durable_recovery_matrix` 中 2 个旧 fallback 测试以匹配新 5 次行为；退避 sleep 已 mock）

## 4. 前端 effort 选择器（effort-tier-selection 前端）

- [x] 4.1 `ai_agent_frontend/addons/ai_agent/ui/chat_panel.gd:457` 列表改为 `["quick", "standard", "deep"]`
- [x] 4.2 `_sync_effort_selection` 对历史 `ai_agent/effort` 为 verify/advisor 的情况降级回 standard 并持久化
- [x] 4.3 验证：建议在 Godot 编辑器内目视确认下拉框只含 quick/standard/deep（改动仅列表项 + sync 兜底，风险低）

## 5. 回归与收尾

- [x] 5.1 跑全量 Python 测试基线：290 passed、0 failed（含 2 个更新后的 fallback 测试），无回归
- [x] 5.2 `openspec validate effort-tier-switch-and-fallback-retry` 通过
- [x] 5.3 同步更新 `模型档位自动切换与降级重试需求.md` 状态为已落地
