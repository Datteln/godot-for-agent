"""旧会话的一次性兼容转换：blocks → 权威展示稿。

规则（见 change design.md 决定 6）：

- 只转换已持久化 frame/事件推断出的 block 序列一次，结果持久化为展示稿并
  标记 `legacy`；之后所有加载直接读取保存结果，不再重复推断。
- 不能可靠恢复的信息不伪造：`thought` block 整体丢弃（新契约不展示
  Thought），无法归属的 `event` block 跳过。
- 转换产物只使用契约内的 kind；`system`/`log` 两个 legacy-only kind 仅
  在此处产生。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence

from app.sessions.store import Session

logger = logging.getLogger(__name__)

_LARGE_VALUE_CHARS = 24_000
_TREE_PREVIEW_CHARS = 8_000


def _truncate_text(value: str, limit: int = _LARGE_VALUE_CHARS) -> str:
    """截断超长文本，防止转换产物把大段内容复制进持久化展示稿。"""
    if len(value) <= limit:
        return value
    return value[:limit] + "…(截断)"


def _convert_user(session: Session, block: Any) -> None:
    _append(session, "user", "complete", {
        "text": str(getattr(block, "text", "")),
        "client_message_id": None,
        "has_context": False,
    })


def _convert_error(session: Session, block: Any) -> None:
    _append(session, "error", "complete", {"text": str(getattr(block, "text", ""))})


def _convert_system_text(session: Session, block: Any) -> None:
    _append(session, "system", "complete", {"text": str(getattr(block, "text", ""))})


def _convert_log_text(session: Session, block: Any) -> None:
    _append(session, "log", "complete", {
        "text": _truncate_text(str(getattr(block, "text", ""))),
        "marker": bool(getattr(block, "marker", False)),
        "indent": bool(getattr(block, "indent", False)),
    })


def _convert_log_read(session: Session, block: Any) -> None:
    path = str(getattr(block, "path", ""))
    _append(session, "tool_activity", "resolved", {
        "tool": "read_file",
        "args": {"path": path},
        "agent": None,
        "is_error": False,
        "result_summary": {
            "kind": "read",
            "path": path,
            "line_start": int(getattr(block, "line_start", 1)),
            "line_end": int(getattr(block, "line_end", 1)),
        },
        "result_count": 1,
        "render_kind": None,
    }, tool_call_id=None)


def _convert_log_grep(session: Session, block: Any) -> None:
    results: list[dict[str, Any]] = []
    for match in getattr(block, "results", []) or []:
        results.append({
            "path": str(getattr(match, "path", "")),
            "line": getattr(match, "line", None),
            "text": _truncate_text(str(getattr(match, "text", "")), 400),
        })
    _append(session, "tool_activity", "resolved", {
        "tool": "grep",
        "args": {
            "pattern": str(getattr(block, "pattern", "")),
            "include": str(getattr(block, "include", "project")),
        },
        "agent": None,
        "is_error": False,
        "result_summary": {
            "kind": "grep",
            "pattern": str(getattr(block, "pattern", "")),
            "match_count": int(getattr(block, "match_count", 0)),
            "results": results,
            "truncated": bool(getattr(block, "truncated", False)),
        },
        "result_count": int(getattr(block, "match_count", 0)),
        "render_kind": None,
    }, tool_call_id=None)


def _convert_log_edit(session: Session, block: Any) -> None:
    path = str(getattr(block, "path", ""))
    _append(session, "tool_activity", "resolved", {
        "tool": "apply_text_edit",
        "args": {"path": path},
        "agent": None,
        "is_error": False,
        "result_summary": {
            "kind": "edit",
            "path": path,
            "added": int(getattr(block, "added", 0)),
            "removed": int(getattr(block, "removed", 0)),
            "after_text": _truncate_text(str(getattr(block, "after_text", ""))),
        },
        "result_count": 1,
        "render_kind": "diff",
    }, tool_call_id=None)


def _convert_node_tree(session: Session, block: Any) -> None:
    tree = getattr(block, "tree", {}) or {}
    _append(session, "tool_activity", "resolved", {
        "tool": "read_scene_tree",
        "args": {},
        "agent": None,
        "is_error": False,
        "result_summary": {
            "kind": "node_tree",
            "title": str(getattr(block, "title", "Scene tree")),
            "tree": _bounded_tree(tree),
        },
        "result_count": 1,
        "render_kind": None,
    }, tool_call_id=None)


def _bounded_tree(tree: dict[str, Any]) -> dict[str, Any] | str:
    """限制场景树快照大小；过大时退化为截断提示，避免持久化膨胀。"""
    try:
        serialized = json.dumps(tree, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return "(场景树不可序列化)"
    if len(serialized) <= _TREE_PREVIEW_CHARS:
        return tree
    return f"(场景树过大，共 {len(serialized)} 字符，已省略)"


def _convert_plan_created(session: Session, block: Any, index: dict[str, Any]) -> None:
    steps = [
        {"title": str(getattr(step, "title", "")), "status": "pending"}
        for step in getattr(block, "steps", []) or []
    ]
    entry = _append(session, "plan", "complete", {
        "summary": str(getattr(block, "summary", "")),
        "steps": steps,
    })
    index["plan_titles"] = [str(getattr(step, "title", "")) for step in getattr(block, "steps", []) or []]
    index["step_entries"] = {}
    index["current_plan_entry"] = str(entry["entry_id"])


def _convert_step_started(session: Session, block: Any, index: dict[str, Any]) -> None:
    step_index = int(getattr(block, "index", 0))
    titles: list[str] = index.get("plan_titles", [])
    title = str(getattr(block, "title", "")) or (
        titles[step_index - 1] if 0 < step_index <= len(titles) else ""
    )
    entry = _append(session, "progress", "running", {
        "step_index": step_index,
        "total_steps": int(getattr(block, "total", 0)),
        "title": title,
        "summary": None,
    })
    index.setdefault("step_entries", {})[step_index] = entry


def _convert_step_completed(session: Session, block: Any, index: dict[str, Any]) -> None:
    step_index = int(getattr(block, "index", 0))
    summary = str(getattr(block, "summary", ""))
    step_entries: dict[int, dict[str, Any]] = index.get("step_entries", {})
    entry = step_entries.get(step_index)
    if entry is not None:
        entry["state"] = "complete"
        entry["revision"] = int(entry.get("revision", 1)) + 1
        payload = dict(entry.get("payload", {}))
        payload["summary"] = summary
        payload["total_steps"] = int(getattr(block, "total", 0)) or payload.get("total_steps", 0)
        entry["payload"] = payload
        return
    _append(session, "progress", "complete", {
        "step_index": step_index,
        "total_steps": int(getattr(block, "total", 0)),
        "title": "",
        "summary": summary,
    })


def _convert_verify_started(session: Session, block: Any, index: dict[str, Any]) -> None:
    file_path = str(getattr(block, "file_path", ""))
    entry = _append(session, "verification", "running", {
        "tool_use_id": None,
        "file_path": file_path,
        "phase": str(getattr(block, "phase", "")),
        "issues_count": None,
        "summary": None,
    }, tool_call_id=None)
    index.setdefault("verify_entries", {})[file_path] = entry


def _convert_verify_finished(
    session: Session, block: Any, index: dict[str, Any], *, passed: bool
) -> None:
    file_path = str(getattr(block, "file_path", ""))
    verify_entries: dict[str, dict[str, Any]] = index.get("verify_entries", {})
    entry = verify_entries.get(file_path)
    summary = str(getattr(block, "summary", ""))
    issues_count = int(getattr(block, "issues_count", 0)) if not passed else 0
    if entry is not None:
        entry["state"] = "passed" if passed else "failed"
        entry["revision"] = int(entry.get("revision", 1)) + 1
        payload = dict(entry.get("payload", {}))
        payload["issues_count"] = issues_count
        payload["summary"] = summary
        entry["payload"] = payload
        return
    _append(session, "verification", "passed" if passed else "failed", {
        "tool_use_id": None,
        "file_path": file_path,
        "phase": "",
        "issues_count": issues_count,
        "summary": summary,
    }, tool_call_id=None)


def _convert_delegate_results(session: Session, block: Any) -> None:
    for result in getattr(block, "results", []) or []:
        agent = str(getattr(result, "agent", ""))
        summary = _truncate_text(str(getattr(result, "summary", "")))
        text = f"[{agent}] {summary}" if agent else summary
        _append(session, "log", "complete", {"text": text, "marker": False, "indent": False})


def _convert_delegate_result(session: Session, block: Any) -> None:
    _append(session, "log", "complete", {
        "text": _truncate_text(str(getattr(block, "summary", ""))),
        "marker": False,
        "indent": False,
    })


def _convert_thought(session: Session, block: Any) -> None:
    """把携带真实内容的历史 Thought block 转为 `kind=thought` 完成态条目。

    仅 `detail` 非空（来自历史推理增量）的 Thought 有可恢复内容；耗时与
    token 计数尽量从旧展示头（"Thought for X.XXs · N tokens"）解析，解析不
    到时置 0，不伪造更精确的数值。
    """
    detail = _truncate_text(str(getattr(block, "detail", "")).strip())
    if detail == "":
        return
    header = str(getattr(block, "header", ""))
    duration = _parse_thought_duration(header)
    token_count = _parse_thought_tokens(header)
    _append(session, "thought", "complete", {
        "content": detail,
        "token_count": token_count,
        "started_at": None,
        "duration_seconds": duration,
    })


def _parse_thought_duration(header: str) -> float:
    """从 "Thought for 3.50s..." 头部尽力解析耗时；失败返回 0.0。"""
    marker = "Thought for "
    start = header.find(marker)
    if start == -1:
        return 0.0
    rest = header[start + len(marker):]
    end = rest.find("s")
    if end <= 0:
        return 0.0
    try:
        return max(float(rest[:end]), 0.0)
    except ValueError:
        return 0.0


def _parse_thought_tokens(header: str) -> int:
    """从 "... · 1,234 tokens" 头部尽力解析 token 数；失败返回 0。"""
    marker = "tokens"
    idx = header.rfind(marker)
    if idx == -1:
        return 0
    digits = ""
    for char in reversed(header[:idx]):
        if char.isdigit():
            digits = char + digits
        elif char in ",. " and digits:
            break
        elif char in ",. ":
            continue
        else:
            break
    try:
        return int(digits) if digits else 0
    except ValueError:
        return 0


def _convert_event(session: Session, block: Any) -> None:
    event_type = str(getattr(block, "event_type", ""))
    payload = getattr(block, "payload", {}) or {}
    if event_type == "error":
        _append(session, "error", "complete", {"text": str(payload.get("text", ""))})
    # 其余事件类型在新契约下不可见（缓存、压缩、模型回退等），不伪造条目。


def _append(
    session: Session,
    kind: str,
    state: str,
    payload: dict[str, Any],
    *,
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """按创建顺序追加一个 legacy 条目（ordinal = 当前长度）。"""
    session.transcript_entry_counter += 1
    entry = {
        "entry_id": f"e{session.transcript_entry_counter}",
        "ordinal": len(session.transcript_entries),
        "kind": kind,
        "state": state,
        "revision": 1,
        "turn_id": None,
        "tool_call_id": tool_call_id,
        "payload": payload,
    }
    session.transcript_entries.append(entry)
    return entry


def convert_legacy_blocks(session: Session, blocks: Sequence[Any]) -> None:
    """把旧 block 序列一次性写入会话展示稿并打上 legacy 标记。

    调用方必须保证该会话尚未转换（`transcript_meta` 无 `converted` 标记），
    并在调用后持久化会话；转换本身不发布任何实时事件。

    Args:
        session: 待转换的会话。
        blocks: 既有历史管线产出的 `SessionHistoryBlock` 序列（按展示顺序）。
    """
    index: dict[str, Any] = {}
    blocks_list = list(blocks)
    skip_next_thought_summary = False
    for block in blocks_list:
        block_type = str(getattr(block, "type", ""))
        if block_type == "thought":
            # 携带真实内容的 Thought（来自历史推理增量）转为 `kind=thought`
            # 完成态条目；空 detail 的 Thought 是 `Thought:` 前缀摘要的占位，
            # 新契约对正文前缀做剥离，连带跳过紧随的孪生 `log_text(marker)`。
            detail = str(getattr(block, "detail", "")).strip()
            if detail != "":
                skip_next_thought_summary = False
                _convert_thought(session, block)
                continue
            skip_next_thought_summary = True
            continue
        if (
            skip_next_thought_summary
            and block_type == "log_text"
            and bool(getattr(block, "marker", False))
        ):
            skip_next_thought_summary = False
            continue
        skip_next_thought_summary = False
        if block_type == "user":
            _convert_user(session, block)
        elif block_type == "error":
            _convert_error(session, block)
        elif block_type == "system_text":
            _convert_system_text(session, block)
        elif block_type == "log_text":
            _convert_log_text(session, block)
        elif block_type == "log_read":
            _convert_log_read(session, block)
        elif block_type == "log_grep":
            _convert_log_grep(session, block)
        elif block_type == "log_edit":
            _convert_log_edit(session, block)
        elif block_type == "node_tree":
            _convert_node_tree(session, block)
        elif block_type == "plan_created":
            _convert_plan_created(session, block, index)
        elif block_type == "step_started":
            _convert_step_started(session, block, index)
        elif block_type == "step_completed":
            _convert_step_completed(session, block, index)
        elif block_type == "verify_started":
            _convert_verify_started(session, block, index)
        elif block_type == "verify_passed":
            _convert_verify_finished(session, block, index, passed=True)
        elif block_type == "verify_failed":
            _convert_verify_finished(session, block, index, passed=False)
        elif block_type == "delegate_results":
            _convert_delegate_results(session, block)
        elif block_type == "delegate_result":
            _convert_delegate_result(session, block)
        elif block_type == "event":
            _convert_event(session, block)
        else:
            logger.debug("Legacy block skipped unknown type=%s", block_type)
    session.transcript_meta["converted"] = True
    session.transcript_meta["legacy"] = len(session.transcript_entries) > 0
    logger.info(
        "Legacy transcript converted session=%s blocks=%d entries=%d",
        session.session_id,
        len(blocks),
        len(session.transcript_entries),
    )
