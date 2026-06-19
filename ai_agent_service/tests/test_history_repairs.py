from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from app.agents.bundled import get_agent
from app.agents.types import Frame
from app.api.schemas import ChatRequest, SessionHistoryResponse
from app.events.store import Event
from app.llm.provider import _reasoning_tokens_from_usage
from app.orchestrator.agent import (
    _delta_callback,
    _estimate_stream_token_count,
    _resolve_request_model,
)
from app.query.engine import (
    _event_payload_for_log,
    _normalize_model_override,
    _structured_history_for_frame,
    _tool_history_blocks,
)


def _frame() -> Frame:
    return Frame(id="f1", agent=get_agent("coordinator", set()), messages=[])


def test_chat_request_accepts_optional_model_override() -> None:
    default_request = ChatRequest(session_id="s1", user_message="hello")
    override_request = ChatRequest(
        session_id="s1",
        user_message="hello",
        model="  custom-model  ",
    )

    assert default_request.model is None
    assert override_request.model == "  custom-model  "


def test_session_history_exposes_event_cursor() -> None:
    response = SessionHistoryResponse(session_id="s1", last_event_seq=42)

    assert response.last_event_seq == 42


def test_request_model_selector_is_temporary_and_trims_input() -> None:
    assert _normalize_model_override("   ") is None
    assert _normalize_model_override("  custom-model  ") == "custom-model"


def test_request_model_override_has_highest_priority() -> None:
    agent = replace(get_agent("coordinator", set()), model="agent-model")

    selected = _resolve_request_model(
        agent,
        "deep",
        lambda _effort: "effort-model",
        "request-model",
    )

    assert selected == "request-model"


def test_model_names_are_redacted_from_event_logs() -> None:
    logged = _event_payload_for_log(
        {
            "model": "request-model",
            "primary_model": "primary-model",
            "fallback_model": "fallback-model",
            "loop": 1,
        }
    )

    assert logged == {
        "model": "<redacted>",
        "primary_model": "<redacted>",
        "fallback_model": "<redacted>",
        "loop": 1,
    }


def test_reasoning_token_count_comes_from_usage() -> None:
    usage = SimpleNamespace(
        completion_tokens_details=SimpleNamespace(reasoning_tokens=321)
    )

    assert _reasoning_tokens_from_usage(usage) == 321
    assert _reasoning_tokens_from_usage(None) is None


def test_stream_token_estimate_handles_english_and_cjk() -> None:
    assert _estimate_stream_token_count("") == 0
    assert _estimate_stream_token_count("abcdefgh") == 2
    assert _estimate_stream_token_count("中文") == 2


def test_reasoning_event_estimates_then_accepts_exact_token_count() -> None:
    events: list[tuple[str, dict[str, object]]] = []
    callback = _delta_callback(
        lambda event_type, payload: events.append((event_type, payload)),
        "f1",
        1,
        2,
        "f1",
        2,
    )
    assert callback is not None

    callback("reasoning", "中文 reasoning text", None)
    assert events[-1][1]["token_count"] == _estimate_stream_token_count("中文 reasoning text")

    callback("reasoning", "中文 reasoning text", 42)
    assert events[-1][1]["token_count"] == 42


def test_runtime_state_history_preserves_node_tree() -> None:
    result = {
        "ok": True,
        "edited_scene": {
            "name": "Root",
            "path": ".",
            "type": "Node2D",
            "children": [
                {"name": "Player", "path": "Player", "type": "CharacterBody2D"}
            ],
        },
    }

    blocks = _tool_history_blocks(
        _frame(),
        "read_runtime_state",
        {"max_depth": 4},
        json.dumps(result),
    )

    assert len(blocks) == 1
    text = blocks[0].model_dump()["text"]
    assert "edited_scene" in text
    assert "Player" in text
    assert "CharacterBody2D" in text


def test_edit_history_preserves_complete_code_and_whitespace() -> None:
    content = "\n" + ("print('x')\n" * 300) + "\n"

    blocks = _tool_history_blocks(
        _frame(),
        "write_file",
        {"path": "scripts/long.gd", "content": content},
        json.dumps({"ok": True, "path": "scripts/long.gd"}),
    )

    assert blocks[0].model_dump()["after_text"] == content


def test_late_usage_updates_the_persisted_reasoning_header() -> None:
    frame = _frame()
    frame.messages = [
        {"role": "system", "content": "system"},
        {"role": "assistant", "content": "final answer"},
    ]
    events = [
        Event(
            seq=1,
            session_id="s1",
            type="agent_reasoning_delta",
            payload={
                "frame_id": "f1",
                "message_index": 1,
                "loop": 1,
                "text": "reasoning",
                "elapsed_ms": 1200,
            },
        ),
        Event(
            seq=2,
            session_id="s1",
            type="agent_text_delta",
            payload={
                "frame_id": "f1",
                "message_index": 1,
                "loop": 1,
                "text": "final answer",
            },
        ),
        Event(
            seq=3,
            session_id="s1",
            type="agent_reasoning_delta",
            payload={
                "frame_id": "f1",
                "message_index": 1,
                "loop": 1,
                "text": "reasoning",
                "elapsed_ms": 1300,
                "token_count": 42,
            },
        ),
    ]

    blocks = _structured_history_for_frame(frame, events)

    thought = next(block for block in blocks if block.type == "thought")
    assert thought.model_dump()["header"] == "Thought for 1.20s · 42 tokens"
