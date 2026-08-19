"""CodeAct 执行边界、Docker worker 与恢复状态回归测试。"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.agents.loader import load_agent_file
from app.agents.types import resolve_effective_tools
from app.codeact.audit import CodeActAuditLog
from app.codeact.audit_routes import add_codeact_audit_routes
from app.codeact.contracts import CodeActErrorCode, CodeActRequest, CodeActResult, CodeActToolName
from app.codeact.editor import EditorLoopbackConnection, EditorRegistry
from app.codeact.gateway import ExecutionGateway
from app.codeact.identity import codeact_call_id, task_execution_id
from app.codeact.policy import policy_decision, tool_visible_to_role
from app.codeact.validation import ValidationSelector
from app.codeact.worker import (
    TaskWorker,
    WorkerManager,
    WorkerProcessResult,
    _worker_start_command,
)
from app.config import AppSettings
from app.orchestrator.map_codeact import record_map_codeact_execution
from app.orchestrator.map_request_scope import bind_map_task, codeact_map_scope, new_request_scope
from app.orchestrator.map_state import MapTaskState
from app.orchestrator.turn.event_projection import result_summary_for_event
from app.permissions.engine import PermissionContext, explicit_approval_granted
from app.security.paths import path_ok, resolve_project_roots, resolved_path_for
from app.security.settings import SecuritySettings
from app.tools.context import ToolContext
from app.tools.registry import REGISTRY, ToolDef, tools_for
from app.tools.server_tools.codeact_tools import _handler, register_codeact_tools


def _docker_available() -> bool:
    """判断本机 Docker daemon 和 CodeAct worker 镜像是否可用于集成测试。"""
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "image", "inspect", "ai-agent-codeact-worker:latest"],
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


class CodeActContractTests(unittest.TestCase):
    """验证版本化请求和角色策略的稳定边界。"""

    def test_request_rejects_blank_task_identity(self) -> None:
        """请求 DTO 必须拒绝空白任务身份，避免审计串线。"""
        with self.assertRaises(ValidationError):
            CodeActRequest(
                task_execution_id=" ",
                task_id="task",
                role="programming",
                call_id="call",
                tool=CodeActToolName.PROJECT_READ,
            )

    def test_advisor_cannot_write_or_run_shell(self) -> None:
        """顾问角色只能使用协议定义的只读工具。"""
        self.assertFalse(tool_visible_to_role("advisor", CodeActToolName.PROJECT_EDIT))
        self.assertFalse(tool_visible_to_role("advisor", CodeActToolName.SHELL_RUN))
        self.assertTrue(tool_visible_to_role("advisor", CodeActToolName.PROJECT_SEARCH))

    def test_policy_rejects_destructive_and_network_commands(self) -> None:
        """策略必须拒绝网络与破坏性命令，依赖安装需审批。"""
        self.assertEqual(
            policy_decision(
                CodeActToolName.SHELL_RUN, {"command": ["curl", "https://example.com"]}
            ),
            "deny",
        )
        self.assertEqual(
            policy_decision(CodeActToolName.SHELL_RUN, {"command": ["git", "reset", "--hard"]}),
            "deny",
        )
        self.assertEqual(
            policy_decision(CodeActToolName.SHELL_RUN, {"command": ["git", "merge", "topic"]}),
            "deny",
        )
        self.assertEqual(
            policy_decision(CodeActToolName.SHELL_RUN, {"command": ["pip", "install", "demo"]}),
            "ask",
        )

    def test_project_and_repository_roots_remain_distinct(self) -> None:
        """Git 证据根可位于项目上层，但 worker 根必须保持项目目录。"""
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            project = repository / "linked-project"
            project.mkdir()
            (repository / ".git").mkdir()
            roots = resolve_project_roots(SecuritySettings(project_root=project))
            self.assertEqual(roots.resolved_project_root, project.resolve())
        self.assertEqual(roots.repository_root, repository.resolve())

    def test_res_path_is_normalized_to_the_project_root(self) -> None:
        """`res://` 必须和相对路径解析到同一项目文件。"""
        with tempfile.TemporaryDirectory() as temporary:
            security = SecuritySettings(project_root=Path(temporary))
            relative = resolved_path_for("scene.tscn", security, write=True)
            res_path = resolved_path_for("res://scene.tscn", security, write=True)
        self.assertEqual(res_path, relative)

    def test_trusted_auto_approve_is_an_explicit_codeact_approval(self) -> None:
        """受信任工程的 auto_approve 可为 ask 动作提供可信批准路径。"""
        tool = ToolDef(
            name="shell.run",
            domain="core",
            side="server",
            executes_process=True,
        )
        security = SecuritySettings(
            project_root=Path.cwd(),
            trusted=True,
            permission_mode="auto_approve",
        )
        context = PermissionContext(
            security=security,
            effective_tools=frozenset({"shell.run"}),
        )
        self.assertTrue(
            explicit_approval_granted(tool, {"command": ["pip3", "install", "x"]}, context)
        )

    def test_worker_command_has_writable_tmpfs_and_read_only_git_metadata(self) -> None:
        """只读 rootfs 必须提供临时写空间并阻止脚本改写 Git 元数据。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / ".git").mkdir()
            roots = resolve_project_roots(SecuritySettings(project_root=root))
            worker = TaskWorker(
                task_execution_id="exec",
                container_name="codeact-exec",
                cache_volume_name="cache",
                task_directory=root / ".task",
                roots=roots,
            )
            command = _worker_start_command(worker, AppSettings(project_root=root))
        joined = " ".join(command)
        self.assertIn("/tmp:rw,nosuid,nodev", joined)
        self.assertIn("/home/codeact:rw,nosuid,nodev", joined)
        self.assertIn("dst=/workspace/.git,readonly", joined)

    def test_symlink_escape_is_rejected(self) -> None:
        """未列入 allowlist 的符号链接不得扩展可读写项目边界。"""
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            project = base / "project"
            outside = base / "outside"
            project.mkdir()
            outside.mkdir()
            link = project / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symbolic links unavailable: {exc}")
            security = SecuritySettings(project_root=project)
            self.assertFalse(path_ok("escape/secret.txt", security, write=False))
            self.assertFalse(path_ok("escape/secret.txt", security, write=True))

    def test_map_scope_survives_as_worker_validation_identity(self) -> None:
        """地图校验入口只接收持久化请求身份，不依赖可能被压缩的消息历史。"""
        scope = bind_map_task(
            new_request_scope(request_id="request", user_message="edit the map"),
            "map-task",
        )
        self.assertEqual(
            codeact_map_scope(scope),
            {
                "request_id": "request",
                "lineage_id": "request:request",
                "map_task_id": "map-task",
                "intent": "map_edit",
                "explicit_continuation": False,
            },
        )

    def test_gateway_result_projects_bounded_display_evidence(self) -> None:
        """前端事件仅获得有界 diff、校验和 artifact 展示数据。"""
        summary = result_summary_for_event(
            "project.edit",
            {
                "status": "ok",
                "data": {"diff": "+line", "validation": {"status": "passed"}},
                "artifacts": ["codeact://exec/diff"],
            },
            False,
        )
        self.assertEqual(summary["kind"], "codeact")
        self.assertEqual(summary["validation"]["status"], "passed")
        self.assertEqual(summary["artifacts"], ["codeact://exec/diff"])


class AuditAndMapRecoveryTests(unittest.TestCase):
    """验证审计脱敏与地图失败保留语义。"""

    def test_audit_redacts_sensitive_output(self) -> None:
        """持久化审计不得保留 credential-like 输出。"""
        audit = CodeActAuditLog(1_024)
        audit.record("exec", "shell", {"stdout": "token=top-secret"})
        event = audit.timeline("exec")[0]
        self.assertEqual(event["payload"]["stdout"], "token=[REDACTED]")

    def test_audit_persists_after_active_timeline_is_released(self) -> None:
        """终态释放内存后仍可从持久文件读取有界脱敏时间线。"""
        with tempfile.TemporaryDirectory() as temporary:
            audit = CodeActAuditLog(1_024, storage_root=Path(temporary))
            audit.record("exec-audit", "result", {"stdout": "token=top-secret"})
            audit.persist("exec-audit")
            audit.release("exec-audit")

            events = audit.timeline("exec-audit")

        self.assertEqual(events[0]["payload"]["stdout"], "token=[REDACTED]")

    def test_map_failure_exhaustion_retains_diff(self) -> None:
        """最终验证失败必须进入 failed_validation 且保留 diff 引用。"""
        state = MapTaskState()
        validation = {"status": "failed", "verifier": "map_validator"}
        record_map_codeact_execution(
            state,
            task_execution_id="map-exec",
            validation=validation,
            diff_artifact="codeact://map-exec/diff",
            retry_budget=1,
        )
        self.assertEqual(state.codeact_execution["execution_status"], "repair_required")
        record_map_codeact_execution(
            state,
            task_execution_id="map-exec",
            validation=validation,
            diff_artifact="codeact://map-exec/diff",
            retry_budget=1,
        )
        self.assertEqual(state.codeact_execution["execution_status"], "failed_validation")
        self.assertEqual(state.codeact_execution["recovery_disposition"], "retain_diff")

    def test_map_unavailable_is_terminal_and_passed_is_validated(self) -> None:
        """地图校验不可用不得继续成功，只有 passed 可以解除完成门控。"""
        state = MapTaskState()
        record_map_codeact_execution(
            state,
            task_execution_id="map-unavailable",
            validation={"status": "unavailable", "verifier": "map_validator"},
            diff_artifact="codeact://map-unavailable/diff",
            retry_budget=2,
        )
        self.assertEqual(state.codeact_execution["execution_status"], "failed_validation")
        record_map_codeact_execution(
            state,
            task_execution_id="map-passed",
            validation={"status": "passed", "verifier": "map_validator"},
            diff_artifact="codeact://map-passed/diff",
            retry_budget=2,
        )
        self.assertEqual(state.codeact_execution["execution_status"], "validated")

    def test_legacy_editor_transactions_are_retired_without_rollback(self) -> None:
        """恢复旧状态时丢弃 Editor 事务日志，并明确保留当前磁盘 diff。"""
        state = MapTaskState.from_dict(
            {
                "status": "running",
                "stage": "validate",
                "transaction_journals": [{"transaction_id": "legacy"}],
            }
        )
        self.assertEqual(state.transaction_journals, [])
        self.assertEqual(state.codeact_execution["legacy_editor_transactions_retired"], 1)
        self.assertEqual(state.codeact_execution["recovery_disposition"], "retain_diff")


class EditorRegistryTests(unittest.IsolatedAsyncioTestCase):
    """验证 Editor 注册身份、allowlist 和撤销。"""

    async def test_registration_routes_only_allowed_methods_then_revokes(self) -> None:
        """注册实例应只响应 allowlist，撤销后立即不可用。"""
        registry = EditorRegistry()

        async def transport(payload: dict[str, object]) -> dict[str, object]:
            """返回带项目和方法回显的模拟观察结果。"""
            return {
                "project_id": "project",
                "method": payload["method"],
                "opened_files": {"scene.tscn": False},
            }

        await registry.register("project", "instance", transport)
        status = await registry.invoke(
            "project",
            {"call_id": "1", "method": "godot.editor.status"},
            timeout_seconds=1,
        )
        self.assertEqual(status["opened_files"], {"scene.tscn": False})
        denied = await registry.invoke(
            "project",
            {"call_id": "2", "method": "godot.editor.write_scene"},
            timeout_seconds=1,
        )
        self.assertEqual(denied["error_code"], "authorization_denied")
        await registry.revoke("project", "instance")
        unavailable = await registry.invoke(
            "project",
            {"call_id": "3", "method": "godot.editor.status"},
            timeout_seconds=1,
        )
        self.assertEqual(unavailable["error_code"], "editor_unavailable")

    async def test_expired_token_and_project_mismatch_are_typed(self) -> None:
        """过期注册不可调用，跨项目结果也不能成为权威观察。"""
        registry = EditorRegistry()
        await registry.register("expired", "instance", ttl_seconds=0)
        expired = await registry.invoke(
            "expired",
            {"call_id": "expired", "method": "godot.editor.status"},
            timeout_seconds=1,
        )
        self.assertEqual(expired["error_code"], "editor_unavailable")

        async def mismatched(_payload: dict[str, object]) -> dict[str, object]:
            """模拟错误项目返回。"""
            return {"project_id": "other"}

        await registry.register("project", "instance", mismatched)
        mismatch = await registry.invoke(
            "project",
            {"call_id": "mismatch", "method": "godot.editor.status"},
            timeout_seconds=1,
        )
        self.assertEqual(mismatch["error_code"], "project_mismatch")

    async def test_late_result_is_audit_only(self) -> None:
        """已取消 waiter 的迟到结果只触发审计回调，不恢复原调用。"""
        late_results: list[dict[str, object]] = []
        connection = EditorLoopbackConnection(late_results.append)
        pending = asyncio.create_task(connection.request({"call_id": "late"}))
        await connection.next_request()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertFalse(connection.resolve({"call_id": "late", "task_execution_id": "exec"}))
        self.assertEqual(late_results, [{"call_id": "late", "task_execution_id": "exec"}])

    async def test_busy_cancel_and_timeout_outcomes_are_typed(self) -> None:
        """Editor transport failures保持可机读，不泄露为成功观察。"""
        registry = EditorRegistry()

        async def busy(_payload: dict[str, object]) -> dict[str, object]:
            """返回 busy 状态。"""
            return {"error_code": "editor_busy"}

        await registry.register("busy", "instance", busy)
        result = await registry.invoke(
            "busy",
            {"call_id": "busy", "method": "godot.editor.status"},
            timeout_seconds=1,
        )
        self.assertEqual(result["error_code"], "editor_busy")

        async def cancelled(_payload: dict[str, object]) -> dict[str, object]:
            """模拟 Plugin transport 取消。"""
            raise asyncio.CancelledError

        await registry.register("cancelled", "instance", cancelled)
        result = await registry.invoke(
            "cancelled",
            {"call_id": "cancelled", "method": "godot.editor.status"},
            timeout_seconds=1,
        )
        self.assertEqual(result["error_code"], "editor_cancelled")

        async def slow(_payload: dict[str, object]) -> dict[str, object]:
            """模拟超过 Gateway deadline 的 Plugin。"""
            await asyncio.sleep(2)
            return {}

        await registry.register("slow", "instance", slow)
        result = await registry.invoke(
            "slow",
            {"call_id": "slow", "method": "godot.editor.status"},
            timeout_seconds=1,
        )
        self.assertEqual(result["error_code"], "timeout")

    async def test_reload_requires_trusted_approval_and_capture_returns_artifact(self) -> None:
        """模型参数不能伪造 reload 审批，观察数据由 Gateway 转成 artifact 引用。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = EditorRegistry()

            async def transport(payload: dict[str, object]) -> dict[str, object]:
                """模拟只读 EditorPlugin。"""
                if payload["method"] == "godot.editor.viewport_capture":
                    return {
                        "project_id": project_id,
                        "artifact": {"mime_type": "image/png", "data": "AA=="},
                    }
                return {"project_id": project_id, "reloaded": True}

            gateway = ExecutionGateway(
                AppSettings(project_root=root, codeact_editor_rpc_enabled=True),
                registry,
            )
            base_context = ToolContext(
                security=SecuritySettings(project_root=root),
                session_id="session",
                agent_role="programming",
            )
            project_id = str(resolve_project_roots(base_context.security).logical_project_root)
            await registry.register(project_id, "instance", transport)
            reload_request = CodeActRequest(
                task_execution_id="editor-exec",
                task_id="task",
                role="programming",
                call_id="reload",
                tool=CodeActToolName.EDITOR_RELOAD,
                arguments={"path": "scene.tscn", "approved": True},
            )
            approval = await gateway.execute(reload_request, base_context)
            self.assertEqual(approval.status, "approval_required")
            approved_context = ToolContext(
                security=base_context.security,
                session_id="session",
                agent_role="programming",
                approved_codeact_call_ids=frozenset({"reload"}),
            )
            reloaded = await gateway.execute(reload_request, approved_context)
            self.assertEqual(reloaded.status, "ok", reloaded.model_dump(mode="json"))

            capture = reload_request.model_copy(
                update={
                    "call_id": "capture",
                    "tool": CodeActToolName.EDITOR_CAPTURE,
                    "arguments": {},
                }
            )
            captured = await gateway.execute(capture, base_context)
            self.assertEqual(captured.status, "ok")
            self.assertEqual(captured.artifacts, ("codeact://editor-exec/editor/capture",))
            self.assertNotIn("artifact", captured.data)

    async def test_res_path_open_scene_is_rejected_before_worker_write(self) -> None:
        """Editor 返回相对路径时，`res://` 请求仍必须命中打开文件冲突。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scene.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
            registry = EditorRegistry()
            security = SecuritySettings(project_root=root)
            project_id = str(resolve_project_roots(security).logical_project_root)

            async def transport(_payload: dict[str, object]) -> dict[str, object]:
                """返回已打开且 clean 的场景状态。"""
                return {"project_id": project_id, "opened_files": {"scene.tscn": False}}

            await registry.register(project_id, "instance", transport)
            gateway = ExecutionGateway(AppSettings(project_root=root), registry)
            request = CodeActRequest(
                task_execution_id="editor-open",
                task_id="task",
                role="scene",
                call_id="edit",
                tool=CodeActToolName.PROJECT_EDIT,
                arguments={
                    "path": "res://scene.tscn",
                    "old_text": "format=3",
                    "new_text": "format=3 load_steps=1",
                },
            )
            result = await gateway.execute(
                request,
                ToolContext(security=security, session_id="session", agent_role="scene"),
            )
        self.assertEqual(result.error_code, CodeActErrorCode.EDITOR_OPEN_CONFLICT)


class CodeActToolIdentityTests(unittest.IsolatedAsyncioTestCase):
    """验证统一工具忽略模型身份并绑定后端 frame/call 身份。"""

    async def test_handler_uses_backend_execution_and_unique_call_identity(self) -> None:
        """模型提供的执行 id 不得影响 worker 归属，scene 角色保持独立。"""
        captured: list[CodeActRequest] = []

        class FakeGateway:
            async def execute(
                self,
                request: CodeActRequest,
                _context: ToolContext,
            ) -> CodeActResult:
                """记录 Gateway 请求并返回类型化成功结果。"""
                captured.append(request)
                return CodeActResult.success(request, {})

        execution_id = task_execution_id("session", "epoch", "frame")
        handler = _handler(CodeActToolName.PROJECT_READ)
        for raw_call_id in ("one", "two"):
            await handler(
                {"kind": "list", "task_execution_id": "model-controlled"},
                ToolContext(
                    security=SecuritySettings(project_root=Path.cwd()),
                    session_id="session",
                    session_epoch="epoch",
                    agent_role="scene",
                    execution_gateway=cast(ExecutionGateway, FakeGateway()),
                    task_execution_id=execution_id,
                    tool_call_id=raw_call_id,
                ),
            )
        self.assertEqual({request.task_execution_id for request in captured}, {execution_id})
        self.assertEqual({request.role for request in captured}, {"scene"})
        self.assertEqual(
            [request.call_id for request in captured],
            [codeact_call_id(execution_id, "one"), codeact_call_id(execution_id, "two")],
        )


class CodeActLifecycleAndDeferredToolTests(unittest.IsolatedAsyncioTestCase):
    """验证终态资源收尾、审计查询和按需 Editor 能力。"""

    async def test_finish_session_clears_execution_state_and_preserves_audit_route(self) -> None:
        """会话终态必须清除 worker 索引，并保留可经 API 查询的最终时间线。"""

        class FakeWorkers:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            def execution_ids(self) -> tuple[str, ...]:
                return ()

            async def cancel(self, execution_id: str) -> None:
                self.cancelled.append(execution_id)

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = AppSettings(project_root=root)
            gateway = ExecutionGateway(settings)
            workers = FakeWorkers()
            gateway._worker_manager = cast(WorkerManager, workers)
            execution_id = task_execution_id("session", "epoch", "frame")
            request = CodeActRequest(
                task_execution_id=execution_id,
                task_id="task",
                role="programming",
                call_id=codeact_call_id(execution_id, "read"),
                tool=CodeActToolName.GIT_STATUS,
                arguments={},
            )
            result = await gateway.execute(
                request,
                ToolContext(
                    security=SecuritySettings(project_root=root),
                    session_id="session",
                    session_epoch="epoch",
                    agent_role="programming",
                ),
            )
            self.assertEqual(result.status, "ok")

            await gateway.finish_session(
                "session",
                "epoch",
                outcome="completed",
                summary={"type": "final"},
            )

            self.assertEqual(gateway._owners, {})
            self.assertEqual(gateway._locks, {})
            self.assertEqual(gateway._baselines, {})
            self.assertIn(execution_id, workers.cancelled)
            timeline = await gateway.audit_timeline(execution_id)
            self.assertEqual(timeline[-1]["kind"], "final_outcome")

            app = FastAPI()
            router = APIRouter()
            add_codeact_audit_routes(router, gateway)
            app.include_router(router)
            with TestClient(app) as client:
                response = client.get(f"/codeact/audit/{execution_id}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["events"][-1]["kind"], "final_outcome")

    async def test_editor_observations_are_deferred_but_activatable_in_role_scope(self) -> None:
        """Editor 观察默认不进 prompt，但角色可通过 deferred 激活获得 schema。"""
        previous = REGISTRY.copy()
        try:
            REGISTRY.clear()
            register_codeact_tools()
            path = (
                Path(__file__).parents[1] / "app" / "agents" / "agent_defs" / "programming-agent.md"
            )
            agent = resolve_effective_tools(load_agent_file(path), set(REGISTRY))
            tool_name = "godot.editor.debugger_errors"

            self.assertIn(tool_name, agent.effective_tools)
            self.assertTrue(REGISTRY[tool_name].deferred)
            default_names = {item["function"]["name"] for item in tools_for(agent.effective_tools)}
            activated_names = {
                item["function"]["name"] for item in tools_for(agent.effective_tools, {tool_name})
            }
            self.assertNotIn(tool_name, default_names)
            self.assertIn(tool_name, activated_names)
        finally:
            REGISTRY.clear()
            REGISTRY.update(previous)

    async def test_timeout_and_task_cancellation_release_execution_resources(self) -> None:
        """超时返回类型化结果，任务取消向上传播，二者都必须先完成资源清理。"""

        class FakeWorkers:
            def __init__(self) -> None:
                self.cancelled: list[str] = []

            def execution_ids(self) -> tuple[str, ...]:
                return ()

            async def cancel(self, execution_id: str) -> None:
                self.cancelled.append(execution_id)

            async def close(self) -> None:
                return None

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            gateway = ExecutionGateway(AppSettings(project_root=root))
            workers = FakeWorkers()
            gateway._worker_manager = cast(WorkerManager, workers)
            context = ToolContext(
                security=SecuritySettings(project_root=root),
                session_id="session",
                session_epoch="epoch",
                agent_role="programming",
            )

            timeout_id = task_execution_id("session", "epoch", "timeout-frame")
            timeout_request = CodeActRequest(
                task_execution_id=timeout_id,
                task_id="task",
                role="programming",
                call_id=codeact_call_id(timeout_id, "call"),
                tool=CodeActToolName.GIT_STATUS,
            )

            async def time_out(_request: CodeActRequest, _context: ToolContext) -> CodeActResult:
                raise TimeoutError

            setattr(gateway, "_dispatch", time_out)
            timeout_result = await gateway.execute(timeout_request, context)
            self.assertEqual(timeout_result.error_code, CodeActErrorCode.TIMEOUT)
            self.assertIn(timeout_id, workers.cancelled)

            cancel_id = task_execution_id("session", "epoch", "cancel-frame")
            cancel_request = timeout_request.model_copy(
                update={
                    "task_execution_id": cancel_id,
                    "call_id": codeact_call_id(cancel_id, "call"),
                }
            )
            entered = asyncio.Event()

            async def block(_request: CodeActRequest, _context: ToolContext) -> CodeActResult:
                entered.set()
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            setattr(gateway, "_dispatch", block)
            task = asyncio.create_task(gateway.execute(cancel_request, context))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIn(cancel_id, workers.cancelled)
            self.assertEqual(gateway._owners, {})
            self.assertEqual(gateway._locks, {})


class MapValidationSelectorTests(unittest.IsolatedAsyncioTestCase):
    """验证每个地图写后动作都可路由到项目配置的 worker 校验入口。"""

    async def test_configured_map_validator_receives_scope_and_changed_paths(self) -> None:
        """地图校验命令携带稳定 scope 与实际变更路径。"""
        calls: list[tuple[str, ...]] = []

        class FakeWorkers:
            async def run(
                self, _worker: object, command: tuple[str, ...], *, timeout_seconds: int
            ) -> WorkerProcessResult:
                """记录隔离 worker 命令。"""
                self_timeout = timeout_seconds
                self.assert_timeout = self_timeout
                calls.append(command)
                return WorkerProcessResult(0, "ok", "", False)

        with tempfile.TemporaryDirectory() as temporary:
            settings = AppSettings(
                project_root=Path(temporary),
                codeact_map_validator_command=["python3", "tools/validate_map.py"],
            )
            selector = ValidationSelector(cast(WorkerManager, FakeWorkers()), settings)
            result = await selector.validate_map(
                cast(TaskWorker, object()),
                ("maps/level.tscn",),
                {"map_task_id": "map-task"},
                timeout_seconds=12,
            )
        self.assertEqual(result.status, "passed")
        self.assertEqual(calls[0][:2], ("python3", "tools/validate_map.py"))
        self.assertIn("map-task", calls[0][3])
        self.assertIn("maps/level.tscn", calls[0][5])


@unittest.skipUnless(_docker_available(), "requires local Docker CodeAct worker image")
class GatewayWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    """验证 Gateway 只通过 worker 写入且拒绝并发工作区漂移。"""

    async def test_second_edit_rejects_external_workspace_drift(self) -> None:
        """首次触及后文件被外部修改时，后续补丁不得覆盖该修改。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project.godot").write_text(
                '[application]\nconfig/name="test"\n', encoding="utf-8"
            )
            target = root / "sample.gd"
            target.write_text("one\n", encoding="utf-8")
            settings = AppSettings(project_root=root, codeact_worker_timeout_s=30)
            gateway = ExecutionGateway(settings)
            self.addAsyncCleanup(gateway.cancel, "workspace-test")
            context = ToolContext(
                security=SecuritySettings(project_root=root),
                session_id="session",
                agent_role="programming",
            )
            first = CodeActRequest(
                task_execution_id="workspace-test",
                task_id="task",
                role="programming",
                call_id="one",
                tool=CodeActToolName.PROJECT_EDIT,
                arguments={"path": "sample.gd", "old_text": "one", "new_text": "two"},
            )
            first_result = await gateway.execute(first, context)
            self.assertEqual(first_result.status, "ok", first_result.message)
            target.write_text("external\n", encoding="utf-8")
            second = first.model_copy(
                update={
                    "call_id": "two",
                    "arguments": {"path": "sample.gd", "old_text": "two", "new_text": "three"},
                }
            )
            second_result = await gateway.execute(second, context)
            self.assertEqual(second_result.error_code, CodeActErrorCode.WORKSPACE_CONFLICT)
            self.assertEqual(target.read_text(encoding="utf-8"), "external\n")


@unittest.skipUnless(_docker_available(), "requires local Docker CodeAct worker image")
class WorkerIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """以真实 Docker 验证 task-scoped worker 的隔离和清理。"""

    async def test_worker_reuses_container_isolates_cache_and_cleans_up(self) -> None:
        """同任务复用容器，缓存不落宿主，关闭后容器与 volume 必须删除。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "project.godot").write_text(
                '[application]\nconfig/name="test"\n', encoding="utf-8"
            )
            settings = AppSettings(project_root=root, codeact_worker_timeout_s=30)
            manager = WorkerManager(settings, root / ".codeact")
            roots = resolve_project_roots(SecuritySettings(project_root=root))
            worker = await manager.get_or_create("worker-integration", roots)
            same_worker = await manager.get_or_create("worker-integration", roots)
            self.assertIs(worker, same_worker)
            cache_write = await manager.run(
                worker,
                (
                    "python3",
                    "-c",
                    "from pathlib import Path; Path('/workspace/.godot/cache.txt').write_text('ok')",
                ),
                timeout_seconds=10,
            )
            self.assertEqual(cache_write.exit_code, 0, cache_write.stderr)
            self.assertFalse((root / ".godot" / "cache.txt").exists())
            inspection = subprocess.run(
                ["docker", "container", "inspect", worker.container_name],
                capture_output=True,
                check=True,
                text=True,
            )
            details = json.loads(inspection.stdout)[0]
            destinations = {mount["Destination"] for mount in details["Mounts"]}
            self.assertEqual(destinations, {"/workspace", "/workspace/.godot", "/task"})
            self.assertEqual(details["HostConfig"]["NetworkMode"], "none")
            self.assertEqual(details["HostConfig"]["PidsLimit"], settings.codeact_worker_pids_limit)
            self.assertEqual(
                details["HostConfig"]["Memory"], settings.codeact_worker_memory_mb * 1024 * 1024
            )
            self.assertEqual(
                details["HostConfig"]["NanoCpus"], int(settings.codeact_worker_cpu * 1_000_000_000)
            )
            network = await manager.run(
                worker,
                (
                    "python3",
                    "-c",
                    "import socket; socket.create_connection(('1.1.1.1', 80), 1)",
                ),
                timeout_seconds=10,
            )
            self.assertNotEqual(network.exit_code, 0)
            with self.assertRaises(TimeoutError):
                await manager.run(worker, ("sleep", "10"), timeout_seconds=1)
            container_name = worker.container_name
            volume_name = worker.cache_volume_name
            await manager.cancel("worker-integration")
            container = subprocess.run(
                ["docker", "container", "inspect", container_name], capture_output=True, check=False
            )
            volume = subprocess.run(
                ["docker", "volume", "inspect", volume_name], capture_output=True, check=False
            )
            self.assertNotEqual(container.returncode, 0)
            self.assertNotEqual(volume.returncode, 0)


if __name__ == "__main__":
    unittest.main()
