## Why

工具结果回传后的 `/chat` 请求会把模型正文与推理增量和事务性事件一起暂存到 Session 提交结束，导致几十秒的真实模型流被压缩成一次数百事件的突发返回。该行为既放大 `/chat` 超时风险，也会让 Godot 聊天面板在单帧批量布局期间误判滚动位置并关闭自动滚动。

## What Changes

- 将模型正文/推理增量定义为带请求身份的临时预览事件，使其在工具结果事务执行期间即可通过 `/chat/events` 增量可见。
- 继续暂存会影响恢复、工具状态、授权、artifact 与 Session 历史的事务性事件，保持现有原子提交和回滚语义。
- 在提交成功时发布明确的预览确认边界；在取消或回滚时发布丢弃边界，防止临时文本成为可恢复历史或与重试结果重复。
- 限制事件查询的单批大小，并允许客户端连续拉取积压事件，避免单帧处理数百条事件。
- 让聊天面板按帧消费事件并把程序性布局变化与用户主动上滚区分开，在用户仍跟随输出时持续滚到底部。
- 增加流式首包延迟、事件批量大小、预览提交/丢弃和自动滚动的回归测试与观测日志。

## Capabilities

### New Capabilities

- `chat-event-streaming`: 定义模型预览增量的低延迟交付、批量背压、提交/丢弃边界以及前端跟随滚动行为。

### Modified Capabilities

- `atomic-tool-result-submission`: 将“所有 Session 事件均缓冲至提交后”收窄为“仅事务性、可恢复事件缓冲”，允许不可恢复的模型预览增量在事务提交前对外可见。

## Impact

- 后端：`app/query/engine.py` 的 publication buffer 与事件分类、`app/events/store.py` 的事件读取上限、`app/api/routes.py` 和响应 schema 的事件游标协议。
- 前端：`agent_http_client.gd` 的事件拉取节奏与积压排空、`chat_panel.gd` 的分帧消费和自动滚动状态判断。
- API：`POST /chat` 的最终三态 JSON 保持兼容；`GET /chat/events` 增加有界批量/积压信息和预览生命周期事件。
- 数据：临时预览不得写入 Session 可恢复历史；现有已提交历史和 map artifact 格式不迁移。
