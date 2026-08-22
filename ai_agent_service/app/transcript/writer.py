"""TranscriptWriter：唯一允许创建/修改可见展示稿条目的服务端组件。

设计约束（见 change design.md 决定 1-3）：

- 在用户消息、首个正文 delta、工具状态、审批、计划、进度、校验、错误与
  最终完成态**发生时**写入记录，而不是读取时从 frame/事件推断。
- 一个助手响应（流式增量与最终完成）只更新同一个助手条目；Thought 与
  `agent_reasoning_delta` 不进入展示稿，`Thought:` 前缀行在产生时一次性剥离。
- 工具完成采用原地更新：`tool_activity` 从 `running` 迁移到
  `resolved`/`failed`，不追加独立结果条目；审批以 `approval` 条目原地记录决定。

Writer 只做两件事：变更 `Session.transcript_*` 内存态，并通过注入的
`emit` 回调发布 `transcript_patch` 事件（持久化仍由调用方的
`SessionStore.save` 统一负责）。
"""

from __future__ import annotations

import copy
import logging
import time
from typing import Any, Callable

from app.sessions.store import Session
from app.transcript.models import VALID_ENTRY_STATES

logger = logging.getLogger(__name__)

_THOUGHT_PREFIX = "Thought:"
_ARGS_VALUE_MAX_CHARS = 24_000

EmitCallable = Callable[[str, str, dict[str, Any]], int]

_APPROVAL_PATH_KEYS: tuple[str, ...] = (
    "path",
    "target_path",
    "file_path",
    "script_path",
    "resource_path",
    "scene_path",
    "material_path",
    "track_path",
    "directory",
)
"""审批入参中可能携带受影响路径的键名白名单（按展示优先级排序）。"""

_APPROVAL_OPERATION_VERBS: dict[str, str] = {
    "apply_text_edit": "修改",
    "propose_script_edit": "修改",
    "write_file": "写入",
    "propose_content_file": "创建",
    "propose_tests": "创建测试",
    "run_system_command": "执行命令",
    "execute_gd_script": "执行脚本",
    "run_tests": "运行测试",
    "run_headless_self_test": "运行自检",
    "set_project_setting": "修改项目设置",
    "batch_rename": "批量重命名",
    "open_scene": "打开场景",
    "add_autoload": "添加 Autoload",
    "remove_autoload": "移除 Autoload",
    "add_input_action": "添加输入动作",
    "remove_input_action": "移除输入动作",
}
"""审批工具名到可读操作动词的映射；未收录工具回退为工具名本身。"""


def approval_operation_summary(tool: str, args: dict[str, Any]) -> str:
    """由工具名与入参生成审批条目的可读操作摘要。

    摘要只依赖持久化的 typed 字段（工具名/入参），不从 UI 或原始传输猜测。
    命令与脚本类工具附带一段截断后的目标描述，便于单行权限结果文本表达。

    Args:
        tool: 待审批工具名。
        args: 已截断的工具入参字典。

    Returns:
        一段可读的操作描述，例如 `修改`、`执行命令 ls -la`。
    """
    verb = _APPROVAL_OPERATION_VERBS.get(tool, tool)
    if tool == "run_system_command":
        command = str(args.get("command", "")).strip()
        if command:
            return f"{verb} {command[:120]}"
    if tool == "execute_gd_script":
        snippet = str(args.get("script", args.get("code", ""))).strip()
        if snippet:
            first_line = snippet.split("\n", 1)[0][:80]
            return f"{verb} {first_line}"
    return verb


def approval_affected_paths(args: dict[str, Any]) -> list[str]:
    """从审批入参中提取受影响路径列表（去重、保持出现顺序）。

    只读取白名单键；`paths` 列表键会被展开。找不到任何路径时返回空列表，
    由渲染端显式标注“未提供”，而不是猜测。

    Args:
        args: 已截断的工具入参字典。

    Returns:
        受影响路径的字符串列表。
    """
    paths: list[str] = []

    def _add(value: Any) -> None:
        text = str(value).strip()
        if text and text not in paths:
            paths.append(text)

    for key in _APPROVAL_PATH_KEYS:
        if key in args:
            _add(args.get(key))
    raw_paths = args.get("paths")
    if isinstance(raw_paths, list):
        for item in raw_paths:
            _add(item)
    return paths


def visible_assistant_text(raw_text: str) -> str:
    """剥离助手正文首行的 `Thought:` 前缀，返回用户可见正文。

    该规则只在展示稿产生时执行一次并随条目持久化；渲染端不再做任何
    文本前缀推断。仅当第一个非空行完整以 `Thought:` 开头时才剥离，
    避免误伤正文中途出现的同样子串。

    Args:
        raw_text: 助手消息原始正文（可能以 `Thought: ...` 行开头）。

    Returns:
        去除 Thought 前缀行后的可见正文（去掉前导空白）。
    """
    lines = raw_text.split("\n")
    first_content_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip():
            first_content_index = index
            break
    if first_content_index is None:
        return raw_text.strip()
    first_line = lines[first_content_index]
    if not first_line.lstrip().startswith(_THOUGHT_PREFIX):
        return raw_text
    remainder = "\n".join(lines[first_content_index + 1 :])
    return remainder.strip()


def _bounded_args(args: dict[str, Any]) -> dict[str, Any]:
    """截断工具入参中的超长字符串，防止展示稿条目无限膨胀。

    Args:
        args: 工具原始入参字典。

    Returns:
        字符串值被截断到上限后的新字典；非字符串值原样保留。
    """
    bounded: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > _ARGS_VALUE_MAX_CHARS:
            bounded[key] = value[:_ARGS_VALUE_MAX_CHARS] + "…(截断)"
        else:
            bounded[key] = value
    return bounded


class TranscriptWriter:
    """在事实产生时写入权威展示稿，并发布对应 `transcript_patch`。"""

    def __init__(self, emit: EmitCallable) -> None:
        """初始化写入器。

        Args:
            emit: 事件发布回调，签名 `(session_id, event_type, payload) -> seq`，
                通常为 `QueryEngine._emit`。
        """
        self._emit = emit

    # ------------------------------------------------------------------
    # 用户消息
    # ------------------------------------------------------------------

    def record_user_message(
        self,
        session: Session,
        text: str,
        *,
        client_message_id: str | None,
        has_context: bool,
    ) -> str:
        """记录一条用户消息条目并确认其 `client_message_id` 身份。

        Args:
            session: 当前会话。
            text: 用户消息原文。
            client_message_id: 客户端提供的消息身份，供乐观条目对账；可空。
            has_context: 本次提交是否附带上下文。

        Returns:
            新条目的 `entry_id`。
        """
        entry = self._create_entry(
            session,
            kind="user",
            state="complete",
            payload={
                "text": text,
                "client_message_id": client_message_id,
                "has_context": has_context,
            },
        )
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    # ------------------------------------------------------------------
    # 助手正文（流式与完成共用同一条目）
    # ------------------------------------------------------------------

    def update_assistant_stream(
        self,
        session: Session,
        *,
        frame_id: str,
        message_index: int,
        cumulative_text: str,
        turn_id: str | None = None,
    ) -> str:
        """按累计正文更新助手流式条目；首个 delta 时创建条目。

        Args:
            session: 当前会话。
            frame_id: 增量所属 agent 帧 id。
            message_index: 该响应在 `frame.messages` 中的位置。
            cumulative_text: 到本次增量为止的累计原始正文。
            turn_id: 当前轮次 id，可空。

        Returns:
            该助手响应对应的 `entry_id`。
        """
        key = f"assistant:{frame_id}:{message_index}"
        entry_id = session.transcript_index.get(key)
        visible = visible_assistant_text(cumulative_text)
        if entry_id is None:
            entry = self._create_entry(
                session,
                kind="assistant",
                state="streaming",
                payload={"text": visible},
                turn_id=turn_id,
                index_key=key,
            )
        else:
            entry = self._update_entry(
                session,
                entry_id,
                state="streaming",
                payload={"text": visible},
                turn_id=turn_id,
            )
        session.transcript_index["assistant:active"] = str(entry["entry_id"])
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    def complete_assistant(
        self,
        session: Session,
        text: str,
        *,
        frame_id: str | None = None,
        message_index: int | None = None,
        turn_id: str | None = None,
    ) -> str:
        """把助手响应标记为完成；无对应流式条目时直接创建完成态条目。

        Args:
            session: 当前会话。
            text: 最终正文原文（产生时剥离 Thought 前缀）。
            frame_id: 最终响应所属帧 id；与 `message_index` 共同定位流式条目。
            message_index: 最终响应在帧消息列表中的位置。
            turn_id: 当前轮次 id，可空。

        Returns:
            完成条目的 `entry_id`。
        """
        entry_id: str | None = None
        if frame_id is not None and message_index is not None:
            entry_id = session.transcript_index.get(f"assistant:{frame_id}:{message_index}")
        if entry_id is None:
            entry_id = session.transcript_index.get("assistant:active")
        visible = visible_assistant_text(text)
        if entry_id is None:
            entry = self._create_entry(
                session,
                kind="assistant",
                state="complete",
                payload={"text": visible},
                turn_id=turn_id,
            )
        else:
            entry = self._update_entry(
                session,
                entry_id,
                state="complete",
                payload={"text": visible},
                turn_id=turn_id,
            )
        session.transcript_index["assistant:active"] = str(entry["entry_id"])
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    # ------------------------------------------------------------------
    # 用户可见 Thought（生产者显式标记可见的推理流）
    # ------------------------------------------------------------------

    def update_thought_stream(
        self,
        session: Session,
        *,
        frame_id: str,
        message_index: int,
        cumulative_text: str,
        token_count: int | None,
    ) -> str:
        """创建/更新一个 `kind=thought` 条目的思考中状态。

        首次调用创建条目并记录开始时间；后续调用以累计内容与最新
        `token_count` 原地更新（revision 递增），不新建条目。

        Args:
            session: 当前会话。
            frame_id: 推理所属 agent 帧 id。
            message_index: 该轮响应在 `frame.messages` 中的位置。
            cumulative_text: 到本次增量为止的累计思考内容。
            token_count: 当前思考 token 计数；None 时保留上次计数。

        Returns:
            该 Thought 条目的 `entry_id`。
        """
        key = f"thought:{frame_id}:{message_index}"
        entry_id = session.transcript_index.get(key)
        if entry_id is None:
            entry = self._create_entry(
                session,
                kind="thought",
                state="thinking",
                payload={
                    "content": cumulative_text,
                    "token_count": int(token_count) if token_count is not None else 0,
                    "started_at": time.time(),
                    "duration_seconds": None,
                },
                index_key=key,
            )
            session.transcript_index[f"thought:open:{frame_id}"] = str(entry["entry_id"])
        else:
            existing = self._find_entry(session, entry_id)
            payload = dict(existing.get("payload", {})) if existing is not None else {}
            if existing is not None and str(existing.get("state", "")) == "complete":
                # `complete` 是单向的（任务 2.7）：完成后的迟到增量不得把条目退回
                # `thinking`；仅在累计内容更长时保留更完整的推理内容。
                existing_content = str(payload.get("content", ""))
                if len(cumulative_text) <= len(existing_content):
                    return str(entry_id)
                payload["content"] = cumulative_text
                if token_count is not None and int(token_count) > int(
                    payload.get("token_count", 0)
                ):
                    payload["token_count"] = int(token_count)
                entry = self._update_entry(session, entry_id, state="complete", payload=payload)
                self._publish(session.session_id, entry)
                return str(entry["entry_id"])
            payload["content"] = cumulative_text
            if token_count is not None:
                payload["token_count"] = int(token_count)
            entry = self._update_entry(session, entry_id, state="thinking", payload=payload)
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    def complete_thought(
        self,
        session: Session,
        *,
        frame_id: str,
        message_index: int | None = None,
    ) -> str | None:
        """把进行中的 Thought 条目原地迁移到 `complete`，写入规范耗时。

        完成判定：`duration_seconds = now - started_at`（墙钟，保留两位小数）。
        已完成的条目重复调用是幂等的。

        Args:
            session: 当前会话。
            frame_id: Thought 所属帧 id。
            message_index: 响应位置；为 None 时结束该帧当前打开的 Thought。

        Returns:
            Thought 条目的 `entry_id`；不存在对应 Thought 时返回 None。
        """
        entry_id: str | None = None
        if message_index is not None:
            entry_id = session.transcript_index.get(f"thought:{frame_id}:{message_index}")
        if entry_id is None:
            entry_id = session.transcript_index.get(f"thought:open:{frame_id}")
        if entry_id is None:
            return None
        entry = self._find_entry(session, entry_id)
        if entry is None:
            session.transcript_index.pop(f"thought:open:{frame_id}", None)
            return None
        if str(entry.get("state", "")) == "complete":
            return str(entry["entry_id"])
        payload = dict(entry.get("payload", {}))
        started_at = payload.get("started_at")
        duration = 0.0
        if isinstance(started_at, (int, float)) and started_at > 0:
            duration = max(round(time.time() - float(started_at), 2), 0.01)
        payload["duration_seconds"] = duration
        updated = self._update_entry(session, entry_id, state="complete", payload=payload)
        open_key = f"thought:open:{frame_id}"
        if session.transcript_index.get(open_key) == entry_id:
            session.transcript_index.pop(open_key, None)
        self._publish(session.session_id, updated)
        return str(updated["entry_id"])

    def complete_open_thoughts(self, session: Session) -> None:
        """结束会话中所有仍在 `thinking` 状态的 Thought 条目（轮次收尾兜底）。"""
        open_keys = [
            key for key in session.transcript_index if key.startswith("thought:open:")
        ]
        for key in open_keys:
            frame_id = key[len("thought:open:") :]
            self.complete_thought(session, frame_id=frame_id)

    # ------------------------------------------------------------------
    # 工具活动（原地更新）与审批
    # ------------------------------------------------------------------

    def start_server_tool(
        self,
        session: Session,
        *,
        tool_call_id: str,
        tool: str,
        args: dict[str, Any],
        agent: str | None,
        turn_id: str | None = None,
    ) -> str:
        """记录服务端工具开始执行（`tool_activity` 原地更新起点）。

        Args:
            session: 当前会话。
            tool_call_id: LLM 分配的工具调用 id，作为关联身份。
            tool: 工具名。
            args: 工具入参（超长字符串会被截断）。
            agent: 发起调用的 agent 名，可空。
            turn_id: 当前轮次 id，可空。

        Returns:
            工具活动条目的 `entry_id`。
        """
        key = f"tool:{tool_call_id}"
        entry_id = session.transcript_index.get(key)
        payload = {
            "tool": tool,
            "args": _bounded_args(args),
            "agent": agent,
            "is_error": False,
            "result_summary": None,
            "result_count": None,
            "render_kind": None,
        }
        if entry_id is None:
            entry = self._create_entry(
                session,
                kind="tool_activity",
                state="running",
                payload=payload,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                index_key=key,
            )
        else:
            entry = self._update_entry(session, entry_id, state="running", payload=payload)
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    def finish_server_tool(
        self,
        session: Session,
        *,
        tool_call_id: str,
        is_error: bool,
        result_summary: dict[str, Any] | None,
        result_count: int | None,
    ) -> str | None:
        """把服务端工具活动条目原地迁移到 `resolved`/`failed`。

        Args:
            session: 当前会话。
            tool_call_id: 与 `start_server_tool` 相同的工具调用 id。
            is_error: 工具执行是否失败。
            result_summary: 有界的结果摘要，可空。
            result_count: 结果条数，可空。

        Returns:
            工具活动条目的 `entry_id`；找不到对应活动时返回 None。
        """
        entry_id = session.transcript_index.get(f"tool:{tool_call_id}")
        if entry_id is None:
            logger.debug(
                "Server tool result without start entry session=%s tool_call_id=%s",
                session.session_id,
                tool_call_id,
            )
            return None
        entry = self._find_entry(session, entry_id)
        if entry is None:
            return None
        payload = dict(entry.get("payload", {}))
        payload["is_error"] = bool(is_error)
        payload["result_summary"] = result_summary
        payload["result_count"] = result_count
        updated = self._update_entry(
            session,
            entry_id,
            state="failed" if is_error else "resolved",
            payload=payload,
        )
        self._publish(session.session_id, updated)
        return str(updated["entry_id"])

    def record_front_tool_calls(
        self,
        session: Session,
        *,
        turn_id: str,
        calls: list[dict[str, Any]],
    ) -> None:
        """为一批 front 工具调用建立可见条目。

        需确认的调用建立 `approval`（`pending`）；静默调用建立
        `tool_activity`（`running`），等待结果回传时原地迁移。

        Args:
            session: 当前会话。
            turn_id: 本轮分配的 `turn_id`。
            calls: `FrontToolCall` 的字典形态列表，需含
                `id`/`name`/`input`/`needs_confirm`/`render_kind`。
        """
        for call in calls:
            call_id = str(call.get("id", ""))
            if not call_id:
                continue
            name = str(call.get("name", ""))
            raw_input = call.get("input", {})
            args = _bounded_args(raw_input) if isinstance(raw_input, dict) else {}
            render_kind = call.get("render_kind")
            if call.get("needs_confirm"):
                key = f"approval:{call_id}"
                entry = self._create_entry(
                    session,
                    kind="approval",
                    state="pending",
                    payload={
                        "tool": name,
                        "args": args,
                        "decision": None,
                        "render_kind": render_kind,
                        # 解决后降级为一行权限结果文本所需的持久化字段（任务 3.2）：
                        # 创建时即写入操作摘要与受影响路径，历史/重连后可直接重建。
                        "operation_summary": approval_operation_summary(name, args),
                        "affected_paths": approval_affected_paths(args),
                        "resolution_summary": None,
                    },
                    turn_id=turn_id,
                    tool_call_id=call_id,
                    index_key=key,
                )
            else:
                key = f"front:{call_id}"
                entry = self._create_entry(
                    session,
                    kind="tool_activity",
                    state="running",
                    payload={
                        "tool": name,
                        "args": args,
                        "agent": call.get("agent"),
                        "is_error": False,
                        "result_summary": None,
                        "result_count": None,
                        "render_kind": render_kind,
                    },
                    turn_id=turn_id,
                    tool_call_id=call_id,
                    index_key=key,
                )
            self._publish(session.session_id, entry)

    def record_front_tool_results(
        self,
        session: Session,
        *,
        results: list[dict[str, Any]],
    ) -> None:
        """按前端回传结果原地更新审批/工具活动条目。

        必须在 `session.clear_pending()` 之前调用，以便读取 pending 元数据。

        Args:
            session: 当前会话。
            results: 每项含 `tool_use_id` 与 `status`
                （`applied`/`rejected`/`error`）的字典列表。
        """
        for result in results:
            call_id = str(result.get("tool_use_id", ""))
            if not call_id:
                continue
            status = str(result.get("status", ""))
            approval_entry_id = session.transcript_index.get(f"approval:{call_id}")
            if approval_entry_id is not None:
                decision = "approved" if status == "applied" else "rejected"
                entry = self._find_entry(session, approval_entry_id)
                payload = dict(entry.get("payload", {})) if entry is not None else {}
                payload["decision"] = decision
                payload["resolution_summary"] = "已确认" if decision == "approved" else "已拒绝"
                # 旧版本创建的审批条目可能缺少操作摘要字段：解决时按持久化的
                # typed 入参补齐，仍不读取 UI 或原始传输。
                tool = str(payload.get("tool", ""))
                bounded_args = payload.get("args")
                args_dict = bounded_args if isinstance(bounded_args, dict) else {}
                payload.setdefault("operation_summary", approval_operation_summary(tool, args_dict))
                payload.setdefault("affected_paths", approval_affected_paths(args_dict))
                updated = self._update_entry(
                    session,
                    approval_entry_id,
                    state=decision,
                    payload=payload,
                )
                self._publish(session.session_id, updated)
                continue
            activity_entry_id = session.transcript_index.get(f"front:{call_id}")
            if activity_entry_id is None:
                continue
            entry = self._find_entry(session, activity_entry_id)
            if entry is None:
                continue
            payload = dict(entry.get("payload", {}))
            is_error = status != "applied"
            payload["is_error"] = is_error
            raw_result = result.get("result")
            if isinstance(raw_result, dict):
                payload["result_summary"] = _bounded_args(raw_result)
            elif raw_result is None:
                payload["result_summary"] = None
            else:
                text = str(raw_result)
                if len(text) > _ARGS_VALUE_MAX_CHARS:
                    text = text[:_ARGS_VALUE_MAX_CHARS] + "…(截断)"
                payload["result_summary"] = {"text": text}
            updated = self._update_entry(
                session,
                activity_entry_id,
                state="failed" if is_error else "resolved",
                payload=payload,
            )
            self._publish(session.session_id, updated)

    # ------------------------------------------------------------------
    # 计划与进度
    # ------------------------------------------------------------------

    def record_plan_created(
        self,
        session: Session,
        *,
        summary: str,
        steps: list[dict[str, Any]],
        turn_id: str | None = None,
    ) -> str:
        """记录一次 `create_plan` 产出的计划条目。

        Args:
            session: 当前会话。
            summary: 计划概述。
            steps: 步骤列表（每项至少含 `title`）。
            turn_id: 当前轮次 id，可空。

        Returns:
            计划条目的 `entry_id`。
        """
        normalized_steps = [
            {"title": str(step.get("title", "")), "status": "pending"} for step in steps
        ]
        entry = self._create_entry(
            session,
            kind="plan",
            state="complete",
            payload={"summary": summary, "steps": normalized_steps},
            turn_id=turn_id,
            index_key="plan:current",
        )
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    def record_plan_step(
        self,
        session: Session,
        *,
        step_index: int,
        total_steps: int,
        title: str | None,
        completed: bool,
        summary: str | None = None,
        turn_id: str | None = None,
    ) -> str | None:
        """记录计划步骤的开始/完成（`progress` 条目原地更新）。

        Args:
            session: 当前会话。
            step_index: 步骤序号（1 起，与编排事件一致）。
            total_steps: 计划总步数。
            title: 步骤标题；为 None 时从当前计划条目回查。
            completed: True 表示步骤完成，False 表示刚开始。
            summary: 步骤完成摘要，可空。
            turn_id: 当前轮次 id，可空。

        Returns:
            进度条目的 `entry_id`；无活跃计划时返回 None。
        """
        plan_entry_id = session.transcript_index.get("plan:current")
        if plan_entry_id is None:
            return None
        resolved_title = title if title else self._plan_step_title(session, plan_entry_id, step_index)
        key = f"step:{plan_entry_id}:{step_index}"
        entry_id = session.transcript_index.get(key)
        payload = {
            "step_index": step_index,
            "total_steps": total_steps,
            "title": resolved_title,
            "summary": summary,
        }
        if entry_id is None:
            entry = self._create_entry(
                session,
                kind="progress",
                state="running" if not completed else "complete",
                payload=payload,
                turn_id=turn_id,
                index_key=key,
            )
        else:
            entry = self._update_entry(
                session,
                entry_id,
                state="running" if not completed else "complete",
                payload=payload,
            )
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    # ------------------------------------------------------------------
    # 校验与错误
    # ------------------------------------------------------------------

    def start_verification(
        self,
        session: Session,
        *,
        tool_use_id: str,
        file_path: str,
        phase: str,
        turn_id: str | None = None,
    ) -> str:
        """记录一次文件校验开始（`verification` 条目原地更新起点）。

        Args:
            session: 当前会话。
            tool_use_id: 触发校验的工具调用 id。
            file_path: 被校验文件路径。
            phase: 校验阶段（`syntax`/`semantic`）。
            turn_id: 当前轮次 id，可空。

        Returns:
            校验条目的 `entry_id`。
        """
        key = f"verify:{tool_use_id}:{phase}"
        entry_id = session.transcript_index.get(key)
        payload = {
            "tool_use_id": tool_use_id,
            "file_path": file_path,
            "phase": phase,
            "issues_count": None,
            "summary": None,
        }
        if entry_id is None:
            entry = self._create_entry(
                session,
                kind="verification",
                state="running",
                payload=payload,
                turn_id=turn_id,
                tool_call_id=tool_use_id,
                index_key=key,
            )
        else:
            entry = self._update_entry(session, entry_id, state="running", payload=payload)
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    def finish_verification(
        self,
        session: Session,
        *,
        tool_use_id: str,
        phase: str,
        passed: bool,
        issues_count: int,
        summary: str,
    ) -> str | None:
        """把校验条目原地迁移到 `passed`/`failed`。

        Args:
            session: 当前会话。
            tool_use_id: 与 `start_verification` 相同的工具调用 id。
            phase: 校验阶段（`syntax`/`semantic`）。
            passed: 校验是否通过。
            issues_count: 发现的问题数。
            summary: 一句话总结。

        Returns:
            校验条目的 `entry_id`；找不到对应条目时返回 None。
        """
        entry_id = session.transcript_index.get(f"verify:{tool_use_id}:{phase}")
        if entry_id is None:
            return None
        entry = self._find_entry(session, entry_id)
        if entry is None:
            return None
        payload = dict(entry.get("payload", {}))
        payload["issues_count"] = issues_count
        payload["summary"] = summary
        updated = self._update_entry(
            session,
            entry_id,
            state="passed" if passed else "failed",
            payload=payload,
        )
        self._publish(session.session_id, updated)
        return str(updated["entry_id"])

    def record_error(
        self,
        session: Session,
        text: str,
        *,
        turn_id: str | None = None,
        context: str | None = None,
        modification_status: str | None = None,
        retryable: bool | None = None,
    ) -> str:
        """记录一条错误条目（可携带操作上下文/修改状态/可重试性）。

        结构化字段供错误渲染器展示失败的操作上下文、已知修改状态与是否可重试；
        均为可空——缺失时渲染端按“未提供/不可重试”处理，绝不猜测。

        Args:
            session: 当前会话。
            text: 用户可读的错误原因文本。
            turn_id: 当前轮次 id，可空。
            context: 失败时的操作/任务上下文描述，可空。
            modification_status: 已知修改状态（如“部分文件可能已被修改”），可空。
            retryable: 该错误是否可重试；None 表示未声明（渲染端不显示重试）。

        Returns:
            错误条目的 `entry_id`。
        """
        entry = self._create_entry(
            session,
            kind="error",
            state="complete",
            payload={
                "text": text,
                "context": context,
                "modification_status": modification_status,
                "retryable": retryable,
            },
            turn_id=turn_id,
        )
        self._publish(session.session_id, entry)
        return str(entry["entry_id"])

    # ------------------------------------------------------------------
    # 内部原语
    # ------------------------------------------------------------------

    def _create_entry(
        self,
        session: Session,
        *,
        kind: str,
        state: str,
        payload: dict[str, Any],
        turn_id: str | None = None,
        tool_call_id: str | None = None,
        index_key: str | None = None,
    ) -> dict[str, Any]:
        """创建一个新条目（revision=1），登记关联索引并返回条目字典。"""
        self._check_state(kind, state)
        session.transcript_entry_counter += 1
        entry = {
            "entry_id": f"e{session.transcript_entry_counter}",
            "ordinal": len(session.transcript_entries),
            "kind": kind,
            "state": state,
            "revision": 1,
            "turn_id": turn_id,
            "tool_call_id": tool_call_id,
            "payload": payload,
        }
        session.transcript_entries.append(entry)
        if index_key is not None:
            session.transcript_index[index_key] = str(entry["entry_id"])
        return entry

    def _update_entry(
        self,
        session: Session,
        entry_id: str,
        *,
        state: str | None = None,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        """原地更新既有条目的状态/载荷并递增 revision。

        Raises:
            KeyError: `entry_id` 不存在（内部状态损坏，属编程错误）。
        """
        entry = self._find_entry(session, entry_id)
        if entry is None:
            raise KeyError(f"transcript entry not found: {entry_id}")
        if state is not None:
            self._check_state(str(entry.get("kind", "")), state)
            entry["state"] = state
        if payload is not None:
            entry["payload"] = payload
        if turn_id is not None:
            entry["turn_id"] = turn_id
        entry["revision"] = int(entry.get("revision", 1)) + 1
        return entry

    def _publish(self, session_id: str, entry: dict[str, Any]) -> None:
        """发布一条 `transcript_patch` 事件（载荷为条目完整最新状态）。"""
        self._emit(
            session_id,
            "transcript_patch",
            {"entry": copy.deepcopy(entry), "stream_key": str(entry["entry_id"])},
        )

    @staticmethod
    def _find_entry(session: Session, entry_id: str) -> dict[str, Any] | None:
        """按 `entry_id` 查找条目；ordinal 即下标，可 O(1) 定位。"""
        for entry in reversed(session.transcript_entries):
            if entry.get("entry_id") == entry_id:
                return entry
        return None

    @staticmethod
    def _plan_step_title(session: Session, plan_entry_id: str, step_index: int) -> str:
        """从当前计划条目的 steps 中回查指定步骤标题；缺失时返回空串。"""
        for entry in reversed(session.transcript_entries):
            if entry.get("entry_id") == plan_entry_id:
                steps = entry.get("payload", {}).get("steps", [])
                if isinstance(steps, list) and 0 < step_index <= len(steps):
                    step = steps[step_index - 1]
                    if isinstance(step, dict):
                        return str(step.get("title", ""))
                return ""
        return ""

    @staticmethod
    def _check_state(kind: str, state: str) -> None:
        """校验 `(kind, state)` 组合合法；非法组合属编程错误。

        Raises:
            ValueError: `state` 不在该 `kind` 的合法取值集合内。
        """
        allowed = VALID_ENTRY_STATES.get(kind)
        if allowed is None or state not in allowed:
            raise ValueError(f"invalid transcript state: kind={kind!r} state={state!r}")
