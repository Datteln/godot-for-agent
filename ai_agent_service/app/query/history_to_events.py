"""将结构化历史块投影为 Canonical Chat Timeline 事件记录。"""

from typing import Any

from app.api.schemas import SessionHistoryBlock


def blocks_to_timeline_events(
    blocks: list[SessionHistoryBlock],
    *,
    session_epoch: str,
    start_index: int,
) -> list[dict[str, Any]]:
    """将一页历史块转换为可由纯 Timeline Projector 消费的事件。"""
    events: list[dict[str, Any]] = []
    for offset, history_block in enumerate(blocks):
        global_index = start_index + offset
        item = _timeline_item(history_block, session_epoch, global_index)
        events.append(
            {
                "schema_version": 1,
                "event_id": f"history:{global_index}",
                "session_epoch": session_epoch,
                "order_key": list(item["order_key"]),
                "item_id": item["item_id"],
                "type": "timeline_item",
                "payload": {"item": item},
            }
        )
    return events


def _timeline_item(
    history_block: SessionHistoryBlock,
    session_epoch: str,
    global_index: int,
) -> dict[str, Any]:
    """把单个历史块转换为唯一可序列化 Timeline item。"""
    block = history_block.model_dump(mode="json")
    block_type = str(block["type"])
    frame_id = str(block.get("frame_id") or "session")
    message_index = block.get("message_index")
    source = {
        "frame_id": "" if frame_id == "session" else frame_id,
        "message_id": (
            f"{frame_id}:{message_index}" if message_index is not None else ""
        ),
        "message_index": message_index if message_index is not None else -1,
        "turn_id": "",
        "tool_use_id": str(block.get("tool_use_id") or ""),
        "artifact_id": "",
        "preview_id": "",
    }
    item_id, order_key, kind, role, content_blocks, copy_text, style_token = (
        _item_fields(block, frame_id, message_index, global_index)
    )
    descriptor = block.get("render_descriptor")
    descriptor_result = (
        descriptor.get("result")
        if isinstance(descriptor, dict) and isinstance(descriptor.get("result"), dict)
        else {}
    )
    status = str(descriptor_result.get("status", "complete"))
    return {
        "schema_version": 1,
        "item_id": item_id,
        "session_epoch": session_epoch,
        "order_key": order_key,
        "kind": kind,
        "role": role,
        "content_blocks": content_blocks,
        "lifecycle": "committed",
        "status": status,
        "copy_text": copy_text,
        "style_token": style_token,
        "source": source,
        "estimated_height": _estimated_height(copy_text),
    }


def _item_fields(
    block: dict[str, Any],
    frame_id: str,
    message_index: int | None,
    global_index: int,
) -> tuple[str, list[int | str], str, str, list[dict[str, Any]], str, str]:
    """根据历史块类型生成稳定身份、顺序与内容块。"""
    block_type = str(block["type"])
    text = str(block.get("text", ""))
    descriptor = block.get("render_descriptor")
    if isinstance(descriptor, dict) and descriptor.get("type") == "tool_result":
        call = descriptor.get("call") if isinstance(descriptor.get("call"), dict) else {}
        result = (
            descriptor.get("result")
            if isinstance(descriptor.get("result"), dict)
            else {}
        )
        tool_use_id = str(block.get("tool_use_id") or "")
        identity = tool_use_id or f"{frame_id}:{message_index}:{global_index}"
        return (
            f"tool:{identity}",
            [frame_id, f"tool:{identity}", 2],
            "tool_result",
            "tool",
            [{"type": "tool", "call": call, "result": result}],
            text,
            "tool_result",
        )
    if block_type == "user":
        return (
            _message_item_id("user", frame_id, message_index, global_index),
            _message_order_key(frame_id, message_index, 1, global_index),
            "message",
            "user",
            [{"type": "markdown", "text": text}],
            text,
            "user",
        )
    if block_type == "thought":
        detail = str(block.get("detail", ""))
        return (
            _message_item_id("reasoning", frame_id, message_index, global_index),
            _message_order_key(frame_id, message_index, 0, global_index),
            "reasoning",
            "assistant",
            [
                {
                    "type": "reasoning",
                    "header": str(block.get("header", "Thought")),
                    "text": detail,
                    "token_count": 0,
                }
            ],
            detail,
            "reasoning",
        )
    if block_type == "log_text" and message_index is not None:
        return (
            _message_item_id("assistant", frame_id, message_index, global_index),
            _message_order_key(frame_id, message_index, 1, global_index),
            "message",
            "assistant",
            [{"type": "markdown", "text": text}],
            text,
            "indented" if bool(block.get("indent", False)) else "assistant",
        )
    if block_type in {"error", "system_text"}:
        role = "error" if block_type == "error" else "system"
        return (
            f"history:{global_index}",
            [global_index, 0, 0],
            role,
            role,
            [{"type": "markdown", "text": text}],
            text,
            role,
        )

    event_type, payload = _event_payload(block)
    return (
        f"history:{global_index}",
        [global_index, 0, 0],
        "tool_result" if event_type in {"server_tool_result", "front_tool_result"} else "system",
        "tool" if event_type in {"server_tool_result", "front_tool_result"} else "system",
        [{"type": "event", "event_type": event_type, "payload": payload}],
        text,
        "tool_result" if event_type in {"server_tool_result", "front_tool_result"} else "system",
    )


def _event_payload(block: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """把结构化块保真转换为公共展示事件描述。"""
    block_type = str(block["type"])
    if block_type == "log_read":
        return "server_tool_result", {
            "tool": "read_file",
            "result_summary": {
                "kind": "read",
                "path": block["path"],
                "line_start": block["line_start"],
                "line_end": block["line_end"],
            },
        }
    if block_type == "log_grep":
        return "server_tool_result", {
            "tool": "grep_code",
            "result_summary": {
                "kind": "grep",
                "pattern": block["pattern"],
                "include": block["include"],
                "match_count": block["match_count"],
                "matches": block["results"],
                "truncated": block["truncated"],
            },
        }
    if block_type == "log_edit":
        return "server_tool_result", {
            "tool": "apply_text_edit",
            "result_summary": {
                "kind": "edit",
                "path": block["path"],
                "added": block["added"],
                "removed": block["removed"],
                "after_text": block["after_text"],
            },
        }
    if block_type == "node_tree":
        return "server_tool_result", {
            "tool": "read_scene_tree",
            "result_summary": {"kind": "tree", "tree": block["tree"]},
        }
    if block_type == "plan_created":
        return "plan_created", {"summary": block["summary"], "steps": block["steps"]}
    if block_type == "step_started":
        return "plan_step_started", {
            "step_index": block["index"],
            "total_steps": block["total"],
            "title": block["title"],
        }
    if block_type == "step_completed":
        return "plan_step_completed", {
            "step_index": block["index"],
            "total_steps": block["total"],
            "summary": block["summary"],
        }
    if block_type == "verify_started":
        return "verify_started", {
            "file_path": block["file_path"],
            "phase": block["phase"],
        }
    if block_type == "verify_outcome":
        return "verify_completed", {
            "file_path": block["file_path"],
            "outcome": block["outcome"],
        }
    if block_type == "delegate_results":
        return "delegate_results", {"results": block["results"]}
    if block_type == "delegate_result":
        return "delegate_result", {
            "agent": block.get("agent", ""),
            "summary": block["summary"],
        }
    if block_type == "event":
        return str(block["event_type"]), dict(block["payload"])
    return "system_message", {"text": str(block.get("text", ""))}


def _message_item_id(
    channel: str,
    frame_id: str,
    message_index: int | None,
    global_index: int,
) -> str:
    """为消息型历史项生成与实时投影一致的稳定身份。"""
    if message_index is None:
        return f"history:{global_index}"
    return f"{channel}:{frame_id}:{message_index}"


def _message_order_key(
    frame_id: str,
    message_index: int | None,
    channel_order: int,
    global_index: int,
) -> list[int | str]:
    """为消息型历史项生成与实时投影一致的确定性顺序键。"""
    if message_index is None:
        return [global_index, channel_order, 0]
    return [frame_id, message_index, channel_order]


def _estimated_height(text: str) -> float:
    """提供不依赖 UI 节点的保守首帧高度估计。"""
    line_count = max(1, text.count("\n") + 1)
    wrap_lines = max(1, (len(text) + 89) // 90)
    return 30.0 + float(max(line_count, wrap_lines)) * 22.0
