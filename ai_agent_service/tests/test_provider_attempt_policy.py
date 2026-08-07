from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx

from app.llm.provider import (
    AssistantTurn,
    LLMError,
    OpenAICompatibleProvider,
    PartialStreamInterrupted,
)


def _provider(*, fallback: str | None = "fallback", attempts: int = 3) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://localhost:9999/v1",
        api_key="test",
        default_model="primary",
        timeout_s=1.0,
        fallback_model=fallback,
        max_attempts=attempts,
    )


def _turn(model: str) -> AssistantTurn:
    return AssistantTurn(raw_message={"role": "assistant", "content": "ok"}, content="ok", model=model)


def _delta_chunk(kind: str) -> Any:
    usage = SimpleNamespace(prompt_tokens=1) if kind == "usage" else None
    if kind == "usage":
        return SimpleNamespace(usage=usage, choices=[])
    tool_calls = []
    content = None
    reasoning = None
    if kind == "content":
        content = "partial"
    elif kind == "reasoning":
        reasoning = "partial thought"
    elif kind == "tool_call":
        tool_calls = [
            SimpleNamespace(
                index=0,
                id="call-1",
                type="function",
                function=SimpleNamespace(name="read_file", arguments="{"),
            )
        ]
    delta = SimpleNamespace(
        role="assistant",
        content=content,
        reasoning_content=reasoning,
        tool_calls=tool_calls,
    )
    choice = SimpleNamespace(finish_reason=None, delta=delta)
    return SimpleNamespace(usage=usage, choices=[choice])


async def _interrupted_stream(kind: str) -> Any:
    yield _delta_chunk(kind)
    request = httpx.Request("POST", "http://localhost:9999/v1/chat/completions")
    raise httpx.ReadError("stream interrupted", request=request)


class ProviderAttemptPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_sdk_retries_are_disabled(self) -> None:
        provider = _provider()
        self.assertEqual(provider._client.max_retries, 0)  # type: ignore[attr-defined]

    async def test_primary_and_fallback_share_exact_wire_budget(self) -> None:
        provider = _provider(attempts=3)
        models: list[str] = []

        async def fail_then_succeed(*args: Any, **_kwargs: Any) -> AssistantTurn:
            model = str(args[2])
            models.append(model)
            if len(models) < 3:
                raise LLMError("temporary", status_code=503)
            return _turn(model)

        provider._chat_once = fail_then_succeed  # type: ignore[method-assign]
        fallbacks: list[tuple[str, str]] = []
        with patch("app.llm.provider.asyncio.sleep", new=AsyncMock()):
            result = await provider.chat(
                [],
                [],
                on_fallback=lambda primary, fallback: fallbacks.append((primary, fallback)),
            )

        self.assertEqual(models, ["primary", "primary", "fallback"])
        self.assertEqual(result.wire_attempt_count, 3)
        self.assertEqual(fallbacks, [("primary", "fallback")])

    async def test_prestream_connection_failure_can_retry(self) -> None:
        provider = _provider(fallback=None, attempts=2)
        provider._chat_once = AsyncMock(  # type: ignore[method-assign]
            # `_chat_once` normally translates SDK exceptions. Use a translated
            # failure to exercise the provider-owned budget rather than SDK logic.
            side_effect=[
            LLMError("connect", status_code=None),
            _turn("primary"),
            ]
        )
        with patch("app.llm.provider.asyncio.sleep", new=AsyncMock()):
            result = await provider.chat([], [])
        self.assertEqual(result.wire_attempt_count, 2)

    async def test_terminal_status_is_not_retried(self) -> None:
        provider = _provider(attempts=3)
        call = AsyncMock(side_effect=LLMError("bad request", status_code=400))
        provider._chat_once = call  # type: ignore[method-assign]

        with self.assertRaises(LLMError):
            await provider.chat([], [])

        self.assertEqual(call.await_count, 1)

    async def test_no_retry_after_each_partial_chunk_kind(self) -> None:
        for kind in ("content", "reasoning", "tool_call", "usage"):
            with self.subTest(kind=kind):
                provider = _provider(attempts=3)
                create = AsyncMock(return_value=_interrupted_stream(kind))
                provider._client = SimpleNamespace(  # type: ignore[assignment]
                    chat=SimpleNamespace(completions=SimpleNamespace(create=create))
                )

                with self.assertRaises(PartialStreamInterrupted) as captured:
                    await provider.chat([], [])

                self.assertIn(kind, captured.exception.accepted_kinds)
                self.assertEqual(captured.exception.error_code, "partial_stream_interrupted")
                self.assertEqual(captured.exception.wire_attempt_count, 1)
                self.assertEqual(create.await_count, 1)


if __name__ == "__main__":
    unittest.main()
