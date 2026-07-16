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

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from app.llm.message_transformer import CacheBreakpoint, inject_cache_breakpoints

logger = logging.getLogger(__name__)

# 流式增量回调：`(kind, delta_text)`，`kind` 为 `"content"`（回复正文）
# 或 `"reasoning"`（思考过程），`delta_text` 为本次新增片段。
DeltaCallback = Callable[[str, str, int | None], None]

# 降级回调：`(primary_model, fallback_model)`，主模型请求失败、即将用
# `fallback_model` 重试时触发一次，供编排层把这次模型切换暴露为事件，
# 避免前端/日志里出现风格突变却查不到原因。
FallbackCallback = Callable[[str, str], None]

# 流式增量事件的最小推送间隔（秒），避免逐 token 产生事件淹没事件队列。
_DELTA_MIN_INTERVAL_S = 0.5


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
    reasoning_tokens: int | None = None
    cached_tokens: int | None = None
    cache_creation_tokens: int | None = None
    total_input_tokens: int | None = None
    model: str = ""


def _is_valid_token_count(value: Any) -> bool:
    """判断 usage 字段是否为可用的非负 token 计数（排除 bool）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _max_token_count(current: int | None, candidate: int | None) -> int | None:
    """取两个可空 token 计数中的较大者，用于跨 chunk 累积 usage（None 视为缺省）。"""
    if candidate is None:
        return current
    if current is None:
        return candidate
    return max(current, candidate)


def _usage_detail_value(details: Any, key: str) -> Any:
    """从 usage 的明细子对象中取字段，兼容对象属性与 dict 两种返回形态。"""
    if details is None:
        return None
    if isinstance(details, dict):
        return details.get(key)
    return getattr(details, key, None)


def _reasoning_tokens_from_usage(usage: Any) -> int | None:
    """从 OpenAI 兼容 usage 对象中提取真实 reasoning token 数。"""
    if usage is None:
        return None
    details = getattr(usage, "completion_tokens_details", None)
    value = _usage_detail_value(details, "reasoning_tokens")
    return value if _is_valid_token_count(value) else None


def _cache_tokens_from_usage(usage: Any) -> tuple[int | None, int | None, int | None]:
    """从 usage 中提取 `(命中缓存 token 数, 总输入 token 数, 新建缓存 token 数)`（§16.1）。

    命中数优先取 `prompt_tokens_details.cached_tokens`（OpenAI/百炼缓存命中的
    标准位置），回退到 usage 顶层的 `cached_tokens`；新建缓存数取
    `prompt_tokens_details.cache_creation_input_tokens`（百炼显式缓存命中
    `cache_control` 但前缀尚未被缓存时，本次按 125% 价格创建新缓存块的 token
    数）；总输入数取 `prompt_tokens`。任一字段缺失或非法时对应位置返回 None。
    """
    if usage is None:
        return None, None, None
    details = getattr(usage, "prompt_tokens_details", None)
    cached = _usage_detail_value(details, "cached_tokens")
    if not _is_valid_token_count(cached):
        cached = getattr(usage, "cached_tokens", None)
    cache_creation = _usage_detail_value(details, "cache_creation_input_tokens")
    total = getattr(usage, "prompt_tokens", None)
    return (
        cached if _is_valid_token_count(cached) else None,
        total if _is_valid_token_count(total) else None,
        cache_creation if _is_valid_token_count(cache_creation) else None,
    )


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
        thinking_budget: int = -1,
        on_delta: DeltaCallback | None = None,
        on_fallback: FallbackCallback | None = None,
        cache_breakpoints: list[CacheBreakpoint] | None = None,
    ) -> AssistantTurn:
        """发起一次对话补全请求。

        Args:
            messages: 当前 agent 帧的完整消息列表（OpenAI message dict）。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表，
                可为空列表（表示不提供工具）。
            model: 本次请求使用的模型名；为 None 时使用 provider 的默认模型。
            temperature: 采样温度；为 None 时使用端点默认值（编排层通常按
                `AgentDefinition.effort` 解析出具体值）。
            thinking_budget: 思考 token 预算。>0 时启用 extended thinking 并
                限制上限；==0 时关闭 thinking（确定性优先）；==-1（默认）时
                沿用 enable_thinking:true 不限预算的原有行为。
            on_delta: 流式增量回调，参数为
                `(kind, delta_text, token_count)`；精确 token 数仅在最终
                usage 可用时传入，否则为 None。
            on_fallback: 降级回调，主模型失败、即将用 `fallback_model`
                重试前触发一次；为 None 时不通知。
            cache_breakpoints: 待标记 `cache_control` 的消息下标（§16.1），
                不支持显式缓存的 provider 可忽略该参数。

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
            "Initialized OpenAI-compatible provider base_url=%s timeout_s=%s fallback_configured=%s",
            base_url,
            timeout_s,
            fallback_model is not None,
        )

    @property
    def supports_tool_calling(self) -> bool:
        """Chat Completions function calling 在 M0 视为默认可用。"""
        return True

    @property
    def supports_prompt_cache(self) -> bool:
        """启用上下文缓存（§16.1）：隐式缓存由端点自动命中稳定前缀，显式缓存由
        `_chat_once` 在前缀足够长时注入 `cache_control` 断点。"""
        return True

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str | None = None,
        temperature: float | None = None,
        thinking_budget: int = -1,
        on_delta: DeltaCallback | None = None,
        on_fallback: FallbackCallback | None = None,
        cache_breakpoints: list[CacheBreakpoint] | None = None,
    ) -> AssistantTurn:
        """调用 `chat.completions.create` 并转换为 `AssistantTurn`。

        主模型请求失败时，若配置了 `fallback_model` 且与本次请求的模型不同，
        会自动用 `fallback_model` 重试一次（§15 降级策略）。

        Args:
            messages: 当前 agent 帧的完整消息列表。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表。
            model: 本次请求使用的模型名；为 None 时使用 `default_model`。
            temperature: 采样温度；为 None 时使用端点默认值。
            thinking_budget: 思考 token 预算，参见 `LLMProvider.chat`。
            on_delta: 流式增量回调，参见 `LLMProvider.chat`。
            on_fallback: 降级回调，参见 `LLMProvider.chat`；在实际发起
                降级请求前调用一次，便于编排层把这次模型切换暴露为事件。
            cache_breakpoints: 待标记 `cache_control` 的消息下标（§16.1），
                由编排层的 `CacheDecisionEngine` 决定；为 None 或空列表时不
                注入任何缓存标记。

        Returns:
            模型本轮的回应。

        Raises:
            LLMError: 主模型与降级模型均连接失败、超时或返回错误状态码时抛出。
        """
        resolved_model = model or self._default_model
        try:
            return await self._chat_once(
                messages, tools, resolved_model, temperature, thinking_budget, on_delta, cache_breakpoints
            )
        except LLMError as exc:
            if self._fallback_model is None or self._fallback_model == resolved_model:
                logger.warning(
                    "LLM chat failed without fallback status_code=%s",
                    exc.status_code,
                )
                raise
            logger.warning(
                "LLM chat failed; retrying configured fallback status_code=%s",
                exc.status_code,
            )
            if on_fallback is not None:
                on_fallback(resolved_model, self._fallback_model)
            return await self._chat_once(
                messages, tools, self._fallback_model, temperature, thinking_budget, on_delta, cache_breakpoints
            )

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float | None,
        thinking_budget: int = -1,
        on_delta: DeltaCallback | None = None,
        cache_breakpoints: list[CacheBreakpoint] | None = None,
    ) -> AssistantTurn:
        """发起一次流式 `chat.completions.create` 请求并转换为 `AssistantTurn`。

        Args:
            messages: 当前 agent 帧的完整消息列表。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表。
            model: 本次请求实际使用的模型名（已解析，非 None）。
            temperature: 采样温度；为 None 时使用端点默认值。
            thinking_budget: 思考 token 预算，参见 `LLMProvider.chat`。
            on_delta: 流式增量回调，参见 `LLMProvider.chat`。
            cache_breakpoints: 待标记 `cache_control` 的消息下标，参见
                `LLMProvider.chat`。

        Returns:
            模型本轮的回应。

        Raises:
            LLMError: 连接失败、超时或端点返回错误状态码时抛出。
        """
        if thinking_budget > 0:
            extra_body: dict[str, Any] | None = {"enable_thinking": True, "thinking_budget": thinking_budget}
        elif thinking_budget == 0:
            extra_body = None
        else:
            extra_body = {"enable_thinking": True}

        # 显式上下文缓存：断点位置由编排层的 CacheDecisionEngine 决定，这里只
        # 负责把标记写进请求体；标记加在消息副本上，不污染调用方持有的
        # `frame.messages`（§16.1）。
        request_messages = messages
        if cache_breakpoints:
            request_messages = inject_cache_breakpoints(messages, cache_breakpoints)
            logger.info(
                "Explicit prompt cache markers injected count=%d segments=%s",
                len(cache_breakpoints),
                [bp.segment for bp in cache_breakpoints],
            )

        logger.info(
            "LLM chat request messages=%d tools=%d temperature=%s thinking_budget=%s",
            len(messages),
            len(tools),
            temperature,
            thinking_budget,
        )
        emitted_content_len = 0
        emitted_reasoning_len = 0
        max_stream_attempts = 5
        for stream_attempt in range(1, max_stream_attempts + 1):
            role = "assistant"
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            tool_calls_acc: dict[int, dict[str, Any]] = {}
            finish_reason: str | None = None
            last_content_emit = 0.0
            last_reasoning_emit = 0.0
            reasoning_tokens: int | None = None
            cached_tokens: int | None = None
            cache_creation_tokens: int | None = None
            total_input_tokens: int | None = None

            try:
                stream = await self._client.chat.completions.create(
                    model=model,
                    messages=request_messages,  # type: ignore[arg-type]
                    tools=tools or None,  # type: ignore[arg-type]
                    tool_choice="auto" if tools else None,
                    temperature=temperature,
                    extra_body=extra_body,
                    stream=True,
                    stream_options={"include_usage": True},
                )
                async for chunk in stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    chunk_reasoning_tokens = _reasoning_tokens_from_usage(chunk_usage)
                    if chunk_reasoning_tokens is not None:
                        reasoning_tokens = chunk_reasoning_tokens
                    # usage 在流式响应里通常只出现在末尾一个 chunk，但部分端点会在
                    # 多个 chunk 重复/分段报告；用 max 累积而非直接覆盖，避免后到的
                    # 0/缺省值把先前已读到的真实计数清掉。
                    chunk_cached, chunk_total, chunk_cache_creation = _cache_tokens_from_usage(chunk_usage)
                    cached_tokens = _max_token_count(cached_tokens, chunk_cached)
                    total_input_tokens = _max_token_count(total_input_tokens, chunk_total)
                    cache_creation_tokens = _max_token_count(cache_creation_tokens, chunk_cache_creation)
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
                    reasoning_piece = getattr(delta, "reasoning_content", None)
                    if reasoning_piece:
                        reasoning_parts.append(reasoning_piece)
                        now = time.monotonic()
                        if on_delta is not None and now - last_reasoning_emit >= _DELTA_MIN_INTERVAL_S:
                            reasoning_text = "".join(reasoning_parts)
                            delta_text = reasoning_text[emitted_reasoning_len:]
                            emitted_reasoning_len = max(emitted_reasoning_len, len(reasoning_text))
                            last_reasoning_emit = now
                            if delta_text:
                                on_delta("reasoning", delta_text, None)
                    if delta.content:
                        content_parts.append(delta.content)
                        now = time.monotonic()
                        if on_delta is not None and now - last_content_emit >= _DELTA_MIN_INTERVAL_S:
                            content_text = "".join(content_parts)
                            delta_text = content_text[emitted_content_len:]
                            emitted_content_len = max(emitted_content_len, len(content_text))
                            last_content_emit = now
                            if delta_text:
                                on_delta("content", delta_text, None)
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
                break
            except (APIConnectionError, APITimeoutError, httpx.TransportError) as exc:
                if stream_attempt < max_stream_attempts:
                    logger.warning(
                        "LLM stream connection error; reconnecting attempt=%d/%d error_type=%s",
                        stream_attempt + 1,
                        max_stream_attempts,
                        type(exc).__name__,
                    )
                    continue
                logger.warning("LLM stream connection error error_type=%s", type(exc).__name__)
                raise LLMError(f"大模型流式响应中断：{exc}") from exc
            except APIStatusError as exc:
                logger.warning("LLM stream status error status_code=%s", exc.status_code)
                raise LLMError(
                    f"大模型端点返回错误（{exc.status_code}）：{exc.message}",
                    status_code=exc.status_code,
                ) from exc

        content = "".join(content_parts) or None
        reasoning = "".join(reasoning_parts) or None
        if on_delta is not None:
            if reasoning is not None:
                delta_text = reasoning[emitted_reasoning_len:]
                if delta_text or reasoning_tokens is not None:
                    on_delta("reasoning", delta_text, reasoning_tokens)
            if content is not None:
                delta_text = content[emitted_content_len:]
                if delta_text:
                    on_delta("content", delta_text, None)

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
            "LLM chat response finish_reason=%s tool_calls=%d content_length=%d reasoning_length=%d",
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
            reasoning_tokens=reasoning_tokens,
            cached_tokens=cached_tokens,
            cache_creation_tokens=cache_creation_tokens,
            total_input_tokens=total_input_tokens,
            model=model,
        )
