## ADDED Requirements

### Requirement: 非固定档位委派子帧跟随会话档位

系统在解析委派子帧的 effort 时，对于不在固定档位集合内的 agent，SHALL 使用 `session.effort` 而非该 agent 自身声明的 effort，使会话级档位对所有非固定档 agent 整体生效。

#### Scenario: standard 类子 agent 跟随会话档位

- **WHEN** `session.effort` 为 `deep` 且 coordinator 委派给声明 `effort: standard` 的 `resource-agent`
- **THEN** 该子帧解析出的 effort 为 `deep`（不弹回 `standard`），对应模型为 `llm_deep_model`

#### Scenario: programming-agent 跟随会话档位

- **WHEN** `session.effort` 为 `quick` 且 coordinator 委派给 `programming-agent`（声明 `effort: deep`）
- **THEN** 该子帧解析出的 effort 为 `quick`，不再因声明值强制为 `deep`

### Requirement: 固定档位 agent 保留自身 effort 不被会话覆盖

系统对于固定档位集合 `{advisor, map-reviewer-agent, map-validator-agent}` 内的 agent，SHALL 始终使用其自身声明的 effort（`advisor` / `verify`），不受 `session.effort` 覆盖。

#### Scenario: advisor 与 map-reviewer 不被会话档位覆盖

- **WHEN** `session.effort` 为 `quick` 且 `advisor` 帧或 `map-reviewer-agent` 帧运行
- **THEN** `advisor` 帧的 effort 仍为 `advisor`，`map-reviewer-agent` 帧的 effort 仍为 `verify`

### Requirement: 前端 effort 选择器仅暴露 quick/standard/deep

前端 effort 选择器 SHALL 仅提供 `quick` / `standard` / `deep` 三个选项；`verify` 与 `advisor` SHALL NOT 出现在用户可选列表中。

#### Scenario: 下拉框不含 verify/advisor

- **WHEN** 前端 effort OptionButton 被构造
- **THEN** 其选项恰好为 `quick`、`standard`、`deep`，不包含 `verify` 与 `advisor`

#### Scenario: 历史 verify/advisor 配置降级回 standard

- **WHEN** 持久化的 `ai_agent/effort` 配置值为 `verify` 或 `advisor` 且前端启动同步选择项
- **THEN** 选择项降级为 `standard`，不选中不存在的项
