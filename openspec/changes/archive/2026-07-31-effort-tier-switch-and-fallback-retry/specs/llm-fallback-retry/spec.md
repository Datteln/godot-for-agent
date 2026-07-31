## ADDED Requirements

### Requirement: 降级重试在当轮内进行且至多 5 次

主模型请求失败时，系统 SHALL 在单次 `chat()` 调用（即 `run_turn` 当轮）内最多进行 5 次尝试（含主模型与降级模型的组合），而非每会话仅一次或仅降级一次。重试预算 SHALL NOT 跨轮、跨会话累计。

#### Scenario: 主模型失败后当轮内多次重试

- **WHEN** 主模型请求失败且配置了与主模型不同的 `llm_fallback_model`
- **THEN** 系统在该轮 `chat()` 内最多进行 5 次尝试（主模型与降级模型组合），而非仅降级 1 次

#### Scenario: 作用域为当轮而非每会话

- **WHEN** 第 N 轮降级重试已耗尽、第 N+1 轮再次失败
- **THEN** 第 N+1 轮独立拥有完整的重试预算，不因第 N 轮已重试而丧失

### Requirement: 按错误类型分类决定是否重试

系统 SHALL 将 `429` / `5xx` / 超时 / 连接错误视为可重试错误并纳入当轮预算、带指数退避；SHALL 将 `401` / `403` / `400` 等鉴权与参数类错误视为不可重试，直接抛出 `LLMError`，不消耗重试预算。

#### Scenario: 429 可重试且带退避

- **WHEN** 端点返回 `429` 或 `5xx` 或请求超时
- **THEN** 该错误进入当轮 5 次预算，重试之间带指数退避

#### Scenario: 401 直接失败不重试

- **WHEN** 端点返回 `401` / `403` / `400`
- **THEN** 系统立即抛出 `LLMError`，不进行重试

### Requirement: 降级事件仅首次发射并覆盖静默路径

系统 SHALL 在首次由主模型切换到降级模型时发射一次 `agent_model_fallback` 事件；同一轮内的后续重试 SHALL NOT 重复发射。自动校验（`verify/runner.py`）与自动压缩摘要（`query/compactor.py`）路径发生降级时 SHALL 同样发射该事件，不再静默。

#### Scenario: 首次降级发事件、后续重试不发

- **WHEN** 主模型失败后首次转用降级模型
- **THEN** 发射一次 `agent_model_fallback` 事件，包含 `primary_model` 与 `fallback_model`
- **WHEN** 同一轮内随后继续重试
- **THEN** 不重复发射降级事件

#### Scenario: verify 与 compact 路径降级不再静默

- **WHEN** 自动校验或自动压缩摘要路径的 `llm.chat()` 发生降级
- **THEN** 同样发射 `agent_model_fallback` 事件（现状为静默）
