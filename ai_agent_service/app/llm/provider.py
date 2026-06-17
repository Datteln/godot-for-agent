"""LLM Provider 抽象（§15）：默认 OpenAI 兼容 Chat Completions 实现。

编排层只依赖 `LLMProvider` 协议，不直接耦合具体 SDK；后续可加
Responses / Anthropic / Gemini provider 而不改编排层。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError

logger = logging.getLogger(__name__)

# 流式增量回调：`(kind, accumulated_text)`，`kind` 为 `"content"`（回复正文）
# 或 `"reasoning"`（思考过程），`accumulated_text` 为截至当前的完整累积文本。
DeltaCallback = Callable[[str, str], None]

# 降级回调：`(primary_model, fallback_model)`，主模型请求失败、即将用
# `fallback_model` 重试时触发一次，供编排层把这次模型切换暴露为事件，
# 避免前端/日志里出现风格突变却查不到原因。
FallbackCallback = Callable[[str, str], None]

# 流式增量事件的最小推送间隔（秒），避免逐 token 产生事件淹没事件队列。
_DELTA_MIN_INTERVAL_S = 0.12


@dataclass(frozen=True)
class ToolCallRequest:
    """模型在一条 assistant 消息中请求的单个工具调用。

    Attributes:
        id: 工具调用 id（`tool_call_id`），回传结果时需原样带回。
        name: 被调用的工具名。
        arguments: 工具入参的原始 JSON 字符串，由调用方 `json.loads`。
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class AssistantTurn:
    """一次 `LLMProvider.chat()` 调用的结果。

    Attributes:
        raw_message: 可直接 `append` 进 `frame.messages` 的 assistant
            消息字典（已 `exclude_none`）。
        content: assistant 消息的文本内容；纯工具调用时可能为 None。
        tool_calls: 本轮请求的工具调用列表，可能为空。
        finish_reason: 模型返回的结束原因（如 `stop`/`tool_calls`/`length`）。
        reasoning: 模型的思考过程文本（`enable_thinking` 开启且端点支持时），
            不写入 `raw_message`，仅供前端展示。
        model: 本次实际应答的模型名（主模型失败降级后为 `fallback_model`），
            供编排层/事件区分"这轮回复来自哪个模型"。
    """

    raw_message: dict[str, Any]
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None
    reasoning: str | None = None
    model: str = ""


class LLMError(Exception):
    """LLM 调用失败的统一异常，供上层转换为 `{"type":"error"}` 响应（§17）。"""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        """记录失败信息与可选的 HTTP 状态码。

        Args:
            message: 可直接展示给用户的错误说明（不包含密钥）。
            status_code: 端点返回的 HTTP 状态码（若有）。
        """
        super().__init__(message)
        self.status_code = status_code


class LLMProvider(Protocol):
    """大模型访问的统一协议。"""

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        on_delta: DeltaCallback | None = None,
        on_fallback: FallbackCallback | None = None,
    ) -> AssistantTurn:
        """发起一次对话补全请求。

        Args:
            messages: 当前 agent 帧的完整消息列表（OpenAI message dict）。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表，
                可为空列表（表示不提供工具）。
            model: 本次请求使用的模型名；为 None 时使用 provider 的默认模型。
            temperature: 采样温度；为 None 时使用端点默认值（编排层通常按
                `AgentDefinition.effort` 解析出具体值）。
            on_delta: 流式增量回调，每次正文/思考过程有新增内容时被调用，
                参数为 `(kind, accumulated_text)`；为 None 时不推送增量。
            on_fallback: 降级回调，主模型失败、即将用 `fallback_model`
                重试前触发一次；为 None 时不通知。

        Returns:
            模型本轮的回应。

        Raises:
            LLMError: 端点不可达、鉴权失败、限流或返回错误状态码时抛出。
        """
        ...

    @property
    def supports_tool_calling(self) -> bool:
        """该 provider/端点是否支持 function calling。"""
        ...

    @property
    def supports_prompt_cache(self) -> bool:
        """该 provider/端点是否可利用 prompt caching（§16.1）。"""
        ...


class OpenAICompatibleProvider:
    """默认 `LLMProvider` 实现：OpenAI 兼容 Chat Completions（§15）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        default_model: str,
        timeout_s: float,
        fallback_model: str | None = None,
    ) -> None:
        """构造 OpenAI 兼容客户端。

        Args:
            base_url: OpenAI 兼容端点的 base_url（支持本地模型/BYO key 端点）。
            api_key: API key；为空字符串时使用占位值，适配无需鉴权的本地端点。
            default_model: `model` 参数缺省时使用的模型名。
            timeout_s: 单次请求超时时间（秒）。
            fallback_model: 主模型请求失败时尝试的降级模型名；为 None 或与
                `default_model`/请求指定模型相同时不降级。
        """
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key or "sk-local-placeholder",
            timeout=timeout_s,
        )
        self._default_model = default_model
        self._fallback_model = fallback_model
        logger.info(
            "Initialized OpenAI-compatible provider base_url=%s default_model=%s fallback_model=%s timeout_s=%s",
            base_url,
            default_model,
            fallback_model,
            timeout_s,
        )

    @property
    def supports_tool_calling(self) -> bool:
        """Chat Completions function calling 在 M0 视为默认可用。"""
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        """M0 不做缓存前缀治理，统一报告不支持（§16.1 留待 M1+）。"""
        return False

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        on_delta: DeltaCallback | None = None,
        on_fallback: FallbackCallback | None = None,
    ) -> AssistantTurn:
        """调用 `chat.completions.create` 并转换为 `AssistantTurn`。

        主模型请求失败时，若配置了 `fallback_model` 且与本次请求的模型不同，
        会自动用 `fallback_model` 重试一次（§15 降级策略）。

        Args:
            messages: 当前 agent 帧的完整消息列表。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表。
            model: 本次请求使用的模型名；为 None 时使用 `default_model`。
            temperature: 采样温度；为 None 时使用端点默认值。
            on_delta: 流式增量回调，参见 `LLMProvider.chat`。
            on_fallback: 降级回调，参见 `LLMProvider.chat`；在实际发起
                降级请求前调用一次，便于编排层把这次模型切换暴露为事件。

        Returns:
            模型本轮的回应。

        Raises:
            LLMError: 主模型与降级模型均连接失败、超时或返回错误状态码时抛出。
        """
        resolved_model = model or self._default_model
        try:
            return await self._chat_once(messages, tools, resolved_model, temperature, on_delta)
        except LLMError as exc:
            if self._fallback_model is None or self._fallback_model == resolved_model:
                logger.warning(
                    "LLM chat failed without fallback model=%s status_code=%s",
                    resolved_model,
                    exc.status_code,
                )
                raise
            logger.warning(
                "LLM chat failed; retrying fallback primary_model=%s fallback_model=%s status_code=%s",
                resolved_model,
                self._fallback_model,
                exc.status_code,
            )
            if on_fallback is not None:
                on_fallback(resolved_model, self._fallback_model)
            return await self._chat_once(messages, tools, self._fallback_model, temperature, on_delta)

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float | None,
        on_delta: DeltaCallback | None = None,
    ) -> AssistantTurn:
        """发起一次流式 `chat.completions.create` 请求并转换为 `AssistantTurn`。

        Args:
            messages: 当前 agent 帧的完整消息列表。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表。
            model: 本次请求实际使用的模型名（已解析，非 None）。
            temperature: 采样温度；为 None 时使用端点默认值。
            on_delta: 流式增量回调，参见 `LLMProvider.chat`。

        Returns:
            模型本轮的回应。

        Raises:
            LLMError: 连接失败、超时或端点返回错误状态码时抛出。
        """
        logger.info(
            "LLM chat request model=%s messages=%d tools=%d temperature=%s",
            model,
            len(messages),
            len(tools),
            temperature,
        )
        try:
            stream = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools or None,  # type: ignore[arg-type]
                tool_choice="auto" if tools else None,
                temperature=temperature,
                extra_body={"enable_thinking": True},
                stream=True,
            )
        except (APIConnectionError, APITimeoutError) as exc:
            logger.warning("LLM connection error model=%s error_type=%s", model, type(exc).__name__)
            raise LLMError(f"无法连接大模型端点：{exc}") from exc
        except APIStatusError as exc:
            logger.warning("LLM status error model=%s status_code=%s", model, exc.status_code)
            raise LLMError(
                f"大模型端点返回错误（{exc.status_code}）：{exc.message}",
                status_code=exc.status_code,
            ) from exc

        role = "assistant"
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls_acc: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        last_content_emit = 0.0
        last_reasoning_emit = 0.0

        try:
            async for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                if delta.role:
                    role = delta.role
                if delta.content:
                    content_parts.append(delta.content)
                    now = time.monotonic()
                    if on_delta is not None and now - last_content_emit >= _DELTA_MIN_INTERVAL_S:
                        last_content_emit = now
                        on_delta("content", "".join(content_parts))
                reasoning_piece = getattr(delta, "reasoning_content", None)
                if reasoning_piece:
                    reasoning_parts.append(reasoning_piece)
                    now = time.monotonic()
                    if on_delta is not None and now - last_reasoning_emit >= _DELTA_MIN_INTERVAL_S:
                        last_reasoning_emit = now
                        on_delta("reasoning", "".join(reasoning_parts))
                for tool_call_delta in delta.tool_calls or []:
                    entry = tool_calls_acc.setdefault(
                        tool_call_delta.index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if tool_call_delta.id:
                        entry["id"] = tool_call_delta.id
                    if tool_call_delta.type:
                        entry["type"] = tool_call_delta.type
                    if tool_call_delta.function:
                        if tool_call_delta.function.name:
                            entry["function"]["name"] += tool_call_delta.function.name
                        if tool_call_delta.function.arguments:
                            entry["function"]["arguments"] += tool_call_delta.function.arguments
        except (APIConnectionError, APITimeoutError) as exc:
            logger.warning("LLM stream connection error model=%s error_type=%s", model, type(exc).__name__)
            raise LLMError(f"大模型流式响应中断：{exc}") from exc
        except APIStatusError as exc:
            logger.warning("LLM stream status error model=%s status_code=%s", model, exc.status_code)
            raise LLMError(
                f"大模型端点返回错误（{exc.status_code}）：{exc.message}",
                status_code=exc.status_code,
            ) from exc

        content = "".join(content_parts) or None
        reasoning = "".join(reasoning_parts) or None
        if on_delta is not None:
            if content is not None:
                on_delta("content", content)
            if reasoning is not None:
                on_delta("reasoning", reasoning)

        tool_calls = [
            ToolCallRequest(
                id=entry["id"],
                name=entry["function"]["name"],
                arguments=entry["function"]["arguments"],
            )
            for _, entry in sorted(tool_calls_acc.items())
        ]

        raw_message: dict[str, Any] = {"role": role}
        if content is not None:
            raw_message["content"] = content
        if tool_calls_acc:
            raw_message["tool_calls"] = [
                {
                    "id": entry["id"],
                    "type": entry["type"],
                    "function": {
                        "name": entry["function"]["name"],
                        "arguments": entry["function"]["arguments"],
                    },
                }
                for _, entry in sorted(tool_calls_acc.items())
            ]

        logger.info(
            "LLM chat response model=%s finish_reason=%s tool_calls=%d content_length=%d reasoning_length=%d",
            model,
            finish_reason,
            len(tool_calls),
            len(content or ""),
            len(reasoning or ""),
        )
        return AssistantTurn(
            raw_message=raw_message,
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            reasoning=reasoning,
            model=model,
        )
