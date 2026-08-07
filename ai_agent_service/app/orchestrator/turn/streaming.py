"""Generic streaming callbacks shared by domain policies.

The delta callback translates provider streaming chunks into orchestration
events.  The fallback callback reports model fallback.  Both are fully generic
and contain no Map/session-specific state.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any


def estimate_stream_token_count(text: str) -> int:
    """Estimate tokens for an accumulated stream without model-specific deps."""
    if not text:
        return 0
    cjk_chars = 0
    other_bytes = 0
    for char in text:
        codepoint = ord(char)
        if (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
        ):
            cjk_chars += 1
        else:
            other_bytes += len(char.encode("utf-8"))
    return cjk_chars + max(0, other_bytes - 1) // 3 + 1


def make_delta_callback(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame_id: str,
    loop: int,
    message_index: int,
    timeline_frame_id: str,
    timeline_message_index: int,
) -> Callable[[str, str, int | None], None] | None:
    """Build the streaming-delta callback passed to ``LLMProvider.chat``.

    Translates provider ``_on_delta(kind, text, token_count)`` into
    ``agent_text_delta`` / ``agent_reasoning_delta`` orchestration events.
    Returns ``None`` when ``event_callback`` is ``None``.
    """
    if event_callback is None:
        return None

    reasoning_started_at = time.monotonic()
    accumulated_text: dict[str, str] = {"content": "", "reasoning": ""}
    chunk_index = 0

    def _on_delta(kind: str, text: str, token_count: int | None) -> None:
        nonlocal chunk_index
        chunk_index += 1
        event_type = (
            "agent_reasoning_delta" if kind == "reasoning" else "agent_text_delta"
        )
        accumulated_text[kind] = accumulated_text.get(kind, "") + text
        payload: dict[str, Any] = {
            "frame_id": frame_id,
            "loop": loop,
            "message_index": message_index,
            "timeline_frame_id": timeline_frame_id,
            "timeline_message_index": timeline_message_index,
            "stream_segment": 0,
            "text": text,
            "append_delta": True,
            "provider_chunk_index": chunk_index,
            "provider_first_chunk": chunk_index == 1,
        }
        if kind == "reasoning":
            payload["elapsed_ms"] = max(
                int((time.monotonic() - reasoning_started_at) * 1000), 1
            )
            payload["token_count"] = (
                token_count
                if token_count is not None
                else estimate_stream_token_count(accumulated_text[kind])
            )
        event_callback(event_type, payload)

    return _on_delta


def make_fallback_callback(
    event_callback: Callable[[str, dict[str, Any]], None] | None,
    frame_id: str,
    loop: int,
) -> Callable[[str, str], None] | None:
    """Build the model-fallback callback for ``LLMProvider.chat``.

    When the primary model fails and the provider retries with a fallback
    model, this emits an ``agent_model_fallback`` event.  Returns ``None``
    when ``event_callback`` is ``None``.
    """
    if event_callback is None:
        return None

    def _on_fallback(primary_model: str, fallback_model: str) -> None:
        event_callback(
            "agent_model_fallback",
            {
                "frame_id": frame_id,
                "loop": loop,
                "primary_model": primary_model,
                "fallback_model": fallback_model,
            },
        )

    return _on_fallback
