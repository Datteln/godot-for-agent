"""Generic model invocation stage shared by domain policies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.llm.message_transformer import CacheBreakpoint
from app.llm.provider import (
    AssistantTurn,
    DeltaCallback,
    FallbackCallback,
    LLMProvider,
    ResponseContract,
)


@dataclass(frozen=True, slots=True)
class ModelInvocation:
    """Fully resolved provider request; contains no Session or Map domain state."""

    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    model: str | None
    temperature: float | None
    thinking_budget: int
    on_delta: DeltaCallback | None = None
    on_fallback: FallbackCallback | None = None
    cache_breakpoints: list[CacheBreakpoint] | None = None
    response_contract: ResponseContract | None = None


async def invoke_model(
    provider: LLMProvider,
    request: ModelInvocation,
) -> AssistantTurn:
    """Execute one canonical provider invocation through the shared model port."""
    return await provider.chat(
        request.messages,
        request.tools,
        model=request.model,
        temperature=request.temperature,
        thinking_budget=request.thinking_budget,
        on_delta=request.on_delta,
        on_fallback=request.on_fallback,
        cache_breakpoints=request.cache_breakpoints,
        response_contract=request.response_contract,
    )
