"""Verify runner for post-edit syntax and semantic checks."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from app.api.schemas import VerifyResultDTO
from app.config import AppSettings
from app.llm.provider import LLMError, LLMProvider
from app.orchestrator.agent import EFFORT_TEMPERATURE, resolve_thinking_budget
from app.query.helpers import _parse_verify_response, _VERIFY_SYSTEM_PROMPT
from app.security.settings import SecuritySettings
from app.sessions.store import Session
from app.tools.context import ToolContext
from app.tools.server_tools.read_file import read_file_handler
from app.verify.syntax_check import run_syntax_check

logger = logging.getLogger(__name__)


class VerifyRunner:
    """运行编辑结果的语法快检和语义校验。"""

    def __init__(
        self,
        settings: AppSettings,
        llm: LLMProvider,
        emit: Callable[[str, str, dict[str, Any]], int],
        model_for_effort: Callable[[str], str | None],
        thinking_budget_for_effort: Callable[[str], int | None],
    ) -> None:
        """保存校验所需依赖。"""
        self._settings = settings
        self._llm = llm
        self._emit = emit
        self._model_for_effort = model_for_effort
        self._thinking_budget_for_effort = thinking_budget_for_effort

    async def run(
        self,
        session: Session,
        security: SecuritySettings,
        candidates: list[dict[str, Any]],
        model_override: str | None = None,
    ) -> None:
        """对一批编辑候选逐个执行两阶段校验。"""
        for candidate in candidates:
            await self._verify_one(session, security, candidate, model_override)

    async def _verify_one(
        self,
        session: Session,
        security: SecuritySettings,
        candidate: dict[str, Any],
        model_override: str | None = None,
    ) -> None:
        """对单个编辑结果跑 Phase 1 语法快检和 Phase 2 语义校验。"""
        settings = self._settings
        tool_use_id = str(candidate["tool_use_id"])
        frame_id = str(candidate["frame_id"])
        tool_name = str(candidate["tool_name"])
        path = str(candidate["path"])
        frame = next((f for f in session.agent_stack if f.id == frame_id), None)
        if frame is None:
            frame = session.top_frame()
            if frame is None:
                logger.warning(
                    "Verify skipped: frame missing session=%s frame=%s",
                    session.session_id,
                    frame_id,
                )
                return

        retries = session.verify_retry_count.get(path, 0)
        if retries >= settings.verify_max_retries:
            logger.info(
                "Verify skipped: max retries reached session=%s path=%s retries=%d",
                session.session_id,
                path,
                retries,
            )
            return

        if settings.verify_syntax_enabled:
            self._emit(
                session.session_id,
                "verify_started",
                {
                    "tool_use_id": tool_use_id,
                    "file_path": path,
                    "phase": "syntax",
                    "frame_id": frame.id,
                    "message_index": len(frame.messages),
                },
            )
            outcome = await run_syntax_check(
                path=path,
                project_root=security.project_root,
                godot_path=settings.verify_godot_path,
                timeout_s=settings.verify_syntax_timeout,
            )
            if outcome is not None:
                passed, issues = outcome
                if not passed:
                    summary = issues[0].message if issues else "语法检查失败"
                    self._emit(
                        session.session_id,
                        "verify_completed",
                        {
                            "tool_use_id": tool_use_id,
                            "file_path": path,
                            "passed": False,
                            "issues_count": len(issues),
                            "summary": summary,
                            "phase": "syntax",
                            "frame_id": frame.id,
                            "message_index": len(frame.messages),
                        },
                    )
                    frame.messages.append(
                        {
                            "role": "system",
                            "content": json.dumps(
                                {
                                    "verify": {
                                        "phase": "syntax",
                                        "passed": False,
                                        "issues": [issue.model_dump() for issue in issues],
                                        "summary": summary,
                                        "file_path": path,
                                    }
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    session.verify_retry_count[path] = retries + 1
                    logger.info(
                        "Verify syntax failed session=%s path=%s issues=%d retries=%d",
                        session.session_id,
                        path,
                        len(issues),
                        session.verify_retry_count[path],
                    )
                    return

        self._emit(
            session.session_id,
            "verify_started",
            {
                "tool_use_id": tool_use_id,
                "file_path": path,
                "phase": "semantic",
                "frame_id": frame.id,
                "message_index": len(frame.messages),
            },
        )
        result = await self._run_semantic_verify(
            security,
            tool_name,
            candidate.get("input", {}),
            path,
            model_override,
        )
        self._emit(
            session.session_id,
            "verify_completed",
            {
                "tool_use_id": tool_use_id,
                "file_path": path,
                "passed": result.passed,
                "issues_count": len(result.issues),
                "summary": result.summary,
                "phase": "semantic",
                "frame_id": frame.id,
                "message_index": len(frame.messages),
            },
        )
        frame.messages.append(
            {
                "role": "system",
                "content": json.dumps(
                    {
                        "verify": {
                            "phase": "semantic",
                            "passed": result.passed,
                            "issues": [issue.model_dump() for issue in result.issues],
                            "summary": result.summary,
                            "file_path": path,
                        }
                    },
                    ensure_ascii=False,
                ),
            }
        )
        session.verify_retry_count[path] = 0 if result.passed else retries + 1
        logger.info(
            "Verify semantic finished session=%s path=%s passed=%s issues=%d",
            session.session_id,
            path,
            result.passed,
            len(result.issues),
        )

    async def _run_semantic_verify(
        self,
        security: SecuritySettings,
        tool_name: str,
        tool_input: dict[str, Any],
        path: str,
        model_override: str | None = None,
    ) -> VerifyResultDTO:
        """调用 LLM 对改动后的文件内容做语义和逻辑校验。"""
        try:
            file_payload = await read_file_handler(
                {"path": path, "limit": 20000},
                ToolContext(security=security, session_id="verify"),
            )
        except (OSError, ValueError) as exc:
            logger.warning("Verify semantic skipped: cannot read file path=%s error=%s", path, exc)
            return VerifyResultDTO(passed=True, issues=[], summary=f"无法读取文件以校验：{exc}")

        user_payload = {
            "tool_name": tool_name,
            "tool_input_path": tool_input.get("path", path),
            "file_path": path,
            "file_content": str(file_payload.get("content", "")),
        }
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _VERIFY_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ]
        try:
            turn = await self._llm.chat(
                messages,
                [],
                model=model_override or self._model_for_effort(self._settings.verify_effort),
                temperature=EFFORT_TEMPERATURE.get(self._settings.verify_effort, 0.0),
                thinking_budget=resolve_thinking_budget(
                    self._settings.verify_effort, self._thinking_budget_for_effort
                ),
            )
        except LLMError as exc:
            logger.warning("Verify semantic LLM call failed path=%s error=%s", path, exc)
            return VerifyResultDTO(passed=True, issues=[], summary="校验调用失败，已跳过")

        return _parse_verify_response(turn.content or "")
