"""将结构化历史块转换为前端可回放的伪事件。"""

from typing import Any

from app.api.schemas import SessionHistoryBlock


def blocks_to_pseudo_events(blocks: list[SessionHistoryBlock]) -> list[dict[str, Any]]:
    """将历史块转换为与 SSE 相同的事件包结构。"""
    events: list[dict[str, Any]] = []
    for history_block in blocks:
        block = history_block.model_dump(mode="json")
        payload: dict[str, Any]
        match block["type"]:
            case "user":
                payload = {"text": block["text"]}
                events.append({"type": "user_submitted", "payload": payload})
            case "error":
                events.append({"type": "error", "payload": {"text": block["text"]}})
            case "system_text":
                events.append({"type": "system_message", "payload": {"text": block["text"]}})
            case "log_text":
                events.append(
                    {
                        "type": "_history_log_text",
                        "payload": {
                            "text": block["text"],
                            "marker": block["marker"],
                            "indent": block["indent"],
                        },
                    }
                )
            case "log_read":
                events.append(
                    {
                        "type": "server_tool_result",
                        "payload": {
                            "tool": "read_file",
                            "result_summary": {
                                "kind": "read",
                                "path": block["path"],
                                "line_start": block["line_start"],
                                "line_end": block["line_end"],
                            },
                        },
                    }
                )
            case "log_grep":
                events.append(
                    {
                        "type": "server_tool_result",
                        "payload": {
                            "tool": "grep_code",
                            "result_summary": {
                                "kind": "grep",
                                "pattern": block["pattern"],
                                "include": block["include"],
                                "match_count": block["match_count"],
                                "matches": block["results"],
                                "truncated": block["truncated"],
                            },
                        },
                    }
                )
            case "log_edit":
                events.append(
                    {
                        "type": "server_tool_result",
                        "payload": {
                            "tool": "apply_text_edit",
                            "result_summary": {
                                "kind": "edit",
                                "path": block["path"],
                                "added": block["added"],
                                "removed": block["removed"],
                            },
                        },
                    }
                )
                if block["after_text"]:
                    events.append(
                        {
                            "type": "_history_code",
                            "payload": {"text": block["after_text"], "path": block["path"]},
                        }
                    )
            case "node_tree":
                events.append(
                    {
                        "type": "server_tool_result",
                        "payload": {
                            "tool": "read_scene_tree",
                            "result_summary": {"kind": "tree", "tree": block["tree"]},
                        },
                    }
                )
            case "thought":
                events.append(
                    {
                        "type": "_history_thought",
                        "payload": {"header": block["header"], "detail": block["detail"]},
                    }
                )
            case "plan_created":
                events.append(
                    {
                        "type": "plan_created",
                        "payload": {"summary": block["summary"], "steps": block["steps"]},
                    }
                )
            case "step_started":
                events.append(
                    {
                        "type": "plan_step_started",
                        "payload": {
                            "step_index": block["index"],
                            "total_steps": block["total"],
                            "title": block["title"],
                            "agent": block.get("agent", ""),
                        },
                    }
                )
            case "step_completed":
                events.append(
                    {
                        "type": "plan_step_completed",
                        "payload": {
                            "step_index": block["index"],
                            "total_steps": block["total"],
                            "summary": block["summary"],
                        },
                    }
                )
            case "verify_started":
                events.append(
                    {
                        "type": "verify_started",
                        "payload": {"file_path": block["file_path"], "phase": block["phase"]},
                    }
                )
            case "verify_passed" | "verify_failed":
                events.append(
                    {
                        "type": "verify_completed",
                        "payload": {
                            "file_path": block["file_path"],
                            "passed": block["type"] == "verify_passed",
                            "issues_count": block.get("issues_count", 0),
                            "summary": block["summary"],
                        },
                    }
                )
            case "delegate_results":
                for result in block["results"]:
                    events.append({"type": "delegate_result", "payload": result})
            case "delegate_result":
                events.append(
                    {
                        "type": "delegate_result",
                        "payload": {"agent": block.get("agent", ""), "summary": block["summary"]},
                    }
                )
            case "event":
                events.append({"type": block["event_type"], "payload": block["payload"]})
    return events
