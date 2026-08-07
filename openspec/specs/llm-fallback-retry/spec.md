# llm-fallback-retry Specification

## Purpose
TBD - created by archiving change effort-tier-switch-and-fallback-retry. Update Purpose after archive.
## Requirements
### Requirement: 降级重试在当轮内进行且至多 5 次

主模型请求失败时，系统 SHALL 在单次 `chat()` 调用内使用一个由 provider 拥有、可配置且至多 5 次的总 wire-attempt 预算，涵盖主模型、降级模型、建连和流创建。SDK 内建重试 MUST 被禁用，流式读取层 MUST NOT 叠加独立重试预算，且预算 SHALL NOT 跨轮或跨会话累计。默认策略前 2 次使用主模型，之后仅在已配置不同降级模型时使用降级模型。

#### Scenario: 主模型失败后当轮内多次重试

- **WHEN** 主模型在任何响应 chunk 被接受前发生可重试失败且配置了不同的 `llm_fallback_model`
- **THEN** provider 在同一总预算中先使用主模型、再按策略切换降级模型，实际 wire 请求总数不超过配置上限

#### Scenario: SDK 客户端创建

- **WHEN** OpenAI-compatible SDK client 被构造
- **THEN** SDK 自身重试被显式设为 0，应用 provider 是唯一重试所有者

#### Scenario: 作用域为当轮而非每会话

- **WHEN** 第 N 轮降级重试已耗尽、第 N+1 轮再次失败
- **THEN** 第 N+1 轮独立拥有完整预算，但第 N 轮的 wire attempts 不会被隐藏在 SDK 或 stream 子预算中

### Requirement: 按错误类型分类决定是否重试

系统 SHALL 将 `429`、`5xx`、连接超时及接受首个 chunk 前的连接错误视为可重试错误并纳入同一总预算、带有界退避；SHALL 将 `401`、`403`、`400` 等鉴权或参数错误视为不可重试。接受任意 text、reasoning、tool-call、usage chunk 后的传输中断 MUST 返回 `partial_stream_interrupted`，不得自动发起新的 completion。

#### Scenario: 429 可重试且带退避

- **WHEN** 端点在首个响应 chunk 前返回 `429`、`5xx` 或请求超时
- **THEN** 该错误进入同一总预算，重试之间带有界指数退避

#### Scenario: 401 直接失败不重试

- **WHEN** 端点返回 `401`、`403` 或参数类 `400`
- **THEN** 系统立即抛出类型化 `LLMError`，不发起另一次 wire 请求

#### Scenario: 部分流之后连接中断

- **WHEN** 任意模型输出 chunk 已被接受并发布为 provisional preview 后连接中断
- **THEN** 系统保留匹配的部分流身份、返回 `partial_stream_interrupted` 并等待新的恢复身份，而不是静默生成另一份 completion

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

### Requirement: Generic providers do not assume request idempotency
The generic OpenAI-compatible provider MUST NOT claim that replaying a completion is safe merely because a client-supplied header is stable. An idempotency header MAY be enabled only by an endpoint adapter whose declared contract guarantees its semantics.

#### Scenario: Generic compatible endpoint times out ambiguously
- **WHEN** the client cannot prove whether the endpoint began generation
- **THEN** retry policy treats duplicate provider work as possible and does not report header-based exactly-once execution

