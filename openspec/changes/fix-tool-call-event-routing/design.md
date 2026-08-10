## Context

服务端对一次模型响应会发布两类工具事件：`agent_tool_calls`（子 agent 的服务端工具调用，纯展示，payload 仅 `{frame_id, agent, tools}`）与 `tool_calls`（需前端执行的 front 工具，payload 带 `calls`，turn_service.py 231-241 行）。前端 `_handle_event` 对两者无差别路由进 `_handle_tool_calls`；后者以 `response.get("calls", [])` 取参，展示事件必然得到空数组，落入 else 分支 `_set_state(WAITING_LLM) + submit_tool_results([])`；`send_tool_results` 抑制空请求并 emit 本地伪 error（"No valid tool results were available; the pending batch was preserved."，恰 70 字符，即日志 text_len:70 条目）。

## Goals / Non-Goals

**Goals:**
- 展示事件与执行事件路由分离；
- 空 calls 早退，杜绝空转循环；
- 空批次静默 no-op，伪 error 仅用于真实非法批次。

**Non-Goals:**
- 不改动 `Recovering tool_calls from event stream` 恢复机制（该机制健康）；
- 不改变服务端事件 schema 或发布时机；
- 不处理 needs_confirm 审批流（行为保持不变）。

## Decisions

1. **路由分离**。`_handle_event` 中仅 `tool_calls` 进入 `_handle_tool_calls`；`agent_tool_calls` 只走 `_timeline_controller.present_event`（既有投影已生成 SearchTools 等展示块）。替代方案"在 _handle_tool_calls 内按 payload 形状判别"被否：路由决策应在事件分发层，且展示事件不应隐含执行语义。
2. **空 calls 早退**。`_handle_tool_calls` 顶部 `if calls.is_empty(): return`，先于状态切换与 state_store 写入。理由：空批次既无执行也无回传，任何状态变更都是噪音。
3. **伪 error 收敛**。`send_tool_results` 空批次分支只记 debug 日志、不 emit response；非空非法批次保留既有 typed error 与 disposition。理由：disposition/next_action 是给恢复逻辑的机器信号，空批次不是错误。

## Risks / Trade-offs

- [服务端未来依赖前端对空 tool_calls 的回传] → 当前服务端语义不期待空回传（编排自行继续）；spec 以 ADDED 要求固化前端 no-op 语义。
- [早退跳过 state_store pending_calls 清理] → 空 calls 时 confirm 必为空，pending_calls 写空数组与现状等价；实现时保持写入或明确跳过，二选一并在测试中固定。
