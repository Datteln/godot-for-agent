"""Unified CodeAct read-edit-verify acceptance coverage without a Docker dependency."""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast

from app.codeact.contracts import CodeActRequest, CodeActRole, CodeActToolName
from app.codeact.gateway import ExecutionGateway
from app.codeact.validation import ValidationSelector
from app.codeact.worker import TaskWorker, WorkerManager, WorkerProcessResult
from app.config import AppSettings
from app.orchestrator.map_request_scope import bind_map_task, new_request_scope
from app.orchestrator.map_state import MapTaskState
from app.security.paths import ProjectRoots
from app.security.settings import SecuritySettings
from app.tools.context import ToolContext


class _AcceptanceWorkerManager:
    """Simulate only the worker filesystem boundary while preserving Gateway orchestration."""

    def __init__(self, root: Path, task_root: Path) -> None:
        self.root = root
        self.task_root = task_root
        self.workers: dict[str, TaskWorker] = {}
        self.cancelled: list[str] = []

    async def get_or_create(self, execution_id: str, roots: object) -> TaskWorker:
        """Return one isolated task directory per execution id."""
        existing = self.workers.get(execution_id)
        if existing is not None:
            return existing
        directory = self.task_root / execution_id
        directory.mkdir(parents=True)
        worker = TaskWorker(
            execution_id,
            f"fake-{execution_id}",
            f"fake-cache-{execution_id}",
            directory,
            cast(ProjectRoots, roots),
            True,
        )
        self.workers[execution_id] = worker
        return worker

    async def run(
        self,
        worker: TaskWorker,
        command: tuple[str, ...],
        *,
        timeout_seconds: int,
    ) -> WorkerProcessResult:
        """Apply generated project edits and report deterministic verifier success."""
        del timeout_seconds
        if command == ("python3", "/task/write_file.py"):
            source = (worker.task_directory / "write_file.py").read_text(encoding="utf-8")
            payload_line = next(line for line in source.splitlines() if line.startswith("payload = json.loads("))
            literal = payload_line.removeprefix("payload = json.loads(").removesuffix(")")
            payload = json.loads(ast.literal_eval(literal))
            target = self.root / str(payload["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(payload["content"]), encoding="utf-8")
        return WorkerProcessResult(0, "verified", "", False)

    async def cancel(self, execution_id: str) -> None:
        """Record lifecycle cleanup and remove the simulated task worker."""
        self.cancelled.append(execution_id)
        self.workers.pop(execution_id, None)


class CodeActAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    """Exercise programming, scene, and map loops through one Gateway."""

    async def test_read_edit_verify_diff_attribution_and_cleanup(self) -> None:
        """All write roles receive validation and task-owned diff evidence before cleanup."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "player.gd").write_text("extends Node\n", encoding="utf-8")
            (root / "level.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
            (root / "map.tscn").write_text("[gd_scene format=3]\n", encoding="utf-8")
            (root / "z_notes.txt").write_text("baseline\n", encoding="utf-8")
            _git(root, "init")
            _git(root, "config", "user.email", "codeact@example.invalid")
            _git(root, "config", "user.name", "CodeAct Test")
            _git(root, "add", ".")
            _git(root, "commit", "-m", "baseline")
            (root / "z_notes.txt").write_text("pre-existing user diff\n", encoding="utf-8")

            settings = AppSettings(
                project_root=root,
                codeact_map_validator_command=["python3", "tools/validate_map.py"],
            )
            gateway = ExecutionGateway(settings)
            fake = _AcceptanceWorkerManager(root, root / ".worker-tasks")
            gateway._worker_manager = cast(WorkerManager, fake)
            gateway._validation = ValidationSelector(cast(WorkerManager, fake), settings)
            security = SecuritySettings(project_root=root)

            programming = await gateway.execute(
                _edit_request("programming-exec", "programming", "player.gd", "extends Node", "extends Node2D"),
                ToolContext(security=security, session_id="session", agent_role="programming"),
            )
            self.assertEqual(programming.status, "ok")
            self.assertEqual(programming.data["validation"]["status"], "passed")
            self.assertIn("player.gd", programming.data["diff"])
            self.assertNotIn("pre-existing user diff", programming.data["diff"])

            scene = await gateway.execute(
                _edit_request("scene-exec", "scene", "level.tscn", "format=3", "format=3 load_steps=1"),
                ToolContext(security=security, session_id="session", agent_role="scene"),
            )
            self.assertEqual(scene.data["validation"]["verifier"], "resource_load:PackedScene")

            map_state = MapTaskState()
            map_scope = bind_map_task(
                new_request_scope(request_id="map-request", user_message="edit the map"),
                "map-task",
            )
            map_result = await gateway.execute(
                _edit_request("map-exec", "map", "map.tscn", "format=3", "format=3 load_steps=1"),
                ToolContext(
                    security=security,
                    session_id="session",
                    agent_role="map",
                    map_task_state=map_state,
                    map_request_scope=map_scope,
                ),
            )
            self.assertEqual(map_result.data["validation"]["verifier"], "map_range_semantic_target")
            self.assertEqual(map_state.codeact_execution["task_execution_id"], "map-exec")

            created = await gateway.execute(
                CodeActRequest(
                    task_execution_id="create-exec",
                    task_id="task",
                    role="programming",
                    call_id="create-exec:edit",
                    tool=CodeActToolName.PROJECT_EDIT,
                    arguments={"path": "new_script.gd", "content": "extends Node\n"},
                ),
                ToolContext(security=security, session_id="session", agent_role="programming"),
            )
            self.assertIn("b/new_script.gd", created.data["diff"])
            self.assertEqual(created.data["validation"]["status"], "passed")

            self.assertEqual(len({worker.task_directory for worker in fake.workers.values()}), 4)
            await gateway.cancel("programming-exec")
            self.assertIn("programming-exec", fake.cancelled)
            self.assertNotIn("programming-exec", fake.workers)


def _edit_request(
    execution_id: str,
    role: str,
    path: str,
    old_text: str,
    new_text: str,
) -> CodeActRequest:
    """Build one stable small-patch request for an acceptance role."""
    return CodeActRequest(
        task_execution_id=execution_id,
        task_id="task",
        role=cast(CodeActRole, role),
        call_id=f"{execution_id}:edit",
        tool=CodeActToolName.PROJECT_EDIT,
        arguments={"path": path, "old_text": old_text, "new_text": new_text},
    )


def _git(root: Path, *arguments: str) -> None:
    """Run a checked local Git setup command for diff-attribution acceptance."""
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
