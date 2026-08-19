"""实现统一 CodeAct 执行网关与任务归属的工作区保护。"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.codeact.audit import CodeActAuditLog
from app.codeact.contracts import CodeActErrorCode, CodeActRequest, CodeActResult, CodeActToolName
from app.codeact.editor import EditorRegistry
from app.codeact.policy import policy_decision, tool_visible_to_role
from app.codeact.worker import WorkerManager, WorkerUnavailableError
from app.codeact.validation import ValidationSelector
from app.config import AppSettings
from app.orchestrator.map_request_scope import MapRequestScope, codeact_map_scope
from app.security.paths import (
    ProjectRootResolutionError,
    ProjectRoots,
    normalized_project_path,
    resolve_project_roots,
    resolved_path_for,
)
from app.tools.context import ToolContext
from app.tools.server_tools.grep_code import grep_code_handler
from app.tools.server_tools.list_files import list_files_handler
from app.tools.server_tools.load_skill import load_skill_handler
from app.tools.server_tools.read_delegate_artifact import read_delegate_artifact_handler
from app.tools.server_tools.read_file import read_file_handler
from app.tools.server_tools.read_map_artifact import read_map_artifact_handler
from app.tools.server_tools.read_planning_snapshot import read_planning_snapshot_handler
from app.tools.server_tools.search_codebase import search_codebase_handler
from app.tools.server_tools.search_tools import search_tools_handler

logger = logging.getLogger(__name__)

_ALLOWED_COMMANDS = frozenset({"godot", "python", "python3", "pytest", "rg", "pip", "pip3", "npm"})
_EDITOR_METHODS = frozenset(
    {
        CodeActToolName.EDITOR_STATUS,
        CodeActToolName.EDITOR_RELOAD,
        CodeActToolName.EDITOR_CAPTURE,
        CodeActToolName.EDITOR_RUNTIME,
        CodeActToolName.EDITOR_DEBUGGER,
        CodeActToolName.EDITOR_PROFILER,
    }
)


@dataclass(slots=True)
class WorkspaceBaseline:
    """记录任务开始时既有 diff 以及首次触及文件的内容摘要。"""

    initial_status: str
    initial_diff: str
    initial_changed_paths: frozenset[str] = frozenset()
    initial_files: dict[str, bytes | None] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)


class ExecutionGateway:
    """集中执行协议授权、worker 调度、冲突检测和审计。"""

    def __init__(
        self, settings: AppSettings, editor_registry: EditorRegistry | None = None
    ) -> None:
        self._settings = settings
        self._worker_manager = WorkerManager(
            settings, settings.project_root / ".ai_agent_service" / "codeact"
        )
        self._validation = ValidationSelector(self._worker_manager, settings)
        self._editor = editor_registry or EditorRegistry()
        self._editor.set_late_result_handler(self._record_late_editor_result)
        self._audit = CodeActAuditLog(
            settings.codeact_artifact_retention_bytes,
            storage_root=settings.project_root / ".ai_agent_service" / "codeact-audit",
        )
        self._baselines: dict[str, WorkspaceBaseline] = {}
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._owners: dict[str, tuple[str, str]] = {}
        self._cleanup_lock = asyncio.Lock()

    @property
    def editor_registry(self) -> EditorRegistry:
        """公开只支持观察注册的 Editor 注册表。"""
        return self._editor

    async def execute(self, request: CodeActRequest, context: ToolContext) -> CodeActResult:
        """执行一次已版本化调用，并把所有失败转换成类型化结果。"""
        if not self._settings.codeact_enabled:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.WORKER_UNAVAILABLE,
                "CodeAct is disabled",
                status="unavailable",
            )
        if not tool_visible_to_role(request.role, request.tool):
            return CodeActResult.failure(
                request, CodeActErrorCode.AUTHORIZATION_DENIED, "tool is unavailable for this role"
            )
        decision = policy_decision(request.tool, request.arguments)
        approved = request.call_id in context.approved_codeact_call_ids
        if decision == "deny":
            return CodeActResult.failure(
                request,
                CodeActErrorCode.AUTHORIZATION_DENIED,
                "policy denied this high-risk action",
            )
        if decision == "ask" and not approved:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.POLICY_APPROVAL_REQUIRED,
                "policy requires a trusted user approval",
                status="approval_required",
            )
        owner = (context.session_id, context.session_epoch)
        existing_owner = self._owners.setdefault(request.task_execution_id, owner)
        if existing_owner != owner:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.AUTHORIZATION_DENIED,
                "task execution identity is already bound to another session epoch",
            )
        if approved:
            self._audit.record(
                request.task_execution_id,
                "approval",
                {"call_id": request.call_id, "tool": request.tool.value, "decision": "allow"},
            )
        self._audit.record(request.task_execution_id, "request", request.model_dump(mode="json"))
        terminal_outcome: str | None = None
        try:
            async with self._locks[request.task_execution_id]:
                result = await self._dispatch(request, context)
        except asyncio.CancelledError:
            result = CodeActResult.failure(
                request, CodeActErrorCode.CANCELLED, "execution was cancelled", status="cancelled"
            )
            self._audit.record(request.task_execution_id, "result", result.model_dump(mode="json"))
            await self.finish(
                request.task_execution_id,
                outcome="cancelled",
                summary=result.model_dump(mode="json"),
            )
            raise
        except (ProjectRootResolutionError, ValueError) as exc:
            result = CodeActResult.failure(request, CodeActErrorCode.PATH_REJECTED, str(exc))
        except WorkerUnavailableError as exc:
            result = CodeActResult.failure(
                request,
                CodeActErrorCode.WORKER_UNAVAILABLE,
                str(exc),
                status="unavailable",
                retryable=True,
            )
        except TimeoutError:
            result = CodeActResult.failure(
                request, CodeActErrorCode.TIMEOUT, "execution timed out", retryable=True
            )
            terminal_outcome = "timeout"
        except OSError as exc:
            result = CodeActResult.failure(
                request, CodeActErrorCode.INVALID_REQUEST, str(exc), retryable=True
            )
        self._audit.record(request.task_execution_id, "result", result.model_dump(mode="json"))
        if terminal_outcome is not None:
            await self.finish(
                request.task_execution_id,
                outcome=terminal_outcome,
                summary=result.model_dump(mode="json"),
            )
        return result

    async def cancel(self, task_execution_id: str) -> None:
        """取消任务并执行统一终态清理。"""
        await self.finish(task_execution_id, outcome="cancelled")

    async def finish(
        self,
        task_execution_id: str,
        *,
        outcome: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """持久化最终审计证据并幂等释放一个任务的全部执行资源。"""
        async with self._cleanup_lock:
            await self._finish_locked(task_execution_id, outcome=outcome, summary=summary)

    async def _finish_locked(
        self,
        task_execution_id: str,
        *,
        outcome: str,
        summary: dict[str, Any] | None,
    ) -> None:
        """在清理互斥区内完成单个执行的终态持久化与资源释放。"""
        if (
            task_execution_id not in self._owners
            and task_execution_id not in self._baselines
            and task_execution_id not in self._locks
            and task_execution_id not in self._worker_manager.execution_ids()
            and task_execution_id not in self._audit.active_execution_ids()
        ):
            return
        self._audit.record(
            task_execution_id,
            "final_outcome",
            {"outcome": outcome, "summary": summary or {}},
        )
        try:
            await asyncio.to_thread(self._audit.persist, task_execution_id)
        except OSError:
            logger.exception("Unable to persist CodeAct audit execution=%s", task_execution_id)
        finally:
            await self._worker_manager.cancel(task_execution_id)
            self._baselines.pop(task_execution_id, None)
            self._locks.pop(task_execution_id, None)
            self._owners.pop(task_execution_id, None)
            self._audit.release(task_execution_id)

    async def finish_session(
        self,
        session_id: str,
        session_epoch: str,
        *,
        outcome: str,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """释放属于一个会话 epoch 的所有 CodeAct 执行。"""
        execution_ids = [
            execution_id
            for execution_id, owner in self._owners.items()
            if owner == (session_id, session_epoch)
        ]
        for execution_id in execution_ids:
            await self.finish(execution_id, outcome=outcome, summary=summary)

    async def close(self) -> None:
        """在服务关闭时持久化审计并清理所有残留执行资源。"""
        execution_ids = set(self._owners)
        execution_ids.update(self._baselines)
        execution_ids.update(self._locks)
        execution_ids.update(self._worker_manager.execution_ids())
        execution_ids.update(self._audit.active_execution_ids())
        for execution_id in sorted(execution_ids):
            await self.finish(execution_id, outcome="service_shutdown")
        await self._worker_manager.close()

    async def audit_timeline(self, task_execution_id: str) -> list[dict[str, Any]]:
        """返回前端仅展示用的有界执行证据。"""
        return await asyncio.to_thread(self._audit.timeline, task_execution_id)

    def _record_late_editor_result(self, payload: dict[str, Any]) -> None:
        """将取消或超时后抵达的 Editor 结果保留为审计证据。"""
        execution_id = str(payload.get("task_execution_id", "unbound"))
        self._audit.record(execution_id, "editor_late_result", payload)
        if execution_id not in self._owners:
            try:
                self._audit.persist(execution_id)
            except OSError:
                logger.exception("Unable to persist late Editor audit execution=%s", execution_id)
            self._audit.release(execution_id)

    async def _dispatch(self, request: CodeActRequest, context: ToolContext) -> CodeActResult:
        """路由到单一工具实现，保持旧工具读操作的安全语义。"""
        if request.tool is CodeActToolName.PROJECT_READ:
            return CodeActResult.success(
                request, await self._project_read(request.arguments, context)
            )
        if request.tool is CodeActToolName.PROJECT_SEARCH:
            return CodeActResult.success(
                request, await self._project_search(request.arguments, context)
            )
        if request.tool is CodeActToolName.SKILL_LOAD:
            return CodeActResult.success(
                request, await load_skill_handler(request.arguments, context)
            )
        if request.tool is CodeActToolName.TOOL_SEARCH:
            return CodeActResult.success(
                request, await search_tools_handler(request.arguments, context)
            )
        if request.tool in {CodeActToolName.GIT_STATUS, CodeActToolName.GIT_DIFF}:
            return await self._git(request, context)
        if request.tool is CodeActToolName.PROJECT_EDIT:
            return await self._edit(request, context)
        if request.tool in {CodeActToolName.SHELL_RUN, CodeActToolName.GODOT_HEADLESS}:
            return await self._run_worker_command(request, context)
        if request.tool in _EDITOR_METHODS:
            return await self._editor_call(request, context)
        return CodeActResult.failure(
            request, CodeActErrorCode.INVALID_REQUEST, "unsupported CodeAct tool"
        )

    async def _project_read(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        """在统一读协议下保留文件、列表与 artifact 的身份及分页校验。"""
        kind = arguments.get("kind", "file")
        if kind == "file":
            return await read_file_handler(arguments, context)
        if kind == "list":
            return await list_files_handler(arguments, context)
        if kind == "map_artifact":
            return await read_map_artifact_handler(arguments, context)
        if kind == "delegate_artifact":
            return await read_delegate_artifact_handler(arguments, context)
        if kind == "planning_snapshot":
            return await read_planning_snapshot_handler(arguments, context)
        raise ValueError("project.read kind is unsupported")

    async def _project_search(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any]:
        """在统一搜索协议下选择精确 grep 或本地代码库检索。"""
        kind = arguments.get("kind", "grep")
        if kind == "grep":
            return await grep_code_handler(arguments, context)
        if kind == "codebase":
            return await search_codebase_handler(arguments, context)
        raise ValueError("project.search kind is unsupported")

    async def _baseline(self, request: CodeActRequest, context: ToolContext) -> WorkspaceBaseline:
        """为任务创建一次只读 Git 基线，和现有修改严格分离。"""
        existing = self._baselines.get(request.task_execution_id)
        if existing is not None:
            return existing
        roots = resolve_project_roots(context.security)
        if roots.repository_root is None:
            baseline = WorkspaceBaseline("", "")
        else:
            status, diff, names = await asyncio.gather(
                self._git_output(roots.repository_root, "status", "--short"),
                self._git_output(roots.repository_root, "diff", "--no-ext-diff"),
                self._git_output(roots.repository_root, "diff", "--name-only", "--no-ext-diff"),
            )
            changed_paths = frozenset(set(filter(None, names.splitlines())) | _status_paths(status))
            baseline = WorkspaceBaseline(
                status,
                diff,
                changed_paths,
                await self._snapshot_files(roots, changed_paths),
            )
        self._baselines[request.task_execution_id] = baseline
        self._audit.record(
            request.task_execution_id,
            "workspace_baseline",
            {"status": baseline.initial_status, "diff": baseline.initial_diff},
        )
        return baseline

    async def _git(self, request: CodeActRequest, context: ToolContext) -> CodeActResult:
        """在独立仓库根仅执行只读 Git 状态或 diff 查询。"""
        roots = resolve_project_roots(context.security)
        if roots.repository_root is None:
            return CodeActResult.success(request, {"repository": None, "output": ""})
        arguments = (
            ("status", "--short")
            if request.tool is CodeActToolName.GIT_STATUS
            else ("diff", "--no-ext-diff")
        )
        output = await self._git_output(roots.repository_root, *arguments)
        return CodeActResult.success(
            request, {"repository": str(roots.repository_root), "output": output}
        )

    async def _git_output(self, root: Path, *arguments: str) -> str:
        """运行不可变的只读 Git 子命令并限制输出长度。"""
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), self._settings.codeact_worker_timeout_s
        )
        if process.returncode != 0:
            raise OSError(stderr.decode("utf-8", errors="replace")[:400])
        return stdout[: self._settings.codeact_artifact_retention_bytes].decode(
            "utf-8", errors="replace"
        )

    async def _edit(self, request: CodeActRequest, context: ToolContext) -> CodeActResult:
        """使用 worker 内临时脚本执行小型整文件替换，并执行摘要冲突检查。"""
        path = request.arguments.get("path")
        content = request.arguments.get("content")
        old_text = request.arguments.get("old_text")
        new_text = request.arguments.get("new_text")
        if not isinstance(path, str):
            return CodeActResult.failure(
                request, CodeActErrorCode.INVALID_REQUEST, "project.edit requires a string path"
            )
        normalized_path = normalized_project_path(path)
        if normalized_path is None:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.PATH_REJECTED,
                "project.edit requires a project-relative or res:// path",
            )
        path = normalized_path
        target = resolved_path_for(path, context.security, write=True)
        if target.suffix.lower() in context.security.editor_managed_extensions:
            project_id = str(resolve_project_roots(context.security).logical_project_root)
            status = await self._editor.invoke(
                project_id,
                {
                    "task_execution_id": request.task_execution_id,
                    "call_id": f"{request.call_id}:open-check",
                    "method": CodeActToolName.EDITOR_STATUS.value,
                    "parameters": {},
                    "timeout_seconds": request.timeout_seconds,
                },
                timeout_seconds=request.timeout_seconds,
            )
            opened_files = status.get("opened_files")
            if isinstance(opened_files, dict) and path in opened_files:
                return CodeActResult.failure(
                    request,
                    CodeActErrorCode.EDITOR_OPEN_CONFLICT,
                    "Editor currently has the target open; worker write was skipped",
                    data={"path": path, "dirty": bool(opened_files[path])},
                )
            self._audit.record(
                request.task_execution_id,
                "editor_open_check",
                {"path": path, "editor_status": status},
            )
        baseline = await self._baseline(request, context)
        current_digest = _digest(target) if target.exists() else "<missing>"
        first_digest = baseline.digests.setdefault(path, current_digest)
        if first_digest != current_digest:
            return CodeActResult.failure(
                request, CodeActErrorCode.WORKSPACE_CONFLICT, "target changed after first touch"
            )
        if isinstance(old_text, str) and isinstance(new_text, str):
            if not target.exists():
                return CodeActResult.failure(
                    request,
                    CodeActErrorCode.INVALID_REQUEST,
                    "a text patch requires an existing file",
                )
            current = await asyncio.to_thread(target.read_text, "utf-8")
            if current.count(old_text) != 1:
                return CodeActResult.failure(
                    request, CodeActErrorCode.INVALID_REQUEST, "old_text must occur exactly once"
                )
            content = current.replace(old_text, new_text, 1)
        elif not isinstance(content, str):
            return CodeActResult.failure(
                request,
                CodeActErrorCode.INVALID_REQUEST,
                "project.edit requires old_text/new_text or content",
            )
        elif target.exists() and not request.arguments.get("allow_full_replace", False):
            return CodeActResult.failure(
                request,
                CodeActErrorCode.INVALID_REQUEST,
                "existing files require a small old_text/new_text patch by default",
            )
        if len(content.encode("utf-8")) > self._settings.codeact_action_write_bytes:
            return CodeActResult.failure(
                request, CodeActErrorCode.INVALID_REQUEST, "write exceeds configured size guard"
            )
        roots = resolve_project_roots(context.security)
        worker = await self._worker_manager.get_or_create(request.task_execution_id, roots)
        script = worker.task_directory / "write_file.py"
        payload = json.dumps({"path": path, "content": content}, ensure_ascii=False)
        await asyncio.to_thread(script.write_text, _writer_script(payload), "utf-8")
        result = await self._worker_manager.run(
            worker, ("python3", "/task/write_file.py"), timeout_seconds=request.timeout_seconds
        )
        if result.exit_code != 0:
            return CodeActResult.failure(
                request, CodeActErrorCode.INVALID_REQUEST, result.stderr or result.stdout
            )
        after_digest = _digest(target) if target.exists() else "<missing>"
        baseline.digests[path] = after_digest
        diff = await self._task_diff(request, context, baseline)
        changed_paths = await self._task_changed_paths(context, baseline)
        if len(changed_paths) > self._settings.codeact_action_file_limit:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.INVALID_REQUEST,
                "write exceeds configured file-count guard",
                data={"changed_paths": changed_paths},
            )
        validation = await self._validate_action(
            request,
            context,
            worker,
            tuple(changed_paths or [path]),
        )
        data = {
            "path": path,
            "diff": diff,
            "output_truncated": result.output_truncated,
            "validation": validation.to_dict(),
        }
        self._audit.record(
            request.task_execution_id,
            "edit",
            {
                "path": path,
                "before_digest": current_digest,
                "after_digest": after_digest,
                **data,
            },
        )
        self._record_map_execution(request, context, validation.to_dict(), diff)
        return CodeActResult.success(request, data)

    async def _run_worker_command(
        self, request: CodeActRequest, context: ToolContext
    ) -> CodeActResult:
        """运行严格 argv 形式的 worker Shell 或 Godot headless 调用。"""
        raw = request.arguments.get("command")
        temporary_script = request.arguments.get("temporary_script")
        if temporary_script is not None and not isinstance(temporary_script, str):
            return CodeActResult.failure(
                request, CodeActErrorCode.INVALID_REQUEST, "temporary_script must be text"
            )
        if temporary_script is not None and request.tool is not CodeActToolName.GODOT_HEADLESS:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.AUTHORIZATION_DENIED,
                "temporary scripts require godot.headless",
            )
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(item, str) and item for item in raw)
        ):
            return CodeActResult.failure(
                request, CodeActErrorCode.INVALID_REQUEST, "command must be a non-empty argv array"
            )
        command = tuple(raw)
        worker = await self._worker_manager.get_or_create(
            request.task_execution_id, resolve_project_roots(context.security)
        )
        if temporary_script is not None:
            script_path = worker.task_directory / "codeact_temporary.gd"
            await asyncio.to_thread(script_path.write_text, temporary_script, "utf-8")
            script_hash = hashlib.sha256(temporary_script.encode("utf-8")).hexdigest()
            command = (
                "godot",
                "--headless",
                "--path",
                "/workspace",
                "--script",
                "/task/codeact_temporary.gd",
                *command,
            )
            self._audit.record(
                request.task_execution_id,
                "temporary_script",
                {"path": "/task/codeact_temporary.gd", "sha256": script_hash, "command": command},
            )
        elif request.tool is CodeActToolName.GODOT_HEADLESS:
            command = ("godot", "--headless", *command)
        if command[0] not in _ALLOWED_COMMANDS:
            return CodeActResult.failure(
                request, CodeActErrorCode.AUTHORIZATION_DENIED, "command prefix is not allowed"
            )
        baseline = await self._baseline(request, context)
        result = await self._worker_manager.run(
            worker, command, timeout_seconds=request.timeout_seconds
        )
        diff = await self._task_diff(request, context, baseline)
        changed_paths = await self._task_changed_paths(context, baseline)
        if len(changed_paths) > self._settings.codeact_action_file_limit:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.INVALID_REQUEST,
                "command exceeds configured file-count guard",
                data={"changed_paths": changed_paths, "diff": diff},
            )
        validation = (
            await self._validate_action(request, context, worker, tuple(changed_paths))
            if changed_paths
            else None
        )
        data = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "output_truncated": result.output_truncated,
            "diff": diff,
            "validation": (
                validation.to_dict()
                if validation is not None
                else {
                    "status": "unavailable",
                    "verifier": "none",
                    "version": "codeact-validator.v1",
                    "details": "no changed paths supplied",
                }
            ),
        }
        self._audit.record(
            request.task_execution_id,
            "worker_command",
            {
                "command": command,
                "cwd": "/workspace",
                "timeout_seconds": request.timeout_seconds,
                **data,
            },
        )
        self._record_map_execution(request, context, data["validation"], diff)
        return CodeActResult.success(request, data)

    async def _validate_action(
        self,
        request: CodeActRequest,
        context: ToolContext,
        worker: Any,
        paths: tuple[str, ...],
    ) -> Any:
        """为地图 owner 强制选择地图校验，其余角色按对象类型选择校验。"""
        if request.role == "map":
            raw_scope = context.map_request_scope
            scope = codeact_map_scope(raw_scope) if isinstance(raw_scope, MapRequestScope) else {}
            return await self._validation.validate_map(
                worker,
                paths,
                scope,
                timeout_seconds=request.timeout_seconds,
            )
        return await self._validation.validate(
            worker,
            paths,
            timeout_seconds=request.timeout_seconds,
        )

    def _record_map_execution(
        self,
        request: CodeActRequest,
        context: ToolContext,
        validation: dict[str, Any],
        diff: str,
    ) -> None:
        """将地图 CodeAct 验证写入 reducer-owned 恢复状态。"""
        if request.role != "map" or context.map_task_state is None:
            return
        from app.orchestrator.map_codeact import record_map_codeact_execution

        record_map_codeact_execution(
            context.map_task_state,
            task_execution_id=request.task_execution_id,
            validation=validation,
            diff_artifact=f"codeact://{request.task_execution_id}/diff",
            retry_budget=self._settings.codeact_map_retry_budget,
            repair_context={"diff": diff, "validation": validation},
        )
        self._audit.record(
            request.task_execution_id,
            "map_validation",
            {
                "validation": validation,
                "retry_budget": self._settings.codeact_map_retry_budget,
                "validation_failures": context.map_task_state.codeact_execution.get(
                    "validation_failures", 0
                ),
                "retries_remaining": context.map_task_state.codeact_execution.get(
                    "retries_remaining", 0
                ),
                "execution_status": context.map_task_state.codeact_execution.get(
                    "execution_status", ""
                ),
            },
        )

    async def _task_diff(
        self, request: CodeActRequest, context: ToolContext, baseline: WorkspaceBaseline
    ) -> str:
        """计算当前 diff 与任务开始前 diff 的差集证据。"""
        roots = resolve_project_roots(context.security)
        if roots.repository_root is None:
            return ""
        changed_paths = await self._task_changed_repo_paths(context, baseline)
        fragments: list[str] = []
        for relative in changed_paths:
            target = (roots.repository_root / relative).resolve()
            if relative in baseline.initial_files:
                fragments.append(
                    _content_diff(
                        relative,
                        baseline.initial_files[relative],
                        await asyncio.to_thread(_read_optional_bytes, target),
                    )
                )
                continue
            tracked_diff = await self._git_output(
                roots.repository_root,
                "diff",
                "HEAD",
                "--no-ext-diff",
                "--",
                relative,
            )
            if tracked_diff:
                fragments.append(tracked_diff)
            else:
                fragments.append(
                    _content_diff(
                        relative,
                        None,
                        await asyncio.to_thread(_read_optional_bytes, target),
                    )
                )
        return "\n".join(filter(None, fragments))[: self._settings.codeact_artifact_retention_bytes]

    async def _task_changed_paths(
        self, context: ToolContext, baseline: WorkspaceBaseline
    ) -> list[str]:
        """返回本任务新增或修改的路径，避免将任务开始前 diff 归属给当前调用。"""
        roots = resolve_project_roots(context.security)
        if roots.repository_root is None:
            return []
        changed = await self._task_changed_repo_paths(context, baseline)
        project_paths: list[str] = []
        for relative in changed:
            target = (roots.repository_root / relative).resolve()
            try:
                project_paths.append(target.relative_to(roots.resolved_project_root).as_posix())
            except ValueError:
                continue
        return sorted(project_paths)

    async def _task_changed_repo_paths(
        self,
        context: ToolContext,
        baseline: WorkspaceBaseline,
    ) -> list[str]:
        """按逐路径任务开始快照返回真正由当前任务改变的仓库路径。"""
        roots = resolve_project_roots(context.security)
        if roots.repository_root is None:
            return []
        output, status = await asyncio.gather(
            self._git_output(roots.repository_root, "diff", "--name-only", "--no-ext-diff"),
            self._git_output(roots.repository_root, "status", "--short"),
        )
        current_changed = set(filter(None, output.splitlines())) | _status_paths(status)
        candidates = current_changed | set(baseline.initial_changed_paths)
        task_changed: list[str] = []
        for relative in sorted(candidates):
            target = (roots.repository_root / relative).resolve()
            try:
                target.relative_to(roots.resolved_project_root)
            except ValueError:
                continue
            if relative not in baseline.initial_files:
                if relative in current_changed:
                    task_changed.append(relative)
                continue
            current_content = await asyncio.to_thread(_read_optional_bytes, target)
            if current_content != baseline.initial_files[relative]:
                task_changed.append(relative)
        return task_changed

    async def _snapshot_files(
        self,
        roots: ProjectRoots,
        paths: frozenset[str],
    ) -> dict[str, bytes | None]:
        """捕获任务开始时已有修改路径的文件内容以支持严格差异归属。"""
        snapshots: dict[str, bytes | None] = {}
        for relative in sorted(paths):
            target = (roots.repository_root / relative).resolve()
            try:
                target.relative_to(roots.resolved_project_root)
            except ValueError:
                continue
            snapshots[relative] = await asyncio.to_thread(_read_optional_bytes, target)
        return snapshots

    async def _editor_call(self, request: CodeActRequest, context: ToolContext) -> CodeActResult:
        """将已批准的只读观察请求交给匹配的本机 Plugin。"""
        if not self._settings.codeact_editor_rpc_enabled:
            return CodeActResult.failure(
                request,
                CodeActErrorCode.EDITOR_UNAVAILABLE,
                "Editor observation RPC is disabled by configuration",
                status="unavailable",
            )
        project_id = str(resolve_project_roots(context.security).logical_project_root)
        payload = {
            "task_execution_id": request.task_execution_id,
            "call_id": request.call_id,
            "method": request.tool.value,
            "parameters": request.arguments,
            "timeout_seconds": request.timeout_seconds,
        }
        result = await self._editor.invoke(
            project_id, payload, timeout_seconds=request.timeout_seconds
        )
        error_code = result.get("error_code")
        if isinstance(error_code, str):
            try:
                code = CodeActErrorCode(error_code)
            except ValueError:
                code = CodeActErrorCode.EDITOR_UNAVAILABLE
            return CodeActResult.failure(
                request, code, error_code, status="unavailable", retryable=True, data=result
            )
        artifacts = self._materialize_editor_artifacts(request, result)
        self._audit.record(
            request.task_execution_id,
            "editor_rpc",
            {"method": request.tool.value, "result": result},
        )
        return CodeActResult.success(request, result, artifacts=artifacts)

    def _materialize_editor_artifacts(
        self,
        request: CodeActRequest,
        result: dict[str, Any],
    ) -> tuple[str, ...]:
        """把 Plugin 返回的有界观察数据转成审计引用，避免内容被当作控制指令。"""
        artifact = result.pop("artifact", None)
        existing = result.get("artifact_ref")
        if isinstance(existing, str) and existing:
            return (existing,)
        if not isinstance(artifact, dict):
            return ()
        artifact_ref = f"codeact://{request.task_execution_id}/editor/{request.call_id}"
        self._audit.record(
            request.task_execution_id,
            "editor_artifact",
            {"artifact_ref": artifact_ref, **artifact},
        )
        result["artifact_ref"] = artifact_ref
        return (artifact_ref,)


def _read_optional_bytes(path: Path) -> bytes | None:
    """读取普通文件内容，不存在或非文件时返回空状态。"""
    return path.read_bytes() if path.is_file() else None


def _content_diff(relative: str, before: bytes | None, after: bytes | None) -> str:
    """生成相对于任务开始内容的文本差异或有界二进制变更标记。"""
    if before == after:
        return ""
    if b"\0" in (before or b"") or b"\0" in (after or b""):
        return f"Binary files a/{relative} and b/{relative} differ\n"
    before_lines = (
        before.decode("utf-8", errors="replace").splitlines(keepends=True)
        if before is not None
        else ()
    )
    after_lines = (
        after.decode("utf-8", errors="replace").splitlines(keepends=True)
        if after is not None
        else ()
    )
    return "".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{relative}" if before is not None else "/dev/null",
            tofile=f"b/{relative}" if after is not None else "/dev/null",
        )
    )


def _digest(path: Path) -> str:
    """计算文件内容摘要，用于拒绝首次触及后的并发漂移。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _writer_script(payload: str) -> str:
    """生成只写 `/workspace` 内相对文件的 worker 临时脚本。"""
    return "\n".join(
        (
            "import json",
            "from pathlib import Path",
            f"payload = json.loads({payload!r})",
            "root = Path('/workspace').resolve()",
            "target = (root / payload['path']).resolve()",
            "target.relative_to(root)",
            "target.parent.mkdir(parents=True, exist_ok=True)",
            "target.write_text(payload['content'], encoding='utf-8')",
        )
    )


def _status_paths(status: str, *, untracked_only: bool = False) -> set[str]:
    """解析 Git porcelain 路径，并过滤 worker 与 Godot 的隔离缓存。"""
    paths: set[str] = set()
    for line in status.splitlines():
        if len(line) < 4 or untracked_only and not line.startswith("??"):
            continue
        path = line[3:].split(" -> ")[-1].replace("\\", "/")
        if path.startswith((".godot/", ".ai_agent_service/codeact/", ".worker-tasks/")):
            continue
        paths.add(path)
    return paths
