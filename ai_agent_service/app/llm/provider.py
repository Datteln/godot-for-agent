"""LLM Provider 抽象（§15）：默认 OpenAI 兼容 Chat Completions 实现。

编排层只依赖 `LLMProvider` 协议，不直接耦合具体 SDK；后续可加
Responses / Anthropic / Gemini provider 而不改编排层。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI
from openai import APIConnectionError, APIStatusError, APITimeoutError


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
    """

    raw_message: dict[str, Any]
    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str | None = None


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
    ) -> AssistantTurn:
        """发起一次对话补全请求。

        Args:
            messages: 当前 agent 帧的完整消息列表（OpenAI message dict）。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表，
                可为空列表（表示不提供工具）。
            model: 本次请求使用的模型名；为 None 时使用 provider 的默认模型。
            temperature: 采样温度；为 None 时使用端点默认值（编排层通常按
                `AgentDefinition.effort` 解析出具体值）。

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
    ) -> AssistantTurn:
        """调用 `chat.completions.create` 并转换为 `AssistantTurn`。

        主模型请求失败时，若配置了 `fallback_model` 且与本次请求的模型不同，
        会自动用 `fallback_model` 重试一次（§15 降级策略）。

        Args:
            messages: 当前 agent 帧的完整消息列表。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表。
            model: 本次请求使用的模型名；为 None 时使用 `default_model`。
            temperature: 采样温度；为 None 时使用端点默认值。

        Returns:
            模型本轮的回应。

        Raises:
            LLMError: 主模型与降级模型均连接失败、超时或返回错误状态码时抛出。
        """
        resolved_model = model or self._default_model
        try:
            return await self._chat_once(messages, tools, resolved_model, temperature)
        except LLMError:
            if self._fallback_model is None or self._fallback_model == resolved_model:
                raise
            return await self._chat_once(messages, tools, self._fallback_model, temperature)

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        model: str,
        temperature: float | None,
    ) -> AssistantTurn:
        """发起一次 `chat.completions.create` 请求并转换为 `AssistantTurn`。

        Args:
            messages: 当前 agent 帧的完整消息列表。
            tools: 当前 agent 可见工具的 OpenAI function schema 列表。
            model: 本次请求实际使用的模型名（已解析，非 None）。
            temperature: 采样温度；为 None 时使用端点默认值。

        Returns:
            模型本轮的回应。

        Raises:
            LLMError: 连接失败、超时或端点返回错误状态码时抛出。
        """
        try:
            response = await self._client.chat.completions.create(
                model=model,
                messages=messages,  # type: ignore[arg-type]
                tools=tools or None,  # type: ignore[arg-type]
                tool_choice="auto" if tools else None,
                temperature=temperature,
            )
        except (APIConnectionError, APITimeoutError) as exc:
            raise LLMError(f"无法连接大模型端点：{exc}") from exc
        except APIStatusError as exc:
            raise LLMError(
                f"大模型端点返回错误（{exc.status_code}）：{exc.message}",
                status_code=exc.status_code,
            ) from exc

        choice = response.choices[0]
        message = choice.message
        tool_calls = [
            ToolCallRequest(id=call.id, name=call.function.name, arguments=call.function.arguments)
            for call in (message.tool_calls or [])
        ]
        return AssistantTurn(
            raw_message=message.model_dump(exclude_none=True),
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason,
        )
